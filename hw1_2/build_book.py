"""
HW1.2 covered-call backtest  ->  book.js
=========================================
    python3 fetch_data.py          # LSEG pull   -> raw_data.pkl
    python3 build_book.py          # backtest    -> book.js   (the site reads only book.js)
    python3 build_book.py --demo   # synthetic data, for testing the site (NEVER turn this in)

The strategy (the whole thing, so the site and the code say the same thing):

  Entry day = Monday (first trading day of the week if Monday is a holiday).
  Order time = first hourly bar at or after 10:00 ET on that day.

  Rule 1  If flat: BUY 100 shares at the stock print (TRDPRC_1 of that bar). Cash goes down.
  Rule 2  Strike: the nearest listed call strike at or above spot (ATM if spot sits exactly on a
          strike), for the Friday expiry of that week.
  Rule 3  SELL 1 call, limit = fill = mid = (BID+ASK)/2 at that same bar. Cash goes up by 100 x mid.
          No bid/ask on that call at that bar  ->  skip the week. Nothing is invented.
  Rule 4  Wait through expiry. No rolls, no buy-to-close.
            stock <= strike at the last Friday bar -> call EXPIRES, keep shares and premium
            stock >  strike                        -> ASSIGNED: shares called away at strike, flat
  Rule 5  Reg T, not portfolio margin.
            NAV = cash + stock MV + option MV (short call is negative)
            Initial = 50% of stock LMV (a covered short call adds $0), Maintenance = 25% of LMV
            Available funds = NAV - Initial,  Excess = NAV - Maintenance
            If available funds would go negative, the trade cannot be put on -> skip and say so.

MY VERSION (same engine, one knob different): the strike rule becomes "first listed strike at or above
spot x (1 + VARIANT_OTM)". Baseline = VARIANT 0%. Both are run on the same bars and shown on the site.

Cash moves only on blotter events: buy stock, collect premium, expire (0), assignment (+100 x strike).
"""
import argparse
import datetime as dt
import json
import math
import os
import pickle
import sys

import numpy as np
import pandas as pd

from rics import parse_call_ric

HERE = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(HERE, "raw_data.pkl")
OUT_JS = os.path.join(HERE, "book.js")
ET = "America/New_York"

# ---- strategy knobs ----------------------------------------------------------
START_CASH = 50_000.0
LOT = 100                        # shares per option contract
ENTRY_MIN = 10 * 60              # first bar at/after 10:00 ET
SESSION_LO = 9 * 60              # keep bars stamped 09:00 ..
SESSION_HI = 15 * 60 + 59        # .. 15:59 ET (regular session; later bars can hold after-hours prints)
REGT_INITIAL = 0.50
REGT_MAINT = 0.25
VARIANT_OTM = 0.03               # my version: first listed strike at or above spot x 1.03
ITM_EPS = 0.005                  # assigned only if stock > strike by more than half a cent
# ------------------------------------------------------------------------------


# ============================ loading & clean-up ==============================
def load_payload(demo: bool):
    if demo:
        from demo_data import make_raw
        return make_raw()
    if not os.path.exists(RAW):
        sys.exit("raw_data.pkl not found. Run fetch_data.py first (needs LSEG Workspace), or use --demo.")
    with open(RAW, "rb") as f:
        return pickle.load(f)


def make_converter(idx):
    """Return (converter, description). LSEG intraday stamps are UTC; if the frame is
    tz-naive we test both readings (UTC vs already-ET) and keep the one that puts the
    bars inside the 09:00-15:59 ET session."""
    idx = pd.DatetimeIndex(idx)
    if idx.tz is not None:
        return (lambda i: pd.DatetimeIndex(i).tz_convert(ET)), "stamps carried a timezone and were converted to ET; only regular-session bars (stamped 09:00-15:59 ET) are used"

    def score(loc):
        loc = loc[~loc.isna()]
        m = loc.hour * 60 + loc.minute
        return float(((m >= SESSION_LO) & (m <= SESSION_HI)).mean()) if len(loc) else 0.0

    as_utc = idx.tz_localize("UTC").tz_convert(ET)
    as_et = idx.tz_localize(ET, ambiguous="NaT", nonexistent="NaT")
    if score(as_utc) >= score(as_et):
        return (lambda i: pd.DatetimeIndex(i).tz_localize("UTC").tz_convert(ET)), \
            "LSEG stamps read as UTC and converted to ET; only regular-session bars (stamped 09:00-15:59 ET) are used"
    return (lambda i: pd.DatetimeIndex(i).tz_localize(ET, ambiguous="NaT", nonexistent="NaT")), \
        "LSEG stamps read as already ET; only regular-session bars (stamped 09:00-15:59 ET) are used"


