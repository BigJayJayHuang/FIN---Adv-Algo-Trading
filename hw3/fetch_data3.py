"""
HW3 - LSEG fetcher (INTC wheel: cash-secured puts -> assignment -> covered calls -> called away)
==================================================================================================
Run on the Mac, with LSEG Workspace open and signed in (same setup as HW1.2/HW2):

    cd site/hw3
    python3 fetch_data3.py          # real pull  -> raw_data3.pkl   (git-ignored)
    python3 build_book3.py          # backtest   -> book3.js        (this is what the site reads)

This is NOT a fixed weekly-call loop like HW1.2/HW2 - which contract to pull next depends on
how the PRIOR week ended (still flat -> sell another put; just got assigned -> sell a covered
call instead), so this script walks the trade weeks in order and makes that call itself, using
strategy.py's pure, no-lookahead rules (read off the stock price alone - vol regime for puts,
cost-basis-vs-spot for calls). build_book3.py then independently re-derives every one of these
decisions from the saved raw bars for the Checks section; it does not trust this script's
bookkeeping, only the fact that these are the real contracts that existed.

RICs that do not exist just come back empty and are skipped. Nothing is invented.
This file is not graded; the published site is.
"""
import argparse
import datetime as dt
import os
import pickle
import sys
import time
import warnings

import numpy as np
import pandas as pd

from rics import build_option_ric, candidate_strikes
from strategy import (daily_closes, trailing_realized_vol, classify_regime, in_earnings_blackout,
                       put_target_strike, call_target_strike, early_close_trigger,
                       VOL_LOOKBACK_DAYS, REGIME_HISTORY, MIN_REGIME_HISTORY,
                       PUT_OTM, CALL_OTM, EARLY_CLOSE_MULT)

warnings.filterwarnings("ignore", category=FutureWarning, module="lseg.data")

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "raw_data3.pkl")

# ---- knobs -------------------------------------------------------------------------------
STOCK_RIC = "INTC.OQ"        # Intel, Nasdaq. Edit here if your feed spells it differently.
ROOT = "INTC"
INTERVAL = "hourly"
INTERVAL_FALLBACKS = ["1h", "60min"]
TRADE_WEEKS = 22              # ~5 months of weekly decisions
BURNIN_WEEKS = 5              # extra stock-only history before week 1, for the 20-day vol lookback
PUT_BAND = (0.01, 0.12)       # strike search band below spot, for the put leg
CALL_BAND = (0.01, 0.10)      # strike search band above spot, for the call leg
FIELDS = ["BID", "ASK", "TRDPRC_1", "OPEN_PRC", "HIGH_1", "LOW_1", "ACVOL_UNS", "NUM_MOVES"]
ENTRY_HOUR = 10               # Monday entry: first bar at/after this hour, ET
SESSION_LO = 9 * 60            # regular session window, ET minutes-of-day - must match build_book3.py's
SESSION_HI = 15 * 60 + 59      # SESSION_LO/SESSION_HI exactly, or the two scripts' vol/regime calls can
                               # disagree (an extended-hours print landing in "the day's last bar" here
                               # but not there) even though both import the same trailing_realized_vol
# -----------------------------------------------------------------------------------------


ET = "America/New_York"


def last_fridays(n: int, today: dt.date = None):
    today = today or dt.date.today()
    d = today - dt.timedelta(days=1)
    while d.weekday() != 4:
        d -= dt.timedelta(days=1)
    return [d - dt.timedelta(days=7 * i) for i in range(n)][::-1]


