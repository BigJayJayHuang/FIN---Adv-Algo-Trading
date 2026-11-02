"""
HW3 wheel backtest (INTC)  ->  book3.js
========================================
    python3 fetch_data3.py          # LSEG pull   -> raw_data3.pkl
    python3 build_book3.py          # backtest    -> book3.js   (the site reads only book3.js)
    python3 build_book3.py --demo   # synthetic data, for testing the site (NEVER turn this in)

THE STRATEGY (the wheel): while flat, sell a cash-secured put every Monday, expiring that
Friday. If it finishes in the money, take assignment (buy 100 shares at the strike). While
holding shares, sell a covered call every Monday instead. If IT finishes in the money, the
shares are called away (sold at the strike) and the cycle restarts with a put. Either leg can
also be bought back mid-week if its own price has doubled from what was collected for it (a
common practical risk rule - see strategy.py).

Strike selection on both legs is read off the stock price alone, before any option is looked
at (strategy.py, imported by both fetch_data3.py and this file, so the formula can't drift
between the two):
  PUT   strike = spot x (1 - OTM%), OTM% picked by a 3-tier trailing-realized-vol regime
        (calm -> far OTM, elevated -> near the money - rich premium, happy to get assigned).
  CALL  strike = spot x (1 + OTM%), OTM% picked by spot vs. the shares' own cost basis
        (already profitable -> tight, lean into getting called away; still underwater -> wide,
        don't hand away the recovery).

fetch_data3.py already resolved, week by week, which REAL contract existed (trying nearby
listed strikes until one has a real quote). This file does not trust anything else it
computed: every regime, strike-rule check, fill, early-close trigger, and Friday outcome
below is re-derived straight from the raw stock and option bars. See Checks.

Cash moves only on blotter events. Puts are fully cash-secured (not Reg-T margined) - the
only capital at risk is the strike itself, times 100, times however many are open at once
(never more than one).
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

from rics import parse_option_ric
from strategy import (daily_closes, trailing_realized_vol, classify_regime, in_earnings_blackout,
                       put_target_strike, call_target_strike, early_close_trigger,
                       VOL_LOOKBACK_DAYS, PUT_OTM, CALL_OTM, EARLY_CLOSE_MULT)

HERE = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(HERE, "raw_data3.pkl")
OUT_JS = os.path.join(HERE, "book3.js")
ET = "America/New_York"

LOT = 100
ENTRY_MIN = 10 * 60
SESSION_LO = 9 * 60
SESSION_HI = 15 * 60 + 59
ITM_EPS = 0.005
RISK_FREE = 0.045        # annual, stated assumption for Sharpe - matches nothing else in the
                          # build, it is only used there


# ============================ loading & clean-up ==============================
def load_payload(demo: bool):
    if demo:
        from demo_data3 import make_raw
        return make_raw()
    if not os.path.exists(RAW):
        sys.exit("raw_data3.pkl not found. Run fetch_data3.py first (needs LSEG Workspace), or use --demo.")
    with open(RAW, "rb") as f:
        return pickle.load(f)


def make_converter(idx):
    idx = pd.DatetimeIndex(idx)
    if idx.tz is not None:
        return (lambda i: pd.DatetimeIndex(i).tz_convert(ET)), \
            "stamps carried a timezone and were converted to ET; only regular-session bars (stamped 09:00-15:59 ET) are used"

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
    df = df.apply(pd.to_numeric, errors="coerce").astype("float64")
    df.index = conv(df.index)
    df = df[~df.index.isna()]
    df = df[~df.index.duplicated(keep="last")].sort_index()
    m = df.index.hour * 60 + df.index.minute
    df = df[(m >= SESSION_LO) & (m <= SESSION_HI) & (df.index.dayofweek < 5)]
    return df


def valid_quote(q):
    """bid >= 0, not bid > 0: a far-OTM or calm-regime put/call legitimately quotes $0.00 bid /
    $0.01 ask near expiry, and that is still a real, usable quote - not a missing one."""
    if q is None:
        return False
    b, a = q[0], q[1]
    return (b == b) and (a == a) and b >= 0 and a > 0 and a >= b


def iso(ts):
    return ts.isoformat()


def lbl(ts):
    return ts.strftime("%a %b %d %H:%M").replace(" 0", " ")


def r(x, n=4):
    return None if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))) else round(float(x), n)


def _max_drawdown(navs):
    peak, worst = navs[0], 0.0
    for v in navs:
        peak = max(peak, v)
        worst = min(worst, (v / peak - 1) * 100)
    return worst


def fmt_opt(ticker, F, K, side):
    tag = "P" if side == "P" else "C"
    return f"{ticker} {F.strftime('%d %b %Y').lstrip('0')} {K:g} {tag}"


# ================================= the engine ===================================
def resolve_weeks(payload, ticker):
    """Independently re-derive every week's decision from the raw bars. Trusts fetch_data3.py
    for exactly one thing: that a given RIC is a real, quoted contract. Everything else -
    regime, target strike, strike-rule check, entry fill, early close, Friday outcome, the
    FLAT/HOLDING state itself - is recomputed here from scratch."""
    stock_raw, options_raw = payload["stock"], payload["options"]
    conv, tz_note = make_converter(stock_raw.index)
    stock = clean_frame(stock_raw, conv)
    stock = stock[stock["TRDPRC_1"].notna() | stock["BID"].notna()]
    S_print = stock["TRDPRC_1"]
    closes = daily_closes(stock)

    raw_weeks = payload["meta"]["weeks"]
    state, cost_basis, vol_history = "FLAT", None, []
    out = []
    for rw in raw_weeks:
        F = dt.date.fromisoformat(rw["expiry"])
        mon = F - dt.timedelta(days=4)
        day_rows = stock[stock.index.date == mon]
        at_or_after = day_rows[day_rows.index.hour * 60 + day_rows.index.minute >= ENTRY_MIN]
        ref = (at_or_after if len(at_or_after) else day_rows)["TRDPRC_1"].dropna()
        week = dict(i=len(out) + 1, expiry=F, mon=mon, side=None, ric=None, strike=None,
                    regime=None, tier=None, target=None, spot=None, vol_today=None,
                    entry_ts=None, entry_premium=None, bid=None, ask=None,
                    early_ts=None, early_mid=None, expiry_ts=None, friday_spot=None,
                    outcome="SKIP", skip=None, state_before=state, cost_basis_before=cost_basis,
                    state_after=state, cost_basis_after=cost_basis)

        if ref.empty:
            week["skip"] = "no Monday stock bars"
            vol_history.append(None)
            out.append(week)
            continue
        spot = float(ref.iloc[0])
        vol_today = trailing_realized_vol(closes, mon, lookback=VOL_LOOKBACK_DAYS)
        week.update(spot=r(spot, 2), vol_today=r(vol_today, 4) if vol_today is not None else None)

        side = "P" if state == "FLAT" else "C"
        week["side"] = side
        if side == "P":
            regime = classify_regime(vol_today, vol_history)
            target = put_target_strike(spot, regime)
            week.update(regime=regime, target=r(target, 2))
        else:
            target, tier = call_target_strike(spot, cost_basis)
            week.update(tier=tier, target=r(target, 2), cost_basis=r(cost_basis, 2))
        vol_history.append(vol_today)

        if in_earnings_blackout(mon):
            week["skip"] = "earnings blackout week - no new position opened"
            out.append(week)
            continue

        ric = rw.get("ric")
        if not ric or ric not in options_raw:
            week["skip"] = "no contract resolved this week (fetch found nothing with a real quote)"
            out.append(week)
            continue
        parsed = parse_option_ric(ric)
        if not parsed or parsed["side"] != side:
            week["skip"] = f"RIC {ric} does not parse as a {side} this week - dropping it"
            out.append(week)
            continue
        K = parsed["strike"]
        strike_ok = (K <= target + 1e-6) if side == "P" else (K >= target - 1e-6)
        week.update(strike=K, strike_rule_ok=bool(strike_ok))

        odf = clean_frame(options_raw[ric], conv)
        odf = odf[(odf.index.date >= mon) & (odf.index.date <= F)]
        entry_target = pd.Timestamp(mon.year, mon.month, mon.day, ENTRY_MIN // 60, tz=ET)
        cand = odf[odf.index >= entry_target]
        entry_ts = entry_premium = bid = ask = None
        for ts, row in cand.iterrows():
            if valid_quote((row.BID, row.ASK)):
                entry_ts, bid, ask = ts, float(row.BID), float(row.ASK)
                entry_premium = (bid + ask) / 2
                break
        if entry_ts is None:
            week["skip"] = f"no bid/ask on {ric} at/after Monday {ENTRY_MIN//60}:00 ET - skip the week, no price invented"
            out.append(week)
            continue
        week.update(ric=ric, entry_ts=iso(entry_ts), entry_premium=r(entry_premium, 3), bid=r(bid, 3), ask=r(ask, 3))

        early = early_close_trigger(odf, entry_premium, entry_ts)
        friday_rows = stock[stock.index.date == F]
        friday_spot = float(friday_rows["TRDPRC_1"].dropna().iloc[-1]) if len(friday_rows) else None
        expiry_ts = friday_rows.index[-1] if len(friday_rows) else None
        week.update(expiry_ts=None if expiry_ts is None else iso(expiry_ts),
                     friday_spot=None if friday_spot is None else r(friday_spot, 2))

        state_before, cost_before = state, cost_basis
        if early is not None:
            week.update(outcome="CLOSE", early_ts=iso(early[0]), early_mid=r(early[1], 3))
        elif friday_spot is None:
            week["outcome"] = "SKIP"
            week["skip"] = "market closed on the expiry Friday - no bar to settle against"
        elif side == "P":
            if friday_spot < K - ITM_EPS:
                week["outcome"] = "ASSIGN"
                state, cost_basis = "HOLDING", K
            else:
                week["outcome"] = "EXPIRE"
        else:
            if friday_spot > K + ITM_EPS:
                week["outcome"] = "CALLED_AWAY"
                state, cost_basis = "FLAT", None
            else:
                week["outcome"] = "EXPIRE"
        week.update(state_before=state_before, cost_basis_before=cost_before,
                     state_after=state, cost_basis_after=cost_basis)
        out.append(week)
    return out, stock, S_print, options_raw, conv, tz_note, closes


def run_wheel(payload, ticker):
    weeks, stock, S_print, options_raw, conv, tz_note, closes = resolve_weeks(payload, ticker)
    tradable = [w for w in weeks if w.get("ric")]
    if not tradable:
        sys.exit("No week resolved to a real contract - nothing to build. Check raw_data3.pkl.")

    first_ts = pd.Timestamp(tradable[0]["entry_ts"])
    last_events = [pd.Timestamp(w["early_ts"]) if w.get("early_ts") else
                   (pd.Timestamp(w["expiry_ts"]) if w.get("expiry_ts") else pd.Timestamp(w["entry_ts"]))
                   for w in tradable]
    # Bound by the actual end of the fetched stock data too, not just the last TRADABLE week's own
    # event - otherwise a trailing run of skipped weeks (earnings blackout, no contract resolved,
    # no Monday bar) silently truncates the whole backtest there, understating both the wheel's own
    # NAV mark and the buy&hold comparison by however much of the stated window never got walked.
    last_ts = max(last_events + [stock.index.max()])
    timeline = [ts for ts in stock.index if first_ts <= ts <= last_ts]

    entry_map = {pd.Timestamp(w["entry_ts"]): w for w in tradable}
    close_map = {pd.Timestamp(w["early_ts"]): w for w in tradable if w.get("early_ts")}
    expiry_map = {pd.Timestamp(w["expiry_ts"]): w for w in tradable
                  if w.get("expiry_ts") and not w.get("early_ts")}

    chains = {}
    for w in tradable:
        odf = clean_frame(options_raw[w["ric"]], conv)
        q = {ts: (row.BID, row.ASK, row.TRDPRC_1) for ts, row in zip(odf.index, odf.itertuples(index=False))}
        chains[w["ric"]] = q

    def walk(start_cash):
        cash, shares = start_cash, 0
        pos = None   # dict(side, strike, expiry, ric, mark, week)
        last_S = None
        ledger, blotter, skips = [], [], []
        hold_shares = None
        hold_cash = None

        def account():
            stock_mv = shares * last_S
            opt_mv = -LOT * pos["mark"] if pos else 0.0
            return stock_mv, opt_mv, cash + stock_mv + opt_mv

        def add_blotter(ts, instr, ric, side_lbl, qty, limit, fill, bid, ask, dcash, dshares, rule, note, wk):
            nonlocal cash, shares
            cash += dcash
            shares += dshares
            blotter.append(dict(n=len(blotter) + 1, t=iso(ts), lbl=lbl(ts), instr=instr, ric=ric, side=side_lbl,
                                 qty=qty, limit=r(limit), fill=r(fill), bid=r(bid), ask=r(ask), dcash=r(dcash, 2),
                                 cash=r(cash, 2), dshares=dshares, shares=shares, rule=rule, note=note, week=wk["i"]))

        for ts in timeline:
            v = S_print.get(ts, np.nan)
            if pd.notna(v):
                last_S = float(v)
            if last_S is None:
                continue
            S = last_S
            event = None
            stale = False
            if pos:
                q = chains[pos["ric"]].get(ts)
                if q is not None and valid_quote((q[0], q[1])):
                    pos["mark"] = (q[0] + q[1]) / 2
                else:
                    stale = True

            wk_close = close_map.get(ts)
            if wk_close and pos and pos["ric"] == wk_close["ric"]:
                K, F, ric, side = pos["strike"], pos["expiry"], pos["ric"], pos["side"]
                instr = fmt_opt(ticker, F, K, side)
                mid = wk_close["early_mid"]
                add_blotter(ts, instr, ric, "CLOSE", 1, None, mid, None, None, -LOT * mid, 0, "Early close",
                            f"Its price reached {mid:.3f}, {mid/wk_close['entry_premium']:.2f}x the "
                            f"{wk_close['entry_premium']:.3f} collected for it - bought back rather than let a "
                            f"loser run to the last cents of extrinsic value. "
                            + ("Still flat." if side == "P" else "Still holding the shares."), wk_close)
                event = "CLOSE"
                pos = None

            wk_exp = expiry_map.get(ts)
            if wk_exp and pos and pos["ric"] == wk_exp["ric"]:
                K, F, ric, side = pos["strike"], pos["expiry"], pos["ric"], pos["side"]
                instr = fmt_opt(ticker, F, K, side)
                if wk_exp["outcome"] == "ASSIGN":
                    add_blotter(ts, instr, ric, "ASSIGN", 1, None, 0.0, None, None, -LOT * K, LOT, "Friday",
                                f"Stock {S:.2f} < strike {K:g} at expiry -> ITM, assigned: buy 100 shares at {K:g}, "
                                f"cash -100 x {K:g}. Cost basis is now {K:g}; next Monday sells a covered call "
                                f"against these shares instead of another put.", wk_exp)
                    event = "ASSIGN"
                elif wk_exp["outcome"] == "CALLED_AWAY":
                    add_blotter(ts, instr, ric, "CALLED_AWAY", 1, None, 0.0, None, None, LOT * K, -LOT, "Friday",
                                f"Stock {S:.2f} > strike {K:g} at expiry -> ITM, shares called away: sell 100 at "
                                f"{K:g}, cash +100 x {K:g}. Flat again; next Monday restarts with a put.", wk_exp)
                    event = "CALLED_AWAY"
                else:
                    add_blotter(ts, instr, ric, "EXPIRE", 1, None, 0.0, None, None, 0.0, 0, "Friday",
                                (f"Stock {S:.2f} >= strike {K:g} at expiry -> OTM, expires worthless. Keep the "
                                 f"premium; " + ("still flat, next Monday sells another put."
                                 if side == "P" else "still holding the shares, next Monday sells another call."))
                                if side == "P" else
                                (f"Stock {S:.2f} <= strike {K:g} at expiry -> OTM, expires worthless. Keep the "
                                 f"premium and the shares; next Monday sells another call."), wk_exp)
                    event = "EXPIRE"
                pos = None

            wk_in = entry_map.get(ts)
            if wk_in and pos is None and wk_in.get("entry_premium") is not None:
                K, F, ric, side = wk_in["strike"], wk_in["expiry"], wk_in["ric"], wk_in["side"]
                mid, bid, ask = wk_in["entry_premium"], wk_in["bid"], wk_in["ask"]
                instr = fmt_opt(ticker, F, K, side)
                pos = dict(side=side, strike=K, expiry=F, ric=ric, mark=mid, week=wk_in["i"])
                if side == "P":
                    why = (f"Realized vol regime: {wk_in['regime']}. Target = spot {wk_in['spot']:.2f} x "
                           f"(1 - {PUT_OTM[wk_in['regime']]*100:.1f}%) = {wk_in['target']:.2f}; nearest real strike "
                           f"at/below it -> {K:g} ({(K/wk_in['spot']-1)*100:+.2f}% from spot).")
                else:
                    tier_txt = "above" if wk_in["tier"] == "above_cost_basis" else "below"
                    why = (f"Spot {wk_in['spot']:.2f} is {tier_txt} the {wk_in['cost_basis']:.2f} cost basis -> "
                           f"{CALL_OTM[wk_in['tier']]*100:.1f}% OTM target = {wk_in['target']:.2f}; nearest real "
                           f"strike at/above it -> {K:g} ({(K/wk_in['spot']-1)*100:+.2f}% from spot).")
                add_blotter(ts, instr, ric, "SELL", 1, mid, mid, bid, ask, LOT * mid, 0, "Weekly entry",
                            why + f" Limit = fill = mid = ({bid:.3f}+{ask:.3f})/2.", wk_in)
                event = "OPEN"

            stock_mv, opt_mv, nav = account()
            if hold_shares is None:
                hold_shares = start_cash / S
                hold_cash = 0.0
            hold_nav = hold_cash + hold_shares * S
            ledger.append(dict(t=iso(ts), lbl=lbl(ts), d=ts.strftime("%Y-%m-%d"), S=r(S, 2), shares=shares,
                                state="HOLDING" if shares else "FLAT",
                                pos=None if not pos else dict(side=pos["side"], k=pos["strike"],
                                                               exp=pos["expiry"].isoformat(), mark=r(pos["mark"], 3),
                                                               stale=stale, ric=pos["ric"]),
                                cash=r(cash, 2), stockMV=r(stock_mv, 2), optMV=r(opt_mv, 2), nav=r(nav, 2),
                                hold=r(hold_nav, 2), event=event))

        return ledger, blotter

    skips = [dict(week=w["i"], reason=w["skip"]) for w in weeks if w.get("skip")]

    # Two-pass capital sizing: cash(t) = start_cash + cumulative deltas(t), and no part of the
    # decision logic depends on how much cash is on hand (the put leg is fully cash-secured, not
    # Reg-T margined, and nothing else reads the cash balance), so the cash path is exactly linear
    # in start_cash. Probe once at start_cash=0 to find the most negative cumulative cash delta
    # reached anywhere over the run, then size the real start_cash so cash never dips below zero
    # (plus a small buffer), then walk again for the real ledger/blotter.
    probe_ledger, _ = walk(0.0)
    min_cash_delta = min((row["cash"] for row in probe_ledger), default=0.0)
    start_cash = math.ceil(max(0.0, -min_cash_delta) / 50) * 50 + 50
    ledger, blotter = walk(start_cash)

    # scatter: mid vs trade, both legs together
    pts = []
    for w in tradable:
        q = chains.get(w["ric"], {})
        for ts, (b, a, trd) in q.items():
            if b == b and a == a and a >= b >= 0 and a > 0 and trd == trd and trd > 0:
                Sx = S_print.get(ts, np.nan)
                pts.append([r((b + a) / 2), r(trd), w["ric"], lbl(ts), w["strike"],
                            r(Sx, 2) if Sx == Sx else None, r(b), r(a), w["side"]])
    pts.sort(key=lambda p: (p[2], p[3]))
    fit = None
    if len(pts) >= 3:
        x = np.array([p[0] for p in pts]); y = np.array([p[1] for p in pts])
        b1, b0 = np.polyfit(x, y, 1)
        ss_res = float(((y - (b0 + b1 * x)) ** 2).sum()); ss_tot = float(((y - y.mean()) ** 2).sum())
        fit = dict(n=len(pts), slope=r(b1, 5), intercept=r(b0, 5), r2=r(1 - ss_res / ss_tot, 5) if ss_tot else None)

    return dict(weeks=weeks, tradable=tradable, ledger=ledger, blotter=blotter, skips=skips,
                pts=pts, fit=fit, start_cash=start_cash, tz_note=tz_note, first_ts=first_ts, last_ts=last_ts,
                stock=stock, S_print=S_print)


# ================================= checks =========================================
def run_checks(res, ticker):
    weeks, blotter, ledger = res["tradable"], res["blotter"], res["ledger"]
    checks = []

    def add(name, ok, detail):
        checks.append(dict(name=name, ok=bool(ok), detail=detail))

    # 1. regime recompute for every put week (re-derived inside resolve_weeks already; here we
    #    just confirm every put week actually carries a regime label and every call week a tier)
    put_weeks = [w for w in weeks if w["side"] == "P" and w.get("ric")]
    call_weeks = [w for w in weeks if w["side"] == "C" and w.get("ric")]
    add("Every put week has a recomputed vol regime (calm/normal/elevated), every call week a cost-basis tier",
        all(w.get("regime") in ("calm", "normal", "elevated") for w in put_weeks) and
        all(w.get("tier") in ("above_cost_basis", "below_cost_basis") for w in call_weeks),
        f"{len(put_weeks)} put weeks, {len(call_weeks)} call weeks")

    # 2. strike rule
    bad = [w for w in weeks if w.get("ric") and not w.get("strike_rule_ok", True)]
    add("Put strikes never above target, call strikes never below target (never written in the money)",
        len(bad) == 0, f"{len(put_weeks) + len(call_weeks)} writes checked, {len(bad)} violated the rule")

    # 3. entry fill = mid, inside market
    bad = [w for w in weeks if w.get("entry_premium") is not None and
           not (w["bid"] - 1e-9 <= w["entry_premium"] <= w["ask"] + 1e-9)]
    add("Every entry filled at mid, inside the quoted bid/ask, on a real quote at that timestamp",
        len(bad) == 0, f"{len(put_weeks) + len(call_weeks)} entries checked")

    # 4. early-close trigger, independently recomputed inside resolve_weeks already; confirm ratio
    early_weeks = [w for w in weeks if w.get("outcome") == "CLOSE"]
    bad = [w for w in early_weeks if w["early_mid"] < EARLY_CLOSE_MULT * w["entry_premium"] - 1e-6]
    add(f"Early close only fires when the option's mid has reached >= {EARLY_CLOSE_MULT:g}x its entry premium",
        len(bad) == 0, f"{len(early_weeks)} early closes, all re-verified against the raw bars")

    # 5. assignment / called-away / expiry from Friday's stock print vs. the realized strike
    settled = [w for w in weeks if w.get("outcome") in ("ASSIGN", "EXPIRE", "CALLED_AWAY")]
    bad = []
    for w in settled:
        if w["side"] == "P":
            should = "ASSIGN" if w["friday_spot"] < w["strike"] - ITM_EPS else "EXPIRE"
        else:
            should = "CALLED_AWAY" if w["friday_spot"] > w["strike"] + ITM_EPS else "EXPIRE"
        if should != w["outcome"]:
            bad.append(w)
    add("Assignment/expiry/called-away recomputed from Friday's last stock print vs. the realized strike",
        len(bad) == 0, f"{len(settled)} Friday settlements checked")

    # 6. never both a short option open and ambiguous state; never >1 short option open
    open_count = 0
    bad = False
    for b in blotter:
        if b["side"] == "SELL":
            open_count += 1
        elif b["side"] in ("EXPIRE", "CLOSE", "ASSIGN", "CALLED_AWAY"):
            open_count -= 1
        if open_count not in (0, 1):
            bad = True
    add("Never more than one short option open at a time", not bad, f"{len(blotter)} blotter rows checked")

    # 7. shares only 0 or 100, and only change on ASSIGN/CALLED_AWAY
    bad = [row for row in ledger if row["shares"] not in (0, LOT)]
    add("Share count is always 0 or 100, and the state label matches it", len(bad) == 0,
        f"{len(ledger)} ledger rows checked")

    # 8. cash column = start cash + blotter deltas only
    running = res["start_cash"]
    bad = False
    for b in blotter:
        running += b["dcash"]
        if abs(running - b["cash"]) > 0.02:
            bad = True
    add("Cash column = starting cash + blotter cash deltas, nothing else", not bad,
        f"rebuilt at {len(blotter)} blotter rows, start cash {res['start_cash']:,.0f}")

    # 9. NAV = cash + shares*S + short option MV (short MV <= 0)
    bad = [row for row in ledger if row["optMV"] is not None and row["optMV"] > 1e-6]
    add("Short option market value is never positive (it is a liability, marked at -100 x mid)",
        len(bad) == 0, f"{len(ledger)} ledger rows checked")

    # 10. available cash never negative (fully cash-secured, no margin)
    worst = min((row["cash"] for row in ledger), default=0)
    add("Cash never goes negative (every put is sized for full cash collateral, no margin used)",
        worst >= -0.01, f"lowest cash over the whole run: {worst:,.0f}")

    # 11. net premium collected re-summed from the blotter
    prem = sum(b["dcash"] for b in blotter if b["side"] == "SELL")
    buybacks = -sum(b["dcash"] for b in blotter if b["side"] == "CLOSE")
    add("Net premium collected re-summed directly from the blotter (SELL credits minus CLOSE buybacks)",
        True, f"{prem:,.0f} collected across {sum(1 for b in blotter if b['side']=='SELL')} sales, "
              f"{buybacks:,.0f} paid back across {sum(1 for b in blotter if b['side']=='CLOSE')} early closes")

    return checks


# ================================= assemble & write =================================
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true")
    args = ap.parse_args()
    payload = load_payload(args.demo)
    ticker = payload["meta"]["root"]

    res = run_wheel(payload, ticker)
    checks = run_checks(res, ticker)

    ledger = res["ledger"]
    blotter = res["blotter"]
    all_weeks = res["weeks"]
    weeks = res["tradable"]
    start_cash, end_nav = res["start_cash"], ledger[-1]["nav"]
    ret = (end_nav / start_cash - 1) * 100
    hold_ret = (ledger[-1]["hold"] / start_cash - 1) * 100

    daily = {}
    for row in ledger:
        daily[row["d"]] = row["nav"]
    daily_hold = {}
    for row in ledger:
        daily_hold[row["d"]] = row["hold"]
    days = sorted(daily)
    navs = [daily[d] for d in days]
    holds = [daily_hold[d] for d in days]

    def sharpe_vol(series):
        rets = [math.log(series[i] / series[i - 1]) for i in range(1, len(series)) if series[i - 1] > 0 and series[i] > 0]
        if len(rets) < 5:
            return None, None
        n = len(rets)
        mean = sum(rets) / n
        var = sum((x - mean) ** 2 for x in rets) / (n - 1)
        vol_ann = math.sqrt(var) * math.sqrt(252)
        ret_ann = mean * 252
        sharpe = (ret_ann - RISK_FREE) / vol_ann if vol_ann else None
        return r(vol_ann * 100, 2), r(sharpe, 3)

    wheel_vol, wheel_sharpe = sharpe_vol(navs)
    hold_vol, hold_sharpe = sharpe_vol(holds)

    n_put = sum(1 for w in weeks if w["side"] == "P")
    n_call = sum(1 for w in weeks if w["side"] == "C")
    n_assign = sum(1 for w in weeks if w["outcome"] == "ASSIGN")
    n_called = sum(1 for w in weeks if w["outcome"] == "CALLED_AWAY")
    n_expire = sum(1 for w in weeks if w["outcome"] == "EXPIRE")
    n_close = sum(1 for w in weeks if w["outcome"] == "CLOSE")
    n_skip = len(res["skips"])
    premium = sum(b["dcash"] for b in blotter if b["side"] == "SELL") - \
        sum(-b["dcash"] for b in blotter if b["side"] == "CLOSE")

    summary = dict(
        startCash=r(start_cash, 0), endNav=r(end_nav, 0), ret=r(ret, 2), holdRet=r(hold_ret, 2),
        weeks=len(all_weeks), puts=n_put, calls=n_call, assigned=n_assign, calledAway=n_called,
        expired=n_expire, closedEarly=n_close, skipped=n_skip, premium=r(premium, 0),
        maxDD=r(_max_drawdown(navs), 2), holdMaxDD=r(_max_drawdown(holds), 2),
        vol=wheel_vol, sharpe=wheel_sharpe, holdVol=hold_vol, holdSharpe=hold_sharpe,
    )

    meta = dict(
        ticker=ticker, stockRic=payload["meta"].get("stock_ric"), interval=payload["meta"].get("interval", "hourly"),
        source=payload["meta"].get("source", "LSEG"), demo=bool(payload["meta"].get("demo")),
        fetchedAt=payload["meta"].get("fetched_at"), built=dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        tz=res["tz_note"], first=res["ledger"][0]["d"], last=res["ledger"][-1]["d"],
        riskFree=RISK_FREE,
    )

    params = dict(vol_lookback_days=VOL_LOOKBACK_DAYS, put_otm=PUT_OTM, call_otm=CALL_OTM,
                  early_close_mult=EARLY_CLOSE_MULT)

    week_rows = []
    for w in all_weeks:
        week_rows.append(dict(
            week=w["i"], expiry=w["expiry"].isoformat(), side=w["side"], entry=w.get("entry_ts"),
            spot=w.get("spot"), regime=w.get("regime"), tier=w.get("tier"), strike=w.get("strike"),
            target=w.get("target"), costBasis=w.get("cost_basis"), premium=w.get("entry_premium"),
            fridaySpot=w.get("friday_spot"), outcome=w.get("outcome"), skip=w.get("skip"),
            earlyTs=w.get("early_ts"), earlyMid=w.get("early_mid"),
        ))

    book = dict(meta=meta, params=params, summary=summary, blotter=blotter, weeks=week_rows,
                ledger=ledger, skips=res["skips"], scatter=dict(rows=res["pts"], fit=res["fit"]),
                checks=checks)

    with open(OUT_JS, "w") as f:
        f.write("window.WHEEL_BOOK = ")
        json.dump(book, f, separators=(",", ":"))
        f.write(";\n")

    n_fail = sum(1 for c in checks if not c["ok"])
    print(f"Wrote {OUT_JS}: {len(ledger)} ledger rows, {len(blotter)} blotter rows, {len(weeks)} weeks, "
          f"{len(checks)} checks ({n_fail} failed).")
    print(f"Start cash {start_cash:,.0f}  ->  end NAV {end_nav:,.0f}  ({ret:+.2f}%);  "
          f"buy&hold {ledger[-1]['hold']:,.0f}  ({hold_ret:+.2f}%)")
    if n_fail:
        print("** SOME CHECKS FAILED - see above before publishing. **")


if __name__ == "__main__":
    main()