def clean_frame(df, conv):
    df = df.copy()
    # LSEG can hand back nullable dtypes (pd.NA); everything downstream wants plain float + NaN
    df = df.apply(pd.to_numeric, errors="coerce").astype("float64")
    df.index = conv(df.index)
    df = df[~df.index.isna()]
    df = df[~df.index.duplicated(keep="last")].sort_index()
    m = df.index.hour * 60 + df.index.minute
    df = df[(m >= SESSION_LO) & (m <= SESSION_HI) & (df.index.dayofweek < 5)]
    return df


def valid_quote(q):
    """q = (bid, ask, ...) -> True only if both sides are real and the market is sane."""
    if q is None:
        return False
    b, a = q[0], q[1]
    return (b == b) and (a == a) and b > 0 and a > 0 and a >= b


# ================================= helpers ====================================
def iso(ts):
    return ts.isoformat()


def lbl(ts):
    return ts.strftime("%a %b %d %H:%M").replace(" 0", " ")


def r(x, n=4):
    return None if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))) else round(float(x), n)


def account(cash, shares, S, call_mark):
    """Reg T account at one instant. call_mark = option mid per share, or None."""
    stock_mv = shares * S
    opt_mv = -LOT * call_mark if call_mark is not None else 0.0
    nav = cash + stock_mv + opt_mv
    init = REGT_INITIAL * stock_mv
    maint = REGT_MAINT * stock_mv
    return dict(stockMV=stock_mv, optMV=opt_mv, nav=nav, lmv=stock_mv, init=init, maint=maint,
                avail=nav - init, excess=nav - maint)