def detect_et(idx):
    """Same detection build_book3.py does independently at build time - duplicated here (not
    imported) only so this script's OWN walk-forward decisions (which strike band to search,
    whether Friday's print was above or below the strike) use the same ET session hours build
    will later re-derive, instead of silently drifting and wasting weeks that build then has to
    throw out for a side mismatch. Returns a converter function; stock_et = stock with that
    applied to its index, used only for timing decisions below - the RAW stock (saved to the
    pickle) is untouched."""
    idx = pd.DatetimeIndex(idx)
    if idx.tz is not None:
        return lambda i: pd.DatetimeIndex(i).tz_convert(ET)

    def score(loc):
        loc = loc[~loc.isna()]
        m = loc.hour * 60 + loc.minute
        return float(((m >= 9 * 60) & (m <= 15 * 60 + 59)).mean()) if len(loc) else 0.0

    as_utc = idx.tz_localize("UTC").tz_convert(ET)
    as_et = idx.tz_localize(ET, ambiguous="NaT", nonexistent="NaT")
    if score(as_utc) >= score(as_et):
        return lambda i: pd.DatetimeIndex(i).tz_localize("UTC").tz_convert(ET)
    return lambda i: pd.DatetimeIndex(i).tz_localize(ET, ambiguous="NaT", nonexistent="NaT")


def session_only_factory(ref_index):
    """Returns a session_only(df) function: ET-converts df's index using the SAME converter
    detected from ref_index (so every dataframe sharing that raw timestamp convention - the stock
    bars, every option contract's bars - goes through one consistent conversion), then restricts
    to the regular session (9:00-15:59 ET, weekdays) - the same window build_book3.py's clean_frame
    applies. Hoisted to module level (rather than a closure inside main()) so other scripts, e.g.
    fetch_wide3.py for the parameter sweep, can reuse this exact logic instead of re-deriving it."""
    et_conv = detect_et(ref_index)

    def session_only(df):
        d = df.copy()
        d.index = et_conv(d.index)
        m = d.index.hour * 60 + d.index.minute
        return d[(m >= SESSION_LO) & (m <= SESSION_HI) & (d.index.dayofweek < 5)]

    return session_only


def tidy(df):
    if df is None or len(df) == 0:
        return None
    df = df.copy()
    if isinstance(df.columns, pd.MultiIndex):
        for lvl in range(df.columns.nlevels):
            if set(df.columns.get_level_values(lvl)) & set(FIELDS):
                df.columns = df.columns.get_level_values(lvl)
                break
    df.index = pd.to_datetime(df.index)
    df.index.name = "ts"
    for f in FIELDS:
        if f not in df.columns:
            df[f] = np.nan
    df = df[FIELDS].apply(pd.to_numeric, errors="coerce")
    df = df.dropna(how="all")
    df = df[~df.index.duplicated(keep="last")].sort_index()
    return df if len(df) else None


LAST_ERROR = {"msg": None}


def pull(ld, ric, start, end):
    attempts = [(FIELDS, True), (FIELDS, False)]
    for fields, with_count in attempts:
        kw = dict(universe=ric, fields=fields, interval=INTERVAL, start=start, end=end)
        if with_count:
            kw["count"] = 10000
        try:
            df = ld.get_history(**kw)
        except Exception as e:
            LAST_ERROR["msg"] = f"{type(e).__name__}: {e}"
            continue
        return tidy(df)
    return None


def monday_spot(stock_et, mon_date):
    """stock_et must already be ET-indexed (see detect_et above). First bar at/after 10:00 ET,
    same definition build_book3.py uses independently - falls back to the day's first bar if
    nothing is at/after 10:00 (a short/holiday-adjacent day)."""
    day_rows = stock_et[stock_et.index.date == mon_date]
    minutes = day_rows.index.hour * 60 + day_rows.index.minute
    at_or_after = day_rows[minutes >= ENTRY_HOUR * 60]
    ref = (at_or_after if len(at_or_after) else day_rows)["TRDPRC_1"].dropna()
    if ref.empty:
        return None, None
    return float(ref.iloc[0]), ref.index[0]


def entry_fill(week_df, entry_ts):
    at_or_after = week_df[week_df.index >= entry_ts]
    for ts, row in at_or_after.iterrows():
        bid, ask = row.get("BID"), row.get("ASK")
        if not pd.isna(bid) and not pd.isna(ask):
            return ts, (bid + ask) / 2
    return None, None


