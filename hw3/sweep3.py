"""
Offline parameter sweep for the wheel strategy, run entirely against already-fetched data
(raw_data3.pkl for stock history, raw_wide3.pkl for the wide option-strike cache) - no new LSEG
queries. For each (put_scale, call_scale, early_close_mult) combination, replays the FULL
stateful walk-forward decision logic (same vol/regime/session-filtering rules fetch_data3.py now
uses, verified against build_book3.py's independent recompute) and scores the result on
risk-adjusted performance: a Sharpe-style ratio of weekly NAV log-returns (annualized with 52
periods/year, same RISK_FREE assumption build_book3.py uses) and max drawdown.

put_scale/call_scale multiply ALL of strategy.py's PUT_OTM / CALL_OTM tiers uniformly, preserving
their relative shape (calm still farther OTM than elevated) while testing how far OTM to sit
overall. early_close_mult replaces strategy.EARLY_CLOSE_MULT.

A combo whose walk needs a strike outside the wide-fetch band (or a side/week with no cached
quote) skips that week exactly like fetch_data3.py does on a live "nothing resolved" week - state
carries forward unchanged, NAV carries forward flat. Combos that end up skipping too many weeks
are flagged, not silently ranked as if they were fully evaluated.

    python3 sweep3.py
"""
import datetime as dt
import math
import pickle

import pandas as pd

import fetch_data3 as fd
from rics import parse_option_ric, candidate_strikes
from strategy import (daily_closes, trailing_realized_vol, classify_regime, early_close_trigger,
                       VOL_LOOKBACK_DAYS, PUT_OTM as BASE_PUT_OTM, CALL_OTM as BASE_CALL_OTM)
from build_book3 import RISK_FREE, _max_drawdown

MIN_WEEKS_OK = 15     # a combo resolving fewer than this many of the 22 weeks is flagged, not ranked
LOT = 100             # shares per contract
PUT_SEARCH = (0.01, 0.16)    # strike-search band (OTM%) - must stay inside fetch_wide3.py's fetched band
CALL_SEARCH = (0.01, 0.14)

PUT_SCALES = [0.5, 0.75, 1.0, 1.5, 2.0]
CALL_SCALES = [0.5, 0.75, 1.0, 1.5, 2.0]
EARLY_MULTS = [2.0, 2.5, 3.0, 4.0, 5.0]   # free to sweep - no new data needed for this one
START_CASH = 11150    # fixed across every combo, matching the baseline real run, so NAV scale and
                       # Sharpe/return are comparable across combos rather than each self-sizing.


def load():
    with open("raw_data3.pkl", "rb") as f:
        base = pickle.load(f)
    try:
        with open("raw_wide3.pkl", "rb") as f:
            wide = pickle.load(f)
    except FileNotFoundError:
        raise SystemExit("raw_wide3.pkl not found - run fetch_wide3.py first.")
    options = dict(wide["options"])
    options.update(base["options"])  # baseline contracts take precedence if both cached it
    return base, options


def build_index(options, root):
    """(expiry_iso, side, strike) -> ric, for every cached contract that parses as this root."""
    idx = {}
    for ric in options:
        p = parse_option_ric(ric)
        if not p or p["root"] != root:
            continue
        idx[(p["expiry"].isoformat(), p["side"], p["strike"])] = ric
    return idx


def nearest_cached(idx, options, F_iso, side, cands):
    """cands already ordered best-first (closest to target). Returns (ric, K, df) or None."""
    for K in cands:
        ric = idx.get((F_iso, side, K))
        if ric is not None:
            return ric, K, options[ric]
    return None


def weekly_sharpe(navs):
    rets = [math.log(navs[i] / navs[i - 1]) for i in range(1, len(navs)) if navs[i - 1] > 0 and navs[i] > 0]
    if len(rets) < 5:
        return None, None
    n = len(rets)
    mean = sum(rets) / n
    var = sum((x - mean) ** 2 for x in rets) / (n - 1)
    vol_ann = math.sqrt(var) * math.sqrt(52)
    ret_ann = mean * 52
    sharpe = (ret_ann - RISK_FREE) / vol_ann if vol_ann else None
    return vol_ann, sharpe