# ================================= the backtest ===============================
def run(payload, otm_pct=0.0):
    meta_in = payload["meta"]
    stock_raw, options_raw = payload["stock"], payload["options"]

    conv, tz_note = make_converter(stock_raw.index)
    stock = clean_frame(stock_raw, conv)
    stock = stock[stock["TRDPRC_1"].notna() | stock["BID"].notna()]
    S_print = stock["TRDPRC_1"]

    # ---- option chains, by expiry -------------------------------------------
    chains = {}      # expiry(date) -> list of dict(strike, ric, q{ts:(bid,ask,trd,...)}, df)
    for ric, df in options_raw.items():
        p = parse_call_ric(ric)
        if not p:
            continue
        d = clean_frame(df, conv)
        if d.empty:
            continue
        q = {ts: (row.BID, row.ASK, row.TRDPRC_1, row.ACVOL_UNS, row.NUM_MOVES)
             for ts, row in zip(d.index, d.itertuples(index=False))}
        chains.setdefault(p["expiry"], []).append(dict(strike=p["strike"], ric=ric, q=q, n=len(d)))
    for v in chains.values():
        v.sort(key=lambda c: c["strike"])

    # ---- weeks --------------------------------------------------------------
    expiries = [dt.date.fromisoformat(s) for s in meta_in["expiries"]]
    day_bars = {}
    for ts in stock.index:
        day_bars.setdefault(ts.date(), []).append(ts)

    weeks = []
    for i, F in enumerate(expiries, 1):
        days = sorted(d for d in day_bars if F - dt.timedelta(days=4) <= d <= F)
        w = dict(i=i, expiry=F, entry_ts=None, expiry_ts=None, skip=None, first_ts=None)
        if not days:
            w["skip"] = "no stock bars this week"
        elif F not in days:
            w["skip"] = "market closed on the expiry Friday (no bars)"
        else:
            eday = days[0]
            w["first_ts"] = day_bars[eday][0]
            w["expiry_ts"] = day_bars[F][-1]
            cand = [t for t in day_bars[eday]
                    if t.hour * 60 + t.minute >= ENTRY_MIN and pd.notna(S_print.get(t, np.nan))]
            if cand:
                w["entry_ts"] = cand[0]
            else:
                w["skip"] = "no stock print at/after 10:00 ET on the entry day"
        weeks.append(w)

    runnable = [w for w in weeks if w["expiry_ts"] is not None]
    if not runnable:
        sys.exit("No usable weeks in the data.")
    first_ts = min(w["first_ts"] for w in runnable)
    last_ts = max(w["expiry_ts"] for w in runnable)
    entry_map = {w["entry_ts"]: w for w in weeks if w["entry_ts"] is not None}
    expiry_map = {w["expiry_ts"]: w for w in weeks if w["expiry_ts"] is not None}
    timeline = [ts for ts in stock.index if first_ts <= ts <= last_ts]

    # ---- state ----------------------------------------------------------------
    cash, shares = START_CASH, 0
    call = None            # dict(strike, expiry, ric, mark, mid0, week)
    last_S = None
    ledger, blotter, week_out, skips = [], [], {}, []
    hold_cash = None

    def fmt_call(F, K):
        return f"AAPL {F.strftime('%d %b %Y').lstrip('0')} {K:g} C"

    def add_blotter(ts, instr, ric, side, qty, limit, fill, bid, ask, dcash, dshares, rule, note, wk):
        nonlocal cash, shares
        cash += dcash
        shares += dshares
        acc = account(cash, shares, last_S, call["mark"] if call else None)
        blotter.append(dict(
            n=len(blotter) + 1, t=iso(ts), lbl=lbl(ts), instr=instr, ric=ric, side=side, qty=qty,
            limit=r(limit), fill=r(fill), bid=r(bid), ask=r(ask), dcash=r(dcash, 2), cash=r(cash, 2),
            dshares=dshares, shares=shares, avail=r(acc["avail"], 2), rule=rule, note=note, week=wk["i"]))

    for ts in timeline:
        v = S_print.get(ts, np.nan)
        if pd.notna(v):
            last_S = float(v)
        if last_S is None:
            continue
        S = last_S
        event = None

        # ---- mark the open call (mid if quoted, else last mid, flagged stale) ----
        stale = False
        if call:
            q = call["q"].get(ts)
            if q is not None and q[0] == q[0] and q[1] == q[1] and q[1] >= q[0] >= 0:
                call["mark"] = (q[0] + q[1]) / 2
            else:
                stale = True

        # ---- Rule 4: expiry -------------------------------------------------------
        wk_exp = expiry_map.get(ts)
        if wk_exp and call and call["expiry"] == wk_exp["expiry"]:
            K, F, cric = call["strike"], call["expiry"], call["ric"]
            instr = fmt_call(F, K)
            ent = week_out[wk_exp["i"]]
            call = None                      # the option is gone before we book the post-settlement state
            if S - K > ITM_EPS:
                add_blotter(ts, instr, cric, "ASSIGN", 1, None, K, None, None, LOT * K, -LOT, "Rule 4",
                            f"Stock {S:.2f} > strike {K:g} at expiry -> ITM, assigned: 100 shares called away at "
                            f"{K:g}, cash +100 x {K:g}. Flat. Next entry day starts the combo again.", wk_exp)
                ent.update(outcome="ASSIGN")
                event = "ASSIGN"
            else:
                add_blotter(ts, instr, cric, "EXPIRE", 1, None, 0.0, None, None, 0.0, 0, "Rule 4",
                            f"Stock {S:.2f} <= strike {K:g} at expiry -> OTM, expires worthless. Keep the shares and "
                            f"the premium; next entry day just writes the next Friday call.", wk_exp)
                ent.update(outcome="EXPIRE")
                event = "EXPIRE"
            ent.update(S_T=r(S, 2))

        # ---- Rules 1-3: entry -----------------------------------------------------
        wk_in = entry_map.get(ts)
        if wk_in:
            row = dict(week=wk_in["i"], expiry=wk_in["expiry"].isoformat(), entry_t=iso(ts), entry_lbl=lbl(ts),
                       spot=r(S, 2), strike=None, bid=None, ask=None, mid=None, outcome=None, skip=None,
                       S_T=None, nav_start=None, nav_end=None)
            week_out[wk_in["i"]] = row
            chain = chains.get(wk_in["expiry"], [])
            spot_c = round(S, 2)
            target_c = round(S * (1 + otm_pct), 2)          # baseline: spot itself. mine: spot x 1.03
            listed = [c for c in chain if c["strike"] >= target_c - 1e-9]
            reason = None
            if call is not None:
                reason = "an earlier call is still open (should not happen)"
            elif not listed:
                reason = "no listed call at or above the target strike in the data"
            else:
                c = listed[0]                       # nearest strike at/above spot (chain is sorted)
                q = c["q"].get(ts)
                row["strike"] = c["strike"]
                if not valid_quote(q):
                    reason = (f"no bid/ask on the {c['strike']:g} call at {lbl(ts)} - skip the week, "
                              f"no print invented")
                else:
                    bid, ask = float(q[0]), float(q[1])
                    mid = (bid + ask) / 2
                    row.update(bid=r(bid), ask=r(ask), mid=r(mid))
                    buying = shares == 0
                    sh_after = shares + (LOT if buying else 0)
                    cash_after = cash - (LOT * S if buying else 0.0) + LOT * mid
                    pro = account(cash_after, sh_after, S, mid)
                    if pro["avail"] < 0:
                        reason = (f"Reg T: available funds would be {pro['avail']:,.0f} after the combo - "
                                  f"the trade could not have been put on")
                    else:
                        # Rule 1
                        if buying:
                            add_blotter(ts, "AAPL", "AAPL.O", "BUY", LOT, None, S, None, None, -LOT * S, LOT,
                                        "Rule 1", f"Flat on entry day -> buy 100 at the stock print "
                                        f"(TRDPRC_1 of the {ts.strftime('%H:%M')} bar).", wk_in)
                        # Rules 2 + 3
                        call = dict(strike=c["strike"], expiry=wk_in["expiry"], ric=c["ric"], mark=mid, week=wk_in["i"], q=c["q"])
                        otm = (c["strike"] / S - 1) * 100
                        if otm_pct == 0:
                            why = (f"Strike rule: nearest listed strike at/above spot {S:.2f} -> {c['strike']:g} "
                                   f"({otm:+.2f}% from spot"
                                   f"{', ATM' if abs(c['strike'] - spot_c) < 1e-9 else ''}). ")
                        else:
                            why = (f"Strike rule: target = spot {S:.2f} x {1 + otm_pct:.2f} = {target_c:.2f}; first listed "
                                   f"strike at/above it -> {c['strike']:g} ({otm:+.2f}% from spot). ")
                        add_blotter(ts, fmt_call(wk_in["expiry"], c["strike"]), c["ric"], "SELL", 1, mid, mid, bid, ask,
                                    LOT * mid, 0, "Rules 2-3",
                                    why + f"Limit = fill = mid = ({bid:.2f}+{ask:.2f})/2.", wk_in)
                        event = "OPEN"
            if reason:
                row["skip"] = reason
                row["outcome"] = "SKIP"
                skips.append(dict(week=wk_in["i"], t=iso(ts), lbl=lbl(ts), reason=reason,
                                  holding=shares > 0))
                event = "SKIP"

        # ---- ledger row (post-event) ----------------------------------------------
        mark = call["mark"] if call else None
        acc = account(cash, shares, S, mark)
        if hold_cash is None and any(b["side"] == "BUY" for b in blotter):
            hold_cash = START_CASH - LOT * S
        hold_nav = (hold_cash + LOT * S) if hold_cash is not None else START_CASH
        ledger.append(dict(
            t=iso(ts), lbl=lbl(ts), d=ts.strftime("%Y-%m-%d"), S=r(S, 2), sh=shares,
            call=None if not call else dict(k=call["strike"], exp=call["expiry"].isoformat(), q=1,
                                            mark=r(call["mark"]), stale=stale, ric=call["ric"]),
            cash=r(cash, 2), stockMV=r(acc["stockMV"], 2), optMV=r(acc["optMV"], 2), nav=r(acc["nav"], 2),
            lmv=r(acc["lmv"], 2), init=r(acc["init"], 2), maint=r(acc["maint"], 2),
            avail=r(acc["avail"], 2), excess=r(acc["excess"], 2), hold=r(hold_nav, 2), event=event))

    # ---- week summaries (NAV at entry / expiry) -----------------------------------
    by_ts = {row["t"]: row for row in ledger}
    for w in weeks:
        if w["entry_ts"] is None:
            week_out[w["i"]] = dict(week=w["i"], expiry=w["expiry"].isoformat(), entry_t=None, entry_lbl=None,
                                    spot=None, strike=None, bid=None, ask=None, mid=None, outcome="SKIP",
                                    skip=w["skip"], S_T=None, nav_start=None, nav_end=None)
            skips.append(dict(week=w["i"], t=None, lbl=None, reason=w["skip"], holding=False))
            continue
        row = week_out.setdefault(w["i"], dict(week=w["i"], expiry=w["expiry"].isoformat(), entry_t=None, entry_lbl=None,
                                               spot=None, strike=None, bid=None, ask=None, mid=None, outcome="SKIP",
                                               skip="entry bar not reached", S_T=None, nav_start=None, nav_end=None))
        if w["entry_ts"] is not None and iso(w["entry_ts"]) in by_ts:
            row["nav_start"] = by_ts[iso(w["entry_ts"])]["nav"]
        if w["expiry_ts"] is not None and iso(w["expiry_ts"]) in by_ts:
            row["nav_end"] = by_ts[iso(w["expiry_ts"])]["nav"]
            if row["outcome"] is None:
                row["outcome"] = "SKIP"
            if row["outcome"] == "SKIP" and row["S_T"] is None:
                row["S_T"] = by_ts[iso(w["expiry_ts"])]["S"]
    week_list = [week_out[w["i"]] for w in weeks]

    # ---- scatter: option TRDPRC_1 (y) vs mid (x) -----------------------------------
    pts = []
    for F, chain in chains.items():
        for c in chain:
            for ts, q in c["q"].items():
                if ts < first_ts or ts > last_ts:
                    continue
                b, a, trd = q[0], q[1], q[2]
                if b == b and a == a and a >= b >= 0 and a > 0 and trd == trd and trd > 0:
                    Sx = S_print.get(ts, np.nan)
                    pts.append([r((b + a) / 2), r(trd), c["ric"], lbl(ts), c["strike"],
                                r(Sx, 2) if Sx == Sx else None, r(b), r(a)])
    pts.sort(key=lambda p: (p[2], p[3]))
    fit = None
    if len(pts) >= 3:
        x = np.array([p[0] for p in pts]); y = np.array([p[1] for p in pts])
        b1, b0 = np.polyfit(x, y, 1)
        ss_res = float(((y - (b0 + b1 * x)) ** 2).sum()); ss_tot = float(((y - y.mean()) ** 2).sum())
        fit = dict(n=len(pts), slope=r(b1, 5), intercept=r(b0, 5), r2=r(1 - ss_res / ss_tot, 5) if ss_tot else None)

    # ---- data coverage per week ------------------------------------------------------
    coverage = []
    for w in weeks:
        chain = chains.get(w["expiry"], [])
        bars = sum(c["n"] for c in chain)
        qbars = sum(1 for c in chain for q in c["q"].values() if valid_quote(q))
        tbars = sum(1 for c in chain for q in c["q"].values() if q[2] == q[2])
        coverage.append(dict(week=w["i"], expiry=w["expiry"].isoformat(), contracts=len(chain), bars=bars,
                             quoted=qbars, traded=tbars,
                             strikes=[chain[0]["strike"], chain[-1]["strike"]] if chain else None))

    result = dict(otm_pct=otm_pct, stock=stock, chains=chains, weeks=weeks, ledger=ledger, blotter=blotter, week_list=week_list,
                  skips=skips, pts=pts, fit=fit, coverage=coverage, tz_note=tz_note, S_print=S_print,
                  first_ts=first_ts, last_ts=last_ts)
    return result