def resolve_contract(ld, root, F, side, cands, start, end, tried):
    """cands already ordered best-first (closest to target). Returns (ric, K, df) or None."""
    for K in cands:
        for expired, padded in [(False, False), (True, False), (False, True), (True, True)]:
            ric = build_option_ric(root, F, K, side, expired, padded)
            tried.append(ric)
            df = pull(ld, ric, start, end)
            if df is not None and df[["BID", "ASK"]].notna().any().any():
                return ric, K, df
    return None


def main():
    global INTERVAL
    ap = argparse.ArgumentParser()
    ap.add_argument("--fresh", action="store_true", help="ignore raw_data3.pkl and start over")
    args = ap.parse_args()
    import lseg.data as ld

    expiries = last_fridays(TRADE_WEEKS)
    window_start = expiries[0] - dt.timedelta(days=4)
    burnin_start = window_start - dt.timedelta(weeks=BURNIN_WEEKS)
    print(f"Trade weeks: {expiries[0]} .. {expiries[-1]} ({len(expiries)} weeks); "
          f"stock history from {burnin_start} ({BURNIN_WEEKS} burn-in weeks for the vol lookback)")
    ld.open_session()

    probe_start = window_start.isoformat()
    probe_end = (window_start + dt.timedelta(days=5)).isoformat()
    for cand in [INTERVAL] + INTERVAL_FALLBACKS:
        INTERVAL = cand
        probe = pull(ld, STOCK_RIC, probe_start, probe_end)
        if probe is not None:
            print(f"Interval '{INTERVAL}' works: {len(probe)} stock bars in week 1.")
            break
    else:
        ld.close_session()
        sys.exit(f"No stock bars for {STOCK_RIC} with any interval name. Last error: {LAST_ERROR['msg']}\n"
                  f"If INTC's RIC is spelled differently on your feed, edit STOCK_RIC at the top of this file.")

    # ---- resume support ---------------------------------------------------------------
    stock_parts, options, tried, weeks = [], {}, [], []
    done_through = None
    if os.path.exists(OUT) and not args.fresh:
        try:
            with open(OUT, "rb") as f:
                old = pickle.load(f)
            stock_parts.append(old["stock"])
            options.update(old["options"])
            tried.extend(old.get("tried", []))
            weeks = old["meta"].get("weeks", [])
            if weeks:
                done_through = max(dt.date.fromisoformat(w["expiry"]) for w in weeks)
                print(f"Resuming from {OUT}: {len(weeks)} weeks already resolved, through {done_through}.")
        except Exception as e:
            print(f"Could not reuse the old raw_data3.pkl ({e}); pulling everything.")

    # ---- stock history, whole window in ~4-week chunks ---------------------------------
    t0 = time.time()
    chunk_start = burnin_start
    final_end = expiries[-1] + dt.timedelta(days=1)
    have_stock = pd.concat(stock_parts).sort_index() if stock_parts else pd.DataFrame()
    while chunk_start < final_end:
        chunk_end = min(chunk_start + dt.timedelta(weeks=4), final_end)
        if len(have_stock) and have_stock.index.min().date() <= chunk_start and have_stock.index.max().date() >= chunk_end - dt.timedelta(days=1):
            chunk_start = chunk_end
            continue
        s = pull(ld, STOCK_RIC, chunk_start.isoformat(), chunk_end.isoformat())
        if s is not None:
            stock_parts.append(s)
            have_stock = pd.concat(stock_parts).sort_index()
            have_stock = have_stock[~have_stock.index.duplicated(keep="last")]
        print(f"  stock {chunk_start} .. {chunk_end}: {'ok' if s is not None else 'NO BARS'}  "
              f"({time.time()-t0:.0f}s)")
        chunk_start = chunk_end

    if not stock_parts:
        ld.close_session()
        sys.exit("No stock data came back. Is LSEG Workspace open and signed in?")
    stock = pd.concat(stock_parts).sort_index()
    stock = stock[~stock.index.duplicated(keep="last")]
    # ONE converter, derived once from the stock index, reused for every dataframe (stock AND every
    # option contract) - build_book3.py does the same thing (make_converter(stock_raw.index) reused
    # for both). LSEG stamps come back as naive UTC; this both localizes to ET AND, since the option
    # legs never went through their own detection, is the only thing that makes their timestamps ET
    # at all - comparing them raw against a naive "10:00" target was silently picking the wrong bar.
    session_only = session_only_factory(stock.index)
    stock_et_session = session_only(stock)
    closes = daily_closes(stock_et_session)
    print(f"Stock: {len(stock)} bars, {len(closes)} trading days, {closes.index.min()} .. {closes.index.max()}")

    # ---- replay prior weeks' state (if resuming) to know where we left off -------------
    state = "FLAT"
    cost_basis = None
    vol_history = []
    for w in weeks:
        vol_history.append(w.get("vol_today"))
        state = w["state_after"]
        cost_basis = w.get("cost_basis_after")

    # ---- walk the trade weeks in order --------------------------------------------------
    for wi, F in enumerate(expiries, 1):
        if done_through is not None and F <= done_through:
            continue
        mon = F - dt.timedelta(days=4)
        start, end = mon.isoformat(), (F + dt.timedelta(days=1)).isoformat()

        # Check for a Monday reference price FIRST, exactly like build_book3.py's resolve_weeks
        # does (it checks ref.empty before ever calling trailing_realized_vol) - a week with no
        # Monday bar contributes a None placeholder to vol_history, not a real number. Computing
        # vol_today unconditionally (the old order) stuffed a real reading into a week build skips,
        # which shifts which weeks land in classify_regime's trailing window for every week after.
        spot, entry_ts_target = monday_spot(stock_et_session, mon)
        if spot is None:
            print(f"[{wi}/{len(expiries)}] {F}: no Monday stock bars; skipping the week entirely")
            vol_history.append(None)
            weeks.append({"expiry": F.isoformat(), "side": None, "outcome": "NO_STOCK",
                          "vol_today": None, "regime": None,
                          "state_before": state, "state_after": state,
                          "cost_basis_before": cost_basis, "cost_basis_after": cost_basis})
            continue

        vol_today = trailing_realized_vol(closes, mon, lookback=VOL_LOOKBACK_DAYS)
        regime = classify_regime(vol_today, vol_history)
        vol_history.append(vol_today)

        if in_earnings_blackout(mon):
            print(f"[{wi}/{len(expiries)}] {F}: earnings blackout week; skipping (state unchanged)")
            weeks.append({"expiry": F.isoformat(), "side": None, "outcome": "EARNINGS_BLACKOUT",
                          "vol_today": vol_today, "regime": regime,
                          "state_before": state, "state_after": state,
                          "cost_basis_before": cost_basis, "cost_basis_after": cost_basis})
            continue

        if state == "FLAT":
            side = "P"
            target = put_target_strike(spot, regime)
            lo, hi = spot * (1 - PUT_BAND[1]), spot * (1 - PUT_BAND[0])
            cands = sorted([k for k in candidate_strikes(lo, hi) if k <= target],
                           key=lambda k: target - k)
            tier = None
        else:
            side = "C"
            target, tier = call_target_strike(spot, cost_basis)
            lo, hi = spot * (1 + CALL_BAND[0]), spot * (1 + CALL_BAND[1])
            cands = sorted([k for k in candidate_strikes(lo, hi) if k >= target],
                           key=lambda k: k - target)
            regime = None

        print(f"[{wi}/{len(expiries)}] {F}: state={state} side={side} spot={spot:.2f} "
              f"vol={vol_today if vol_today is None else round(vol_today,3)} regime={regime} tier={tier} "
              f"target={target:.2f} trying {len(cands)} strikes")

        found = resolve_contract(ld, ROOT, F, side, cands, start, end, tried)
        if found is None:
            print(f"        !! nothing resolved this week; skipping (state unchanged)")
            weeks.append({"expiry": F.isoformat(), "side": side, "outcome": "SKIP",
                          "vol_today": vol_today, "regime": regime, "tier": tier, "target": target,
                          "state_before": state, "state_after": state,
                          "cost_basis_before": cost_basis, "cost_basis_after": cost_basis})
            continue

        ric, K, df = found
        options[ric] = df  # raw, UTC-stamped - build_book3.py applies its own conversion+session
        # filter on load. Our OWN walk-forward decisions (entry fill, early close) have to go
        # through that same ET-convert-then-restrict-to-session step, or they silently compare a
        # naive "10:00" target against raw UTC stamps and pick the wrong bar entirely.
        df_session = session_only(df)
        # df_session's index is tz-aware ET (session_only/et_conv localize it) - the comparison
        # target has to be tz-aware too, the same way build_book3.py builds its own entry_target
        # with tz=ET, or this raises "Cannot compare tz-naive and tz-aware datetime-like objects".
        entry_target = pd.Timestamp(mon.year, mon.month, mon.day, ENTRY_HOUR, tz=ET)
        entry_ts, entry_premium = entry_fill(df_session, entry_target)
        early = early_close_trigger(df_session, entry_premium, entry_ts) if entry_ts is not None else None

        friday_rows = stock_et_session[stock_et_session.index.date == F]
        friday_spot = float(friday_rows["TRDPRC_1"].dropna().iloc[-1]) if len(friday_rows) else None

        state_before, cost_before = state, cost_basis
        if early is not None:
            outcome = "CLOSE"
            # state unchanged by an early close either way (put: never assigned; call: still holding)
        elif friday_spot is None:
            outcome = "NO_FRIDAY_BAR"
        elif side == "P":
            if friday_spot < K:
                outcome, state, cost_basis = "ASSIGN", "HOLDING", K
            else:
                outcome = "EXPIRE"
        else:
            if friday_spot > K:
                outcome, state, cost_basis = "CALLED_AWAY", "FLAT", None
            else:
                outcome = "EXPIRE"

        print(f"        -> {ric}  K={K}  entry={entry_premium}  early={'yes' if early else 'no'}  "
              f"fri_spot={friday_spot}  outcome={outcome}  next_state={state}  ({time.time()-t0:.0f}s)")

        weeks.append({
            "expiry": F.isoformat(), "side": side, "ric": ric, "strike": K, "target": target,
            "spot": spot, "vol_today": vol_today, "regime": regime, "tier": tier,
            "entry_ts": None if entry_ts is None else entry_ts.isoformat(),
            "entry_premium": entry_premium,
            "early_close_ts": None if early is None else early[0].isoformat(),
            "early_close_mid": None if early is None else early[1],
            "friday_spot": friday_spot, "outcome": outcome,
            "state_before": state_before, "state_after": state,
            "cost_basis_before": cost_before, "cost_basis_after": cost_basis,
        })

        # checkpoint after every week - a long multi-month pull should survive a dropped session
        payload = {
            "meta": {
                "stock_ric": STOCK_RIC, "root": ROOT, "interval": INTERVAL,
                "trade_weeks": [d.isoformat() for d in expiries],
                "burnin_weeks": BURNIN_WEEKS, "window_start": window_start.isoformat(),
                "fetched_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "source": "LSEG", "demo": False, "weeks": weeks,
                "params": {"vol_lookback_days": VOL_LOOKBACK_DAYS, "regime_history": REGIME_HISTORY,
                           "min_regime_history": MIN_REGIME_HISTORY, "put_otm": PUT_OTM,
                           "call_otm": CALL_OTM, "early_close_mult": EARLY_CLOSE_MULT,
                           "put_band": PUT_BAND, "call_band": CALL_BAND},
            },
            "stock": stock, "options": options, "tried": tried,
        }
        with open(OUT, "wb") as f:
            pickle.dump(payload, f)

    ld.close_session()
    n_resolved = sum(1 for w in weeks if w.get("ric"))
    print(f"\nDone. {len(stock)} stock bars, {len(options)} option RICs with data, "
          f"{n_resolved}/{len(weeks)} weeks resolved to a real contract.")
    print(f"Saved {OUT}\nNext: python3 build_book3.py")


if __name__ == "__main__":
    main()