def run_combo(put_scale, call_scale, early_mult, expiries, closes, stock_et_session, idx, options,
              session_only):
    put_otm = {k: v * put_scale for k, v in BASE_PUT_OTM.items()}
    call_otm = {k: v * call_scale for k, v in BASE_CALL_OTM.items()}

    state, cost_basis = "FLAT", None
    cash, shares = START_CASH, 0
    vol_history = []
    navs = [float(cash)]
    n_resolved = 0

    for F in expiries:
        mon = F - dt.timedelta(days=4)
        F_iso = F.isoformat()
        spot, _ = fd.monday_spot(stock_et_session, mon)
        if spot is None:
            vol_history.append(None)
            navs.append(navs[-1])  # no Monday bar - nothing to decide, NAV carries forward flat
            continue

        vol_today = trailing_realized_vol(closes, mon, lookback=VOL_LOOKBACK_DAYS)
        regime = classify_regime(vol_today, vol_history)
        vol_history.append(vol_today)

        if state == "FLAT":
            side = "P"
            target = spot * (1 - put_otm[regime])
            lo, hi = spot * (1 - PUT_SEARCH[1]), spot * (1 - PUT_SEARCH[0])
            cands = sorted([k for k in candidate_strikes(lo, hi) if k <= target], key=lambda k: target - k)
        else:
            side = "C"
            tier = "above_cost_basis" if spot >= cost_basis else "below_cost_basis"
            target = spot * (1 + call_otm[tier])
            lo, hi = spot * (1 + CALL_SEARCH[0]), spot * (1 + CALL_SEARCH[1])
            cands = sorted([k for k in candidate_strikes(lo, hi) if k >= target], key=lambda k: k - target)

        found = nearest_cached(idx, options, F_iso, side, cands)
        if found is None:
            navs.append(navs[-1])  # nothing tradable this week under this combo - NAV carries forward
            continue

        ric, K, df = found
        n_resolved += 1
        df_session = session_only(df)
        entry_target = pd.Timestamp(mon.year, mon.month, mon.day, fd.ENTRY_HOUR, tz=fd.ET)
        entry_ts, entry_premium = fd.entry_fill(df_session, entry_target)
        early = early_close_trigger(df_session, entry_premium, entry_ts, mult=early_mult) \
            if entry_ts is not None else None

        if entry_premium is None:
            navs.append(navs[-1])
            continue

        friday_rows = stock_et_session[stock_et_session.index.date == F]
        friday_spot = float(friday_rows["TRDPRC_1"].dropna().iloc[-1]) if len(friday_rows) else None

        cash += entry_premium * LOT  # sell the option now (cash-secured puts: collateral implicit)
        if early is not None:
            cash -= early[1] * LOT
        elif friday_spot is not None:
            if side == "P" and friday_spot < K:
                cash -= K * LOT
                shares, state, cost_basis = LOT, "HOLDING", K
            elif side == "C" and friday_spot > K:
                cash += K * LOT
                shares, state, cost_basis = 0, "FLAT", None
            # else EXPIRE - nothing else changes

        mark_spot = friday_spot if friday_spot is not None else spot
        navs.append(cash + shares * mark_spot)

    return navs, n_resolved


def main():
    base, options = load()
    stock = base["stock"]
    session_only = fd.session_only_factory(stock.index)
    stock_et_session = session_only(stock)
    closes = daily_closes(stock_et_session)
    expiries = fd.last_fridays(fd.TRADE_WEEKS)
    idx = build_index(options, fd.ROOT)
    print(f"{len(options)} cached contracts, {len(idx)} parsed and indexed for {fd.ROOT}.\n")

    results = []
    for ps in PUT_SCALES:
        for cs in CALL_SCALES:
            for em in EARLY_MULTS:
                navs, n_resolved = run_combo(ps, cs, em, expiries, closes, stock_et_session, idx,
                                              options, session_only)
                ret = (navs[-1] / navs[0] - 1) * 100
                vol_ann, sharpe = weekly_sharpe(navs)
                dd = _max_drawdown(navs)
                results.append(dict(put_scale=ps, call_scale=cs, early_mult=em, n_resolved=n_resolved,
                                     ret=ret, vol=vol_ann, sharpe=sharpe, dd=dd))

    ok = [r for r in results if r["n_resolved"] >= MIN_WEEKS_OK and r["sharpe"] is not None]
    n_flagged = len(results) - len(ok)
    ok.sort(key=lambda r: r["sharpe"], reverse=True)

    print(f"{len(ok)}/{len(results)} combos resolved >= {MIN_WEEKS_OK}/22 weeks and are ranked below "
          f"({n_flagged} flagged - too many weeks fell outside the fetched strike band or had no quote).\n")
    print(f"{'put_scale':>9} {'call_scale':>10} {'early_mult':>10} {'weeks':>6} {'return%':>8} "
          f"{'vol_ann%':>9} {'sharpe':>7} {'maxDD%':>7}")
    for r in ok[:15]:
        vol_pct = r["vol"] * 100 if r["vol"] else float("nan")
        print(f"{r['put_scale']:>9.2f} {r['call_scale']:>10.2f} {r['early_mult']:>10.1f} "
              f"{r['n_resolved']:>6} {r['ret']:>8.2f} {vol_pct:>9.2f} {r['sharpe']:>7.3f} {r['dd']:>7.2f}")

    base_combo = next((r for r in results if r["put_scale"] == 1.0 and r["call_scale"] == 1.0
                        and r["early_mult"] == 3.0), None)
    if base_combo is not None:
        in_ok = base_combo in ok
        rank = ok.index(base_combo) + 1 if in_ok else None
        vol_pct = base_combo["vol"] * 100 if base_combo["vol"] else float("nan")
        tag = f"(rank {rank} of {len(ok)})" if in_ok else "(flagged / not ranked)"
        print(f"\nBaseline (1.0, 1.0, 3.0x): weeks={base_combo['n_resolved']} ret={base_combo['ret']:.2f}% "
              f"vol={vol_pct:.2f}% sharpe={base_combo['sharpe']} maxDD={base_combo['dd']:.2f}%  {tag}")


if __name__ == "__main__":
    main()