# ================================== checks ====================================
def T(s):
    return pd.Timestamp(s).tz_convert(ET)


def run_checks(res, otm_pct=0.0):
    ledger, blotter, chains, S_print = res["ledger"], res["blotter"], res["chains"], res["S_print"]
    out = []

    def add(name, ok, detail):
        out.append(dict(name=name, ok=bool(ok), detail=detail))

    sells = [b for b in blotter if b["side"] == "SELL"]
    # 1. fills at mid and inside the market, quotes exist in the raw data at that exact bar
    bad = []
    for b in sells:
        F = dt.date.fromisoformat(next(w["expiry"] for w in res["week_list"] if w["week"] == b["week"]))
        ts = T(b["t"])
        c = next(c for c in chains[F] if c["ric"] == b["ric"])
        q = c["q"].get(ts)
        if not valid_quote(q) or abs(b["fill"] - (q[0] + q[1]) / 2) > 1e-4 or not (q[0] <= b["fill"] <= q[1]) \
                or b["limit"] != b["fill"]:
            bad.append(b["n"])
    add("Every short call filled at mid, inside the market, on a real bid/ask at that timestamp",
        not bad, f"{len(sells)} calls sold" + (f"; bad blotter rows {bad}" if bad else ""))

    # 2. strike rule recomputed from raw data
    bad = []
    for b in sells:
        F = dt.date.fromisoformat(next(w["expiry"] for w in res["week_list"] if w["week"] == b["week"]))
        ts = T(b["t"])
        S = round(float(S_print.get(ts)), 2)
        target = round(float(S_print.get(ts)) * (1 + otm_pct), 2)
        k = min((c["strike"] for c in chains[F] if c["strike"] >= target - 1e-9), default=None)
        strike_here = parse_call_ric(b["ric"])["strike"]
        if k is None or abs(k - strike_here) > 1e-9 or strike_here < S - 1e-9:
            bad.append(b["n"])
    add(("Strike = nearest listed strike at or above spot (never in the money when written)" if otm_pct == 0 else
         f"Strike = first listed strike at or above spot x {1 + otm_pct:.2f} (never in the money when written)"),
        not bad, f"recomputed independently for {len(sells)} writes" + (f"; mismatches {bad}" if bad else ""))

    # 3. expiry is that week's Friday, after entry
    bad = [b["n"] for b in sells
           if parse_call_ric(b["ric"])["expiry"].weekday() != 4 or T(b["t"]).date() > parse_call_ric(b["ric"])["expiry"]]
    add("Every call expires on a Friday on/after the day it was written", not bad,
        "ok" if not bad else f"bad rows {bad}")

    # 4. shares, covered, one call at a time
    bad_sh = [r_["t"] for r_ in ledger if r_["sh"] not in (0, LOT)]
    bad_cov = [r_["t"] for r_ in ledger if r_["call"] and r_["sh"] != LOT]
    open_calls, overlap = 0, False
    for b in blotter:
        if b["side"] == "SELL":
            open_calls += 1
        elif b["side"] in ("EXPIRE", "ASSIGN"):
            open_calls -= 1
        overlap |= open_calls > 1 or open_calls < 0
    add("Never short a call without owning 100 shares; never more than one call open",
        not bad_sh and not bad_cov and not overlap,
        f"{len(ledger)} ledger rows checked")

    # 5. cash moves only on blotter events (rebuild the cash column from the blotter alone)
    cum, i, bad = START_CASH, 0, []
    bl_sorted = sorted(blotter, key=lambda b: b["n"])
    for row in ledger:
        while i < len(bl_sorted) and bl_sorted[i]["t"] <= row["t"]:
            cum += bl_sorted[i]["dcash"]; i += 1
        if abs(cum - row["cash"]) > 0.05:
            bad.append(row["t"])
    add("Cash column = starting cash + blotter cash deltas, nothing else", not bad,
        f"rebuilt at {len(ledger)} timestamps" + (f"; first mismatch {bad[0]}" if bad else ""))

    # 6. shares column rebuilt from the blotter
    sh, i, bad = 0, 0, []
    for row in ledger:
        while i < len(bl_sorted) and bl_sorted[i]["t"] <= row["t"]:
            sh += bl_sorted[i]["dshares"]; i += 1
        if sh != row["sh"]:
            bad.append(row["t"])
    add("Share count = BUYs minus assignments", not bad, "ok" if not bad else f"first mismatch {bad[0]}")

    # 7. NAV identity and Reg T arithmetic
    bad = []
    for row in ledger:
        nav = row["cash"] + row["stockMV"] + row["optMV"]
        if abs(nav - row["nav"]) > 0.02 or abs(row["init"] - REGT_INITIAL * row["lmv"]) > 0.02 \
                or abs(row["maint"] - REGT_MAINT * row["lmv"]) > 0.02 \
                or abs(row["avail"] - (row["nav"] - row["init"])) > 0.02 \
                or abs(row["excess"] - (row["nav"] - row["maint"])) > 0.02 or row["optMV"] > 1e-9:
            bad.append(row["t"])
    add("NAV = cash + stock MV + option MV (short call <= 0); Initial 50% and Maintenance 25% of stock LMV; "
        "Available = NAV - Initial; Excess = NAV - Maintenance", not bad,
        "ok at every timestamp" if not bad else f"first mismatch {bad[0]}")

    # 8. available funds at every trade
    neg = [b["n"] for b in blotter if b["side"] in ("BUY", "SELL") and b["avail"] < 0]
    min_av = min(row["avail"] for row in ledger)
    add("Available funds >= 0 when each trade was put on", not neg,
        f"lowest available funds over the whole run: ${min_av:,.0f}")

    # 9. settlement vs stock print
    bad = []
    for b in blotter:
        if b["side"] in ("EXPIRE", "ASSIGN"):
            ts = T(b["t"])
            S = float(S_print.ffill().get(ts))
            K = parse_call_ric(b["ric"])["strike"]
            if (b["side"] == "ASSIGN") != (S - K > ITM_EPS) or (b["side"] == "ASSIGN" and abs(b["dcash"] - LOT * K) > 0.01):
                bad.append(b["n"])
    add("Assigned if and only if stock > strike at the last Friday bar; assignment pays 100 x strike", not bad,
        "ok" if not bad else f"bad rows {bad}")
    return out


# ================================== output ====================================
def _max_drawdown(navs):
    peak, worst = navs[0], 0.0
    for v in navs:
        peak = max(peak, v)
        worst = min(worst, (v / peak - 1) * 100)
    return worst


def summarize(res, meta_in):
    L = res["ledger"]
    bl = res["blotter"]
    wk = res["week_list"]
    end, start = L[-1], L[0]
    sells = [b for b in bl if b["side"] == "SELL"]
    prem = sum(b["dcash"] for b in sells)
    return dict(
        weeks=len(wk),
        traded=sum(1 for w in wk if w["outcome"] in ("EXPIRE", "ASSIGN")),
        expired=sum(1 for w in wk if w["outcome"] == "EXPIRE"),
        assigned=sum(1 for w in wk if w["outcome"] == "ASSIGN"),
        skipped=sum(1 for w in wk if w["outcome"] == "SKIP"),
        premium=r(prem, 2),
        startNav=r(start["nav"], 2), endNav=r(end["nav"], 2),
        ret=r((end["nav"] / START_CASH - 1) * 100, 3), holdRet=r((end["hold"] / START_CASH - 1) * 100, 3),
        minAvail=r(min(x["avail"] for x in L), 2),
        endShares=end["sh"], endCash=end["cash"],
        avgOtm=r(sum((w["strike"] / w["spot"] - 1) * 100 for w in wk if w["strike"] and w["spot"] and w["mid"] is not None)
                 / max(1, sum(1 for w in wk if w["strike"] and w["spot"] and w["mid"] is not None)), 3),
        avgPrem=r(prem / max(1, len(sells)), 2),
        maxDD=r(_max_drawdown([x["nav"] for x in L]), 3),
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true", help="synthetic data (testing only)")
    args = ap.parse_args()

    payload = load_payload(args.demo)
    meta_in = payload["meta"]
    res = run(payload, 0.0)                 # the assignment baseline: nearest strike at/above spot
    checks = run_checks(res, 0.0)
    summ = summarize(res, meta_in)
    vres = run(payload, VARIANT_OTM)        # my version: first strike at/above spot x 1.03
    vchecks = run_checks(vres, VARIANT_OTM)
    for c in vchecks:
        c["name"] = f"[my version] {c['name']}"
    vsumm = summarize(vres, meta_in)

    book = dict(
        meta=dict(ticker=meta_in.get("root", "AAPL"), stockRic=meta_in.get("stock_ric"), interval=meta_in.get("interval"),
                  source=meta_in.get("source", "LSEG"), demo=bool(meta_in.get("demo")), fetchedAt=meta_in.get("fetched_at"),
                  tz=res["tz_note"], first=iso(res["first_ts"]), last=iso(res["last_ts"]),
                  built=dt.datetime.now().strftime("%Y-%m-%d %H:%M")),
        config=dict(startCash=START_CASH, lot=LOT, entry="10:00 ET (first bar at or after)",
                    regtInitial=REGT_INITIAL, regtMaint=REGT_MAINT),
        summary=summ, weeks=res["week_list"], blotter=res["blotter"], ledger=res["ledger"],
        skips=res["skips"], scatter=dict(fields=["mid", "trd", "ric", "t", "k", "S", "bid", "ask"], rows=res["pts"], fit=res["fit"]),
        coverage=res["coverage"], checks=checks,
        variant=dict(label=f"{VARIANT_OTM * 100:g}% OTM", otmPct=VARIANT_OTM, summary=vsumm, weeks=vres["week_list"],
                     blotter=vres["blotter"], ledger=vres["ledger"], skips=vres["skips"], checks=vchecks),
    )
    with open(OUT_JS, "w") as f:
        f.write("// generated by build_book.py - do not edit by hand\n")
        f.write("window.BOOK = " + json.dumps(book, separators=(",", ":"), allow_nan=False) + ";\n")

    print(f"tz: {res['tz_note']}")
    print(f"window: {res['first_ts']:%Y-%m-%d %H:%M} .. {res['last_ts']:%Y-%m-%d %H:%M} ET, {len(res['ledger'])} bars")
    print(f"weeks: {summ['weeks']}  expired {summ['expired']}  assigned {summ['assigned']}  skipped {summ['skipped']}")
    print(f"blotter rows: {len(res['blotter'])}   premium collected ${summ['premium']:,.2f}")
    print(f"NAV {summ['startNav']:,.2f} -> {summ['endNav']:,.2f}  ({summ['ret']:+.2f}%)   buy&hold {summ['holdRet']:+.2f}%")
    if res["fit"]:
        print(f"scatter: n={res['fit']['n']}  trade = {res['fit']['intercept']} + {res['fit']['slope']} x mid   R^2 = {res['fit']['r2']}")
    for s in res["skips"]:
        print(f"  skipped week {s['week']}: {s['reason']}")
    print(f"\nMY VERSION ({VARIANT_OTM * 100:g}% OTM): expired {vsumm['expired']}  assigned {vsumm['assigned']}  "
          f"skipped {vsumm['skipped']}  premium ${vsumm['premium']:,.2f}  avg strike {vsumm['avgOtm']:+.2f}% from spot")
    print(f"  NAV {vsumm['startNav']:,.2f} -> {vsumm['endNav']:,.2f}  ({vsumm['ret']:+.2f}%)   max drawdown {vsumm['maxDD']:.2f}%"
          f"   (baseline {summ['maxDD']:.2f}%)")
    for s in vres["skips"]:
        print(f"  skipped week {s['week']}: {s['reason']}")
    print()
    checks = checks + vchecks
    bad = [c for c in checks if not c["ok"]]
    for c in checks:
        print(("  PASS  " if c["ok"] else "  FAIL  ") + c["name"])
    print(f"wrote {OUT_JS}  ({os.path.getsize(OUT_JS) / 1024:.0f} KB)")
    if args.demo:
        print("\n*** DEMO DATA: synthetic. Do not turn this in. Run fetch_data.py + build_book.py for the real thing. ***")
    if bad:
        sys.exit(1)


if __name__ == "__main__":
    main()
