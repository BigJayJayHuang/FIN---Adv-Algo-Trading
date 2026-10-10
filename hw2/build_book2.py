"""
HW2 poor man's covered call (DAL) backtest  ->  book2.js
=========================================================
    python3 fetch_data2.py          # LSEG pull   -> raw_data2.pkl
    python3 build_book2.py          # backtest    -> book2.js   (the site reads only book2.js)
    python3 build_book2.py --demo   # synthetic data, for testing the site (NEVER turn this in)

THE STRATEGY (poor man's covered call = a diagonal call spread):
  Long leg   Buy ONE call, deep in the money (~20% below spot, moneyness is used as the delta
             proxy since the hourly history has no greeks field) and ~6 months out, at the very
             start of the window. Pay mid. Hold it, marked to market every bar, for the whole
             10-week test - it is never rolled or sold early.
  Short leg  Every Monday (first bar at/after 10:00 ET), if the long call is open, sell 1 call
             expiring that Friday, strike = first listed strike at or above spot x 1.03 (3% OTM),
             at mid. No bid/ask on that call at that bar -> skip the week, nothing invented.
  Friday     stock <= short strike  -> the short call expires worthless, keep the premium.
             stock >  short strike  -> there is no stock to deliver, so the position is closed by
             buying the short call back at its own last mark instead of letting it be assigned.
             This is the practical difference from a real covered call: a PMCC trader never lets
             the short call run into an uncovered assignment, because there's nothing to deliver
             except by exercising the long call early and giving up its remaining time value.
  Capital    Because the short strike is always above the long strike here (+3% vs -20%), FINRA's
             diagonal-spread margin rule treats the short call as fully covered: $0 additional
             margin, as long as that relationship holds (checked below). The capital actually
             needed is just what was paid for the long call.

COVERED CALL comparison (same engine as HW1.2, same ticker, same 3% OTM weekly short call, so the
only thing that differs between the two strategies is what collateralizes the short call: 100
shares here, vs the long call above): buy 100 shares, sell the weekly short call, wait through
expiry, Reg T margin (Initial 50% / Maintenance 25% of stock LMV).

Cash moves only on blotter events.
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
RAW = os.path.join(HERE, "raw_data2.pkl")
OUT_JS = os.path.join(HERE, "book2.js")
ET = "America/New_York"

# ---- strategy knobs ----------------------------------------------------------
LOT = 100
ENTRY_MIN = 10 * 60
SESSION_LO = 9 * 60
SESSION_HI = 15 * 60 + 59
REGT_INITIAL = 0.50
REGT_MAINT = 0.25
SHORT_OTM = 0.03
ITM_EPS = 0.005
CC_CASH_BUFFER = 1.25     # covered call: starting cash = 100 shares x entry spot x this, rounded up
PMCC_CASH_BUFFER = 1.20   # PMCC: starting cash = long call cost x this, rounded up
# ------------------------------------------------------------------------------


# ============================ loading & clean-up ==============================
def load_payload(demo: bool):
    if demo:
        from demo_data2 import make_raw
        return make_raw()
    if not os.path.exists(RAW):
        sys.exit("raw_data2.pkl not found. Run fetch_data2.py first (needs LSEG Workspace), or use --demo.")
    with open(RAW, "rb") as f:
        p = pickle.load(f)
    if p.get("long") is None:
        sys.exit("raw_data2.pkl has no long-call data. Re-run fetch_data2.py (it prints why the long call "
                  "didn't resolve) before building the book.")
    return p


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
    if q is None:
        return False
    b, a = q[0], q[1]
    return (b == b) and (a == a) and b > 0 and a > 0 and a >= b


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


def build_chains(options_raw, conv):
    chains = {}
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
    return chains


def build_weeks(expiries, stock, S_print):
    day_bars = {}
    for ts in stock.index:
        day_bars.setdefault(ts.date(), []).append(ts)
    weeks = []
    for F in expiries:
        days = sorted(d for d in day_bars if F - dt.timedelta(days=4) <= d <= F)
        w = dict(i=len(weeks) + 1, expiry=F, entry_ts=None, expiry_ts=None, skip=None, first_ts=None)
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
            w["entry_ts"] = cand[0] if cand else None
            if not cand:
                w["skip"] = "no stock print at/after 10:00 ET on the entry day"
        weeks.append(w)
    return weeks


def fmt_call(ticker, F, K, tag=""):
    return f"{ticker} {F.strftime('%d %b %Y').lstrip('0')} {K:g} C{tag}"


# ================================= covered call (HW1.2 engine, generalized) ====
def account_cc(cash, shares, S, call_mark):
    stock_mv = shares * S
    opt_mv = -LOT * call_mark if call_mark is not None else 0.0
    nav = cash + stock_mv + opt_mv
    init = REGT_INITIAL * stock_mv
    maint = REGT_MAINT * stock_mv
    return dict(stockMV=stock_mv, optMV=opt_mv, nav=nav, lmv=stock_mv, init=init, maint=maint,
                avail=nav - init, excess=nav - maint)


def run_cc(payload, ticker, start_cash, otm_pct=SHORT_OTM):
    meta_in = payload["meta"]
    stock_raw, options_raw = payload["stock"], payload["options"]
    conv, tz_note = make_converter(stock_raw.index)
    stock = clean_frame(stock_raw, conv)
    stock = stock[stock["TRDPRC_1"].notna() | stock["BID"].notna()]
    S_print = stock["TRDPRC_1"]
    chains = build_chains(options_raw, conv)
    expiries = [dt.date.fromisoformat(s) for s in meta_in["expiries"]]
    weeks = build_weeks(expiries, stock, S_print)

    runnable = [w for w in weeks if w["expiry_ts"] is not None]
    if not runnable:
        sys.exit("No usable weeks in the data.")
    first_ts = min(w["first_ts"] for w in runnable)
    last_ts = max(w["expiry_ts"] for w in runnable)
    entry_map = {w["entry_ts"]: w for w in weeks if w["entry_ts"] is not None}
    expiry_map = {w["expiry_ts"]: w for w in weeks if w["expiry_ts"] is not None}
    timeline = [ts for ts in stock.index if first_ts <= ts <= last_ts]

    cash, shares = start_cash, 0
    call = None
    last_S = None
    ledger, blotter, week_out, skips = [], [], {}, []
    hold_cash = None

    def add_blotter(ts, instr, ric, side, qty, limit, fill, bid, ask, dcash, dshares, rule, note, wk):
        nonlocal cash, shares
        cash += dcash
        shares += dshares
        acc = account_cc(cash, shares, last_S, call["mark"] if call else None)
        blotter.append(dict(n=len(blotter) + 1, t=iso(ts), lbl=lbl(ts), instr=instr, ric=ric, side=side, qty=qty,
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
        stale = False
        if call:
            q = call["q"].get(ts)
            if q is not None and q[0] == q[0] and q[1] == q[1] and q[1] >= q[0] >= 0:
                call["mark"] = (q[0] + q[1]) / 2
            else:
                stale = True

        wk_exp = expiry_map.get(ts)
        if wk_exp and call and call["expiry"] == wk_exp["expiry"]:
            K, F, cric = call["strike"], call["expiry"], call["ric"]
            instr = fmt_call(ticker, F, K)
            ent = week_out[wk_exp["i"]]
            call = None
            if S - K > ITM_EPS:
                add_blotter(ts, instr, cric, "ASSIGN", 1, None, K, None, None, LOT * K, -LOT, "Rule 4",
                            f"Stock {S:.2f} > strike {K:g} at expiry -> ITM, assigned: 100 shares called away at "
                            f"{K:g}, cash +100 x {K:g}. Flat. Next entry day starts the combo again.", wk_exp)
                ent.update(outcome="ASSIGN")
                event = "ASSIGN"
            else:
                add_blotter(ts, instr, cric, "EXPIRE", 1, None, 0.0, None, None, 0.0, 0, "Rule 4",
                            f"Stock {S:.2f} <= strike {K:g} at expiry -> OTM, expires worthless. Keep the shares "
                            f"and the premium; next entry day just writes the next Friday call.", wk_exp)
                ent.update(outcome="EXPIRE")
                event = "EXPIRE"
            ent.update(S_T=r(S, 2))

        wk_in = entry_map.get(ts)
        if wk_in:
            row = dict(week=wk_in["i"], expiry=wk_in["expiry"].isoformat(), entry_t=iso(ts), entry_lbl=lbl(ts),
                       spot=r(S, 2), strike=None, bid=None, ask=None, mid=None, outcome=None, skip=None,
                       S_T=None, nav_start=None, nav_end=None)
            week_out[wk_in["i"]] = row
            chain = chains.get(wk_in["expiry"], [])
            target_c = round(S * (1 + otm_pct), 2)
            listed = [c for c in chain if c["strike"] >= target_c - 1e-9]
            reason = None
            if call is not None:
                reason = "an earlier call is still open (should not happen)"
            elif not listed:
                reason = "no listed call at or above the target strike in the data"
            else:
                c = listed[0]
                q = c["q"].get(ts)
                row["strike"] = c["strike"]
                if not valid_quote(q):
                    reason = f"no bid/ask on the {c['strike']:g} call at {lbl(ts)} - skip the week, no print invented"
                else:
                    bid, ask = float(q[0]), float(q[1])
                    mid = (bid + ask) / 2
                    row.update(bid=r(bid), ask=r(ask), mid=r(mid))
                    buying = shares == 0
                    sh_after = shares + (LOT if buying else 0)
                    cash_after = cash - (LOT * S if buying else 0.0) + LOT * mid
                    pro = account_cc(cash_after, sh_after, S, mid)
                    if pro["avail"] < 0:
                        reason = f"Reg T: available funds would be {pro['avail']:,.0f} after the combo - the trade could not have been put on"
                    else:
                        if buying:
                            add_blotter(ts, ticker, meta_in.get("stock_ric", ticker), "BUY", LOT, None, S, None, None,
                                        -LOT * S, LOT, "Rule 1", f"Flat on entry day -> buy 100 at the stock print.", wk_in)
                        call = dict(strike=c["strike"], expiry=wk_in["expiry"], ric=c["ric"], mark=mid, week=wk_in["i"], q=c["q"])
                        otm = (c["strike"] / S - 1) * 100
                        why = (f"Strike rule: target = spot {S:.2f} x {1 + otm_pct:.2f} = {target_c:.2f}; first listed "
                               f"strike at/above it -> {c['strike']:g} ({otm:+.2f}% from spot). ")
                        add_blotter(ts, fmt_call(ticker, wk_in["expiry"], c["strike"]), c["ric"], "SELL", 1, mid, mid, bid, ask,
                                    LOT * mid, 0, "Rules 2-3", why + f"Limit = fill = mid = ({bid:.2f}+{ask:.2f})/2.", wk_in)
                        event = "OPEN"
            if reason:
                row["skip"] = reason
                row["outcome"] = "SKIP"
                skips.append(dict(week=wk_in["i"], t=iso(ts), lbl=lbl(ts), reason=reason, holding=shares > 0))
                event = "SKIP"

        mark = call["mark"] if call else None
        acc = account_cc(cash, shares, S, mark)
        if hold_cash is None and any(b["side"] == "BUY" for b in blotter):
            hold_cash = start_cash - LOT * S
        hold_nav = (hold_cash + LOT * S) if hold_cash is not None else start_cash
        ledger.append(dict(t=iso(ts), lbl=lbl(ts), d=ts.strftime("%Y-%m-%d"), S=r(S, 2), sh=shares,
                            call=None if not call else dict(k=call["strike"], exp=call["expiry"].isoformat(), mark=r(call["mark"]), stale=stale, ric=call["ric"]),
                            cash=r(cash, 2), stockMV=r(acc["stockMV"], 2), optMV=r(acc["optMV"], 2), nav=r(acc["nav"], 2),
                            lmv=r(acc["lmv"], 2), init=r(acc["init"], 2), maint=r(acc["maint"], 2),
                            avail=r(acc["avail"], 2), excess=r(acc["excess"], 2), hold=r(hold_nav, 2), event=event))

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

    pts = []
    for F, chain in chains.items():
        for c in chain:
            for ts, q in c["q"].items():
                if ts < first_ts or ts > last_ts:
                    continue
                b, a, trd = q[0], q[1], q[2]
                if b == b and a == a and a >= b >= 0 and a > 0 and trd == trd and trd > 0:
                    Sx = S_print.get(ts, np.nan)
                    pts.append([r((b + a) / 2), r(trd), c["ric"], lbl(ts), c["strike"], r(Sx, 2) if Sx == Sx else None, r(b), r(a)])
    pts.sort(key=lambda p: (p[2], p[3]))
    fit = None
    if len(pts) >= 3:
        x = np.array([p[0] for p in pts]); y = np.array([p[1] for p in pts])
        b1, b0 = np.polyfit(x, y, 1)
        ss_res = float(((y - (b0 + b1 * x)) ** 2).sum()); ss_tot = float(((y - y.mean()) ** 2).sum())
        fit = dict(n=len(pts), slope=r(b1, 5), intercept=r(b0, 5), r2=r(1 - ss_res / ss_tot, 5) if ss_tot else None)

    coverage = []
    for w in weeks:
        chain = chains.get(w["expiry"], [])
        bars = sum(c["n"] for c in chain)
        qbars = sum(1 for c in chain for q in c["q"].values() if valid_quote(q))
        tbars = sum(1 for c in chain for q in c["q"].values() if q[2] == q[2])
        coverage.append(dict(week=w["i"], expiry=w["expiry"].isoformat(), contracts=len(chain), bars=bars,
                             quoted=qbars, traded=tbars, strikes=[chain[0]["strike"], chain[-1]["strike"]] if chain else None))

    return dict(otm_pct=otm_pct, start_cash=start_cash, chains=chains, weeks=weeks, ledger=ledger, blotter=blotter,
                week_list=week_list, skips=skips, pts=pts, fit=fit, coverage=coverage, tz_note=tz_note, S_print=S_print,
                first_ts=first_ts, last_ts=last_ts)


def run_checks_cc(res, ticker, otm_pct=SHORT_OTM):
    ledger, blotter, chains, S_print, start_cash = res["ledger"], res["blotter"], res["chains"], res["S_print"], res["start_cash"]
    out = []

    def add(name, ok, detail):
        out.append(dict(name=name, ok=bool(ok), detail=detail))

    sells = [b for b in blotter if b["side"] == "SELL"]
    bad = []
    for b in sells:
        F = dt.date.fromisoformat(next(w["expiry"] for w in res["week_list"] if w["week"] == b["week"]))
        ts = pd.Timestamp(b["t"]).tz_convert(ET)
        c = next(c for c in chains[F] if c["ric"] == b["ric"])
        q = c["q"].get(ts)
        if not valid_quote(q) or abs(b["fill"] - (q[0] + q[1]) / 2) > 1e-4 or not (q[0] <= b["fill"] <= q[1]) or b["limit"] != b["fill"]:
            bad.append(b["n"])
    add("Every short call filled at mid, inside the market, on a real bid/ask at that timestamp", not bad,
        f"{len(sells)} calls sold" + (f"; bad blotter rows {bad}" if bad else ""))

    bad = []
    for b in sells:
        F = dt.date.fromisoformat(next(w["expiry"] for w in res["week_list"] if w["week"] == b["week"]))
        ts = pd.Timestamp(b["t"]).tz_convert(ET)
        S = round(float(S_print.get(ts)), 2)
        target = round(float(S_print.get(ts)) * (1 + otm_pct), 2)
        k = min((c["strike"] for c in chains[F] if c["strike"] >= target - 1e-9), default=None)
        strike_here = parse_call_ric(b["ric"])["strike"]
        if k is None or abs(k - strike_here) > 1e-9 or strike_here < S - 1e-9:
            bad.append(b["n"])
    add(f"Strike = first listed strike at or above spot x {1 + otm_pct:.2f} (never in the money when written)",
        not bad, f"recomputed independently for {len(sells)} writes" + (f"; mismatches {bad}" if bad else ""))

    bad = [b["n"] for b in sells if parse_call_ric(b["ric"])["expiry"].weekday() != 4
           or pd.Timestamp(b["t"]).tz_convert(ET).date() > parse_call_ric(b["ric"])["expiry"]]
    add("Every call expires on a Friday on/after the day it was written", not bad, "ok" if not bad else f"bad rows {bad}")

    bad_sh = [row["t"] for row in ledger if row["sh"] not in (0, LOT)]
    bad_cov = [row["t"] for row in ledger if row["call"] and row["sh"] != LOT]
    open_calls, overlap = 0, False
    for b in blotter:
        if b["side"] == "SELL":
            open_calls += 1
        elif b["side"] in ("EXPIRE", "ASSIGN"):
            open_calls -= 1
        overlap |= open_calls > 1 or open_calls < 0
    add("Never short a call without owning 100 shares; never more than one call open",
        not bad_sh and not bad_cov and not overlap, f"{len(ledger)} ledger rows checked")

    cum, i, bad = start_cash, 0, []
    bl_sorted = sorted(blotter, key=lambda b: b["n"])
    for row in ledger:
        while i < len(bl_sorted) and bl_sorted[i]["t"] <= row["t"]:
            cum += bl_sorted[i]["dcash"]; i += 1
        if abs(cum - row["cash"]) > 0.05:
            bad.append(row["t"])
    add("Cash column = starting cash + blotter cash deltas, nothing else", not bad,
        f"rebuilt at {len(ledger)} timestamps" + (f"; first mismatch {bad[0]}" if bad else ""))

    sh, i, bad = 0, 0, []
    for row in ledger:
        while i < len(bl_sorted) and bl_sorted[i]["t"] <= row["t"]:
            sh += bl_sorted[i]["dshares"]; i += 1
        if sh != row["sh"]:
            bad.append(row["t"])
    add("Share count = BUYs minus assignments", not bad, "ok" if not bad else f"first mismatch {bad[0]}")

    bad = []
    for row in ledger:
        nav = row["cash"] + row["stockMV"] + row["optMV"]
        if abs(nav - row["nav"]) > 0.02 or abs(row["init"] - REGT_INITIAL * row["lmv"]) > 0.02 \
                or abs(row["maint"] - REGT_MAINT * row["lmv"]) > 0.02 \
                or abs(row["avail"] - (row["nav"] - row["init"])) > 0.02 \
                or abs(row["excess"] - (row["nav"] - row["maint"])) > 0.02 or row["optMV"] > 1e-9:
            bad.append(row["t"])
    add("NAV = cash + stock MV + option MV (short call <= 0); Initial 50% and Maintenance 25% of stock LMV; "
        "Available = NAV - Initial; Excess = NAV - Maintenance", not bad, "ok at every timestamp" if not bad else f"first mismatch {bad[0]}")

    neg = [b["n"] for b in blotter if b["side"] in ("BUY", "SELL") and b["avail"] < 0]
    min_av = min(row["avail"] for row in ledger)
    add("Available funds >= 0 when each trade was put on", not neg, f"lowest available funds over the whole run: ${min_av:,.0f}")

    bad = []
    for b in blotter:
        if b["side"] in ("EXPIRE", "ASSIGN"):
            ts = pd.Timestamp(b["t"]).tz_convert(ET)
            S = float(S_print.ffill().get(ts))
            K = parse_call_ric(b["ric"])["strike"]
            if (b["side"] == "ASSIGN") != (S - K > ITM_EPS) or (b["side"] == "ASSIGN" and abs(b["dcash"] - LOT * K) > 0.01):
                bad.append(b["n"])
    add("Assigned if and only if stock > strike at the last Friday bar; assignment pays 100 x strike", not bad,
        "ok" if not bad else f"bad rows {bad}")
    return out


# ================================= poor man's covered call =====================
def account_pmcc(cash, long_mark, short_mark, long_strike, short_strike):
    long_mv = LOT * long_mark if long_mark is not None else 0.0
    short_mv = -LOT * short_mark if short_mark is not None else 0.0
    nav = cash + long_mv + short_mv
    covered = short_strike is None or long_strike is None or short_strike >= long_strike
    return dict(longMV=long_mv, shortMV=short_mv, nav=nav, init=0.0, covered=covered)


def run_pmcc(payload, ticker, start_cash, long_strike, long_expiry, long_ric, otm_pct=SHORT_OTM):
    meta_in = payload["meta"]
    stock_raw, options_raw = payload["stock"], payload["options"]
    conv, tz_note = make_converter(stock_raw.index)
    stock = clean_frame(stock_raw, conv)
    stock = stock[stock["TRDPRC_1"].notna() | stock["BID"].notna()]
    S_print = stock["TRDPRC_1"]
    chains = build_chains(options_raw, conv)
    long_d = clean_frame(payload["long"]["df"], conv)
    long_q = {ts: (row.BID, row.ASK, row.TRDPRC_1, row.ACVOL_UNS, row.NUM_MOVES)
              for ts, row in zip(long_d.index, long_d.itertuples(index=False))}

    expiries = [dt.date.fromisoformat(s) for s in meta_in["expiries"]]
    weeks = build_weeks(expiries, stock, S_print)
    runnable = [w for w in weeks if w["expiry_ts"] is not None]
    if not runnable:
        sys.exit("No usable weeks in the data.")
    first_ts = min(w["first_ts"] for w in runnable)
    last_ts = max(w["expiry_ts"] for w in runnable)
    entry_map = {w["entry_ts"]: w for w in weeks if w["entry_ts"] is not None}
    expiry_map = {w["expiry_ts"]: w for w in weeks if w["expiry_ts"] is not None}
    timeline = [ts for ts in stock.index if first_ts <= ts <= last_ts]

    # first bar with a real long-call quote at/after the first week's target entry
    target_entry = next((w["entry_ts"] for w in weeks if w["entry_ts"] is not None), None)
    long_entry_ts = next((ts for ts in timeline if ts >= target_entry and valid_quote(long_q.get(ts))), None)

    cash = start_cash
    long_pos = None          # dict(strike, expiry, ric, mark)
    call = None               # this week's short call
    last_S = None
    ledger, blotter, week_out, skips = [], [], {}, []
    long_cost = None

    def add_blotter(ts, instr, ric, side, qty, limit, fill, bid, ask, dcash, rule, note, wk):
        nonlocal cash
        cash += dcash
        lm = long_pos["mark"] if long_pos else None
        sm = call["mark"] if call else None
        acc = account_pmcc(cash, lm, sm, long_strike, call["strike"] if call else None)
        blotter.append(dict(n=len(blotter) + 1, t=iso(ts), lbl=lbl(ts), instr=instr, ric=ric, side=side, qty=qty,
                             limit=r(limit), fill=r(fill), bid=r(bid), ask=r(ask), dcash=r(dcash, 2), cash=r(cash, 2),
                             nav=r(acc["nav"], 2), rule=rule, note=note, week=wk["i"] if wk else None))

    for ts in timeline:
        v = S_print.get(ts, np.nan)
        if pd.notna(v):
            last_S = float(v)
        if last_S is None:
            continue
        S = last_S
        event = None

        if ts == long_entry_ts and long_pos is None:
            q = long_q[ts]
            bid, ask = float(q[0]), float(q[1])
            mid = (bid + ask) / 2
            long_pos = dict(strike=long_strike, expiry=long_expiry, ric=long_ric, mark=mid)
            long_cost = LOT * mid
            add_blotter(ts, fmt_call(ticker, long_expiry, long_strike, " (long)"), long_ric, "BUY", 1, mid, mid, bid, ask,
                        -LOT * mid, "Entry", f"Buy 1 call {(1 - long_strike / S) * 100:.1f}% in the money, expiring "
                        f"{long_expiry:%d %b %Y} (~{(long_expiry - ts.date()).days // 30} months out). "
                        f"Limit = fill = mid = ({bid:.2f}+{ask:.2f})/2.", None)

        stale_long = False
        if long_pos:
            q = long_q.get(ts)
            if valid_quote(q):
                long_pos["mark"] = (q[0] + q[1]) / 2
            else:
                stale_long = True

        stale_short = False
        if call:
            q = call["q"].get(ts)
            if valid_quote(q):
                call["mark"] = (q[0] + q[1]) / 2
            else:
                stale_short = True

        wk_exp = expiry_map.get(ts)
        if wk_exp and call and call["expiry"] == wk_exp["expiry"]:
            K, F, cric, mk = call["strike"], call["expiry"], call["ric"], call["mark"]
            instr = fmt_call(ticker, F, K)
            ent = week_out[wk_exp["i"]]
            call = None
            if S - K > ITM_EPS:
                add_blotter(ts, instr, cric, "CLOSE", 1, None, mk, None, None, -LOT * mk, "Friday",
                            f"Stock {S:.2f} > strike {K:g} at expiry -> ITM. No shares to deliver, so I buy the call "
                            f"back at its own last mark ({mk:.3f}) instead of letting it be assigned. "
                            f"The long call stays open.", wk_exp)
                ent.update(outcome="CLOSE")
                event = "CLOSE"
            else:
                add_blotter(ts, instr, cric, "EXPIRE", 1, None, 0.0, None, None, 0.0, "Friday",
                            f"Stock {S:.2f} <= strike {K:g} at expiry -> OTM, expires worthless. Keep the premium; "
                            f"next Monday writes the next Friday call against the same long call.", wk_exp)
                ent.update(outcome="EXPIRE")
                event = "EXPIRE"
            ent.update(S_T=r(S, 2))

        wk_in = entry_map.get(ts)
        if wk_in and long_pos is not None:
            row = dict(week=wk_in["i"], expiry=wk_in["expiry"].isoformat(), entry_t=iso(ts), entry_lbl=lbl(ts),
                       spot=r(S, 2), strike=None, bid=None, ask=None, mid=None, outcome=None, skip=None,
                       S_T=None, nav_start=None, nav_end=None)
            week_out[wk_in["i"]] = row
            chain = chains.get(wk_in["expiry"], [])
            target_c = round(S * (1 + otm_pct), 2)
            listed = [c for c in chain if c["strike"] >= target_c - 1e-9]
            reason = None
            if call is not None:
                reason = "an earlier call is still open (should not happen)"
            elif not listed:
                reason = "no listed call at or above the target strike in the data"
            else:
                c = listed[0]
                q = c["q"].get(ts)
                row["strike"] = c["strike"]
                if not valid_quote(q):
                    reason = f"no bid/ask on the {c['strike']:g} call at {lbl(ts)} - skip the week, no print invented"
                elif c["strike"] < long_pos["strike"]:
                    reason = f"target short strike {c['strike']:g} would be below the long strike {long_pos['strike']:g} - not fully covered, skipped"
                else:
                    bid, ask = float(q[0]), float(q[1])
                    mid = (bid + ask) / 2
                    row.update(bid=r(bid), ask=r(ask), mid=r(mid))
                    call = dict(strike=c["strike"], expiry=wk_in["expiry"], ric=c["ric"], mark=mid, week=wk_in["i"], q=c["q"])
                    otm = (c["strike"] / S - 1) * 100
                    why = (f"Strike rule: target = spot {S:.2f} x {1 + otm_pct:.2f} = {target_c:.2f}; first listed "
                           f"strike at/above it -> {c['strike']:g} ({otm:+.2f}% from spot, {c['strike'] - long_pos['strike']:g} "
                           f"above the {long_pos['strike']:g} long strike, so still fully covered). ")
                    add_blotter(ts, fmt_call(ticker, wk_in["expiry"], c["strike"]), c["ric"], "SELL", 1, mid, mid, bid, ask,
                                LOT * mid, "Rules 2-3", why + f"Limit = fill = mid = ({bid:.2f}+{ask:.2f})/2.", wk_in)
                    event = "OPEN"
            if reason:
                row["skip"] = reason
                row["outcome"] = "SKIP"
                skips.append(dict(week=wk_in["i"], t=iso(ts), lbl=lbl(ts), reason=reason, holding=long_pos is not None))
                event = "SKIP"

        lm = long_pos["mark"] if long_pos else None
        sm = call["mark"] if call else None
        acc = account_pmcc(cash, lm, sm, long_strike, call["strike"] if call else None)
        ledger.append(dict(t=iso(ts), lbl=lbl(ts), d=ts.strftime("%Y-%m-%d"), S=r(S, 2),
                            long=None if not long_pos else dict(k=long_pos["strike"], exp=long_pos["expiry"].isoformat(),
                                                                 mark=r(long_pos["mark"]), stale=stale_long, ric=long_pos["ric"]),
                            call=None if not call else dict(k=call["strike"], exp=call["expiry"].isoformat(),
                                                             mark=r(call["mark"]), stale=stale_short, ric=call["ric"]),
                            cash=r(cash, 2), longMV=r(acc["longMV"], 2), shortMV=r(acc["shortMV"], 2),
                            nav=r(acc["nav"], 2), covered=acc["covered"], event=event))

    by_ts = {row["t"]: row for row in ledger}
    for w in weeks:
        if w["i"] in week_out:
            row = week_out[w["i"]]
        else:
            reason = w["skip"] or "entry bar not reached before the long call was bought"
            row = dict(week=w["i"], expiry=w["expiry"].isoformat(), entry_t=None, entry_lbl=None,
                       spot=None, strike=None, bid=None, ask=None, mid=None, outcome="SKIP",
                       skip=reason, S_T=None, nav_start=None, nav_end=None)
            week_out[w["i"]] = row
            if w["i"] not in [s["week"] for s in skips]:
                skips.append(dict(week=w["i"], t=None, lbl=None, reason=reason, holding=False))
        if w["entry_ts"] is not None and iso(w["entry_ts"]) in by_ts:
            row["nav_start"] = by_ts[iso(w["entry_ts"])]["nav"]
        if w["expiry_ts"] is not None and iso(w["expiry_ts"]) in by_ts:
            row["nav_end"] = by_ts[iso(w["expiry_ts"])]["nav"]
            if row["outcome"] is None:
                row["outcome"] = "SKIP"
            if row["outcome"] == "SKIP" and row["S_T"] is None:
                row["S_T"] = by_ts[iso(w["expiry_ts"])]["S"]
    week_list = [week_out[w["i"]] for w in weeks]

    return dict(start_cash=start_cash, long_strike=long_strike, long_expiry=long_expiry, long_ric=long_ric,
                long_cost=long_cost, long_entry_ts=long_entry_ts, chains=chains, weeks=weeks, ledger=ledger,
                blotter=blotter, week_list=week_list, skips=skips, tz_note=tz_note, S_print=S_print,
                first_ts=first_ts, last_ts=last_ts, long_q=long_q)


def run_checks_pmcc(res, ticker, otm_pct=SHORT_OTM):
    ledger, blotter, chains, S_print = res["ledger"], res["blotter"], res["chains"], res["S_print"]
    long_q, long_strike = res["long_q"], res["long_strike"]
    out = []

    def add(name, ok, detail):
        out.append(dict(name=name, ok=bool(ok), detail=detail))

    buys = [b for b in blotter if b["side"] == "BUY"]
    bad = []
    for b in buys:
        ts = pd.Timestamp(b["t"]).tz_convert(ET)
        q = long_q.get(ts)
        if not valid_quote(q) or abs(b["fill"] - (q[0] + q[1]) / 2) > 1e-4:
            bad.append(b["n"])
    add("The long call was bought once, at mid, on a real bid/ask at that timestamp", not bad and len(buys) == 1,
        f"{len(buys)} long call purchase(s)" + (f"; bad rows {bad}" if bad else ""))

    entry_spot = next((float(row["S"]) for row in ledger if row["t"] == buys[0]["t"]), None) if buys else None
    itm_pct = (1 - long_strike / entry_spot) * 100 if entry_spot else None
    add("Long call strike is at least 10% in the money at entry (recomputed from that bar's stock print)",
        itm_pct is not None and itm_pct >= 10 - 1e-6,
        f"strike {long_strike:g} vs spot {entry_spot:.2f} at entry = {itm_pct:+.1f}% ITM" if entry_spot else "no entry found")

    sells = [b for b in blotter if b["side"] == "SELL"]
    bad = []
    for b in sells:
        F = dt.date.fromisoformat(next(w["expiry"] for w in res["week_list"] if w["week"] == b["week"]))
        ts = pd.Timestamp(b["t"]).tz_convert(ET)
        c = next(c for c in chains[F] if c["ric"] == b["ric"])
        q = c["q"].get(ts)
        if not valid_quote(q) or abs(b["fill"] - (q[0] + q[1]) / 2) > 1e-4 or not (q[0] <= b["fill"] <= q[1]):
            bad.append(b["n"])
    add("Every short call filled at mid, inside the market, on a real bid/ask at that timestamp", not bad,
        f"{len(sells)} calls sold" + (f"; bad blotter rows {bad}" if bad else ""))

    bad = []
    for b in sells:
        F = dt.date.fromisoformat(next(w["expiry"] for w in res["week_list"] if w["week"] == b["week"]))
        ts = pd.Timestamp(b["t"]).tz_convert(ET)
        target = round(float(S_print.get(ts)) * (1 + otm_pct), 2)
        k = min((c["strike"] for c in chains[F] if c["strike"] >= target - 1e-9), default=None)
        strike_here = parse_call_ric(b["ric"])["strike"]
        if k is None or abs(k - strike_here) > 1e-9:
            bad.append(b["n"])
    add(f"Short strike = first listed strike at or above spot x {1 + otm_pct:.2f}", not bad,
        f"recomputed independently for {len(sells)} writes" + (f"; mismatches {bad}" if bad else ""))

    bad = [b["n"] for b in sells if parse_call_ric(b["ric"])["strike"] < long_strike - 1e-9]
    add("Short strike is always at or above the long strike (the diagonal is fully covered -> $0 extra margin)",
        not bad, f"checked {len(sells)} writes" + (f"; bad rows {bad}" if bad else ""))

    bad = []
    for b in blotter:
        if b["side"] in ("EXPIRE", "CLOSE"):
            ts = pd.Timestamp(b["t"]).tz_convert(ET)
            S = float(S_print.ffill().get(ts))
            K = parse_call_ric(b["ric"])["strike"]
            itm = S - K > ITM_EPS
            if (b["side"] == "CLOSE") != itm:
                bad.append(b["n"])
    add("Closed (bought back) if and only if the stock is above the short strike at Friday's last bar; "
        "otherwise it expires worthless", not bad, "ok" if not bad else f"bad rows {bad}")

    open_calls, overlap = 0, False
    for b in blotter:
        if b["side"] == "SELL":
            open_calls += 1
        elif b["side"] in ("EXPIRE", "CLOSE"):
            open_calls -= 1
        overlap |= open_calls > 1 or open_calls < 0
    add("Never more than one short call open at a time", not overlap, f"{len(blotter)} blotter rows checked")

    cum, i, bad = res["start_cash"], 0, []
    bl_sorted = sorted(blotter, key=lambda b: b["n"])
    for row in ledger:
        while i < len(bl_sorted) and bl_sorted[i]["t"] <= row["t"]:
            cum += bl_sorted[i]["dcash"]; i += 1
        if abs(cum - row["cash"]) > 0.05:
            bad.append(row["t"])
    add("Cash column = starting cash + blotter cash deltas, nothing else", not bad,
        f"rebuilt at {len(ledger)} timestamps" + (f"; first mismatch {bad[0]}" if bad else ""))

    bad = []
    for row in ledger:
        longMV = row["longMV"]; shortMV = row["shortMV"]
        nav = row["cash"] + longMV + shortMV
        if abs(nav - row["nav"]) > 0.02 or shortMV > 1e-9:
            bad.append(row["t"])
    add("NAV = cash + long call MV + short call MV (short call <= 0)", not bad,
        "ok at every timestamp" if not bad else f"first mismatch {bad[0]}")
    return out


def summarize_cc(res, ticker):
    L, bl, wk = res["ledger"], res["blotter"], res["week_list"]
    end, start = L[-1], L[0]
    sells = [b for b in bl if b["side"] == "SELL"]
    prem = sum(b["dcash"] for b in sells)
    return dict(weeks=len(wk), traded=sum(1 for w in wk if w["outcome"] in ("EXPIRE", "ASSIGN")),
                expired=sum(1 for w in wk if w["outcome"] == "EXPIRE"), assigned=sum(1 for w in wk if w["outcome"] == "ASSIGN"),
                skipped=sum(1 for w in wk if w["outcome"] == "SKIP"), premium=r(prem, 2),
                startNav=r(start["nav"], 2), endNav=r(end["nav"], 2), startCash=res["start_cash"],
                ret=r((end["nav"] / res["start_cash"] - 1) * 100, 3), holdRet=r((end["hold"] / res["start_cash"] - 1) * 100, 3),
                minAvail=r(min(x["avail"] for x in L), 2), capitalRequired=r(res["start_cash"], 0),
                maxDD=r(_max_drawdown([x["nav"] for x in L]), 3))


def summarize_pmcc(res):
    L, bl, wk = res["ledger"], res["blotter"], res["week_list"]
    end, start = L[-1], L[0]
    sells = [b for b in bl if b["side"] == "SELL"]
    closes = [b for b in bl if b["side"] == "CLOSE"]
    gross_prem = sum(b["dcash"] for b in sells)
    buyback_cost = -sum(b["dcash"] for b in closes)
    net_prem = gross_prem - buyback_cost
    return dict(weeks=len(wk), traded=sum(1 for w in wk if w["outcome"] in ("EXPIRE", "CLOSE")),
                expired=sum(1 for w in wk if w["outcome"] == "EXPIRE"), closed=sum(1 for w in wk if w["outcome"] == "CLOSE"),
                skipped=sum(1 for w in wk if w["outcome"] == "SKIP"), grossPremium=r(gross_prem, 2),
                buybackCost=r(buyback_cost, 2), premium=r(net_prem, 2),
                startNav=r(start["nav"], 2), endNav=r(end["nav"], 2), startCash=res["start_cash"],
                ret=r((end["nav"] / res["start_cash"] - 1) * 100, 3), minCash=r(min(x["cash"] for x in L), 2),
                minNav=r(min(x["nav"] for x in L), 2), capitalRequired=r(res["long_cost"], 0),
                longCost=r(res["long_cost"], 2), longStrike=res["long_strike"], longExpiry=res["long_expiry"].isoformat(),
                maxDD=r(_max_drawdown([x["nav"] for x in L]), 3))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true")
    args = ap.parse_args()
    payload = load_payload(args.demo)
    meta_in = payload["meta"]
    ticker = meta_in.get("root", "DAL")
    long_meta = meta_in.get("long") or {}
    long_strike = float(long_meta["strike"])
    long_expiry = dt.date.fromisoformat(long_meta["expiry"])
    long_ric = long_meta["ric"]
    otm_pct = meta_in.get("short_otm", SHORT_OTM)

    conv0, _ = make_converter(payload["stock"].index)
    stock0 = clean_frame(payload["stock"], conv0)
    entry_spot_guess = float(stock0["TRDPRC_1"].dropna().iloc[0])
    cc_start_cash = math.ceil(LOT * entry_spot_guess * CC_CASH_BUFFER / 100) * 100

    # provisional PMCC run to discover the long call's actual cost, then a real run with the right start cash
    probe = run_pmcc(payload, ticker, 10 ** 9, long_strike, long_expiry, long_ric, otm_pct)
    if probe["long_cost"] is None:
        sys.exit("The long call never had a valid quote in this window - nothing to backtest. "
                  "Check raw_data2.pkl / widen LONG_BAND in fetch_data2.py and re-pull.")
    pmcc_start_cash = math.ceil(probe["long_cost"] * PMCC_CASH_BUFFER / 100) * 100

    cc = run_cc(payload, ticker, cc_start_cash, otm_pct)
    cc_checks = run_checks_cc(cc, ticker, otm_pct)
    cc_summ = summarize_cc(cc, ticker)

    pmcc = run_pmcc(payload, ticker, pmcc_start_cash, long_strike, long_expiry, long_ric, otm_pct)
    pmcc_checks = run_checks_pmcc(pmcc, ticker, otm_pct)
    pmcc_summ = summarize_pmcc(pmcc)

    book = dict(
        meta=dict(ticker=ticker, stockRic=meta_in.get("stock_ric"), interval=meta_in.get("interval"),
                  source=meta_in.get("source", "LSEG"), demo=bool(meta_in.get("demo")), fetchedAt=meta_in.get("fetched_at"),
                  tz=cc["tz_note"], first=iso(cc["first_ts"]), last=iso(cc["last_ts"]),
                  built=dt.datetime.now().strftime("%Y-%m-%d %H:%M"), shortOtm=otm_pct,
                  longStrike=long_strike, longExpiry=long_expiry.isoformat(), longRic=long_ric),
        config=dict(ccStartCash=cc_start_cash, pmccStartCash=pmcc_start_cash, lot=LOT,
                    entry="10:00 ET (first bar at or after)", regtInitial=REGT_INITIAL, regtMaint=REGT_MAINT),
        cc=dict(summary=cc_summ, weeks=cc["week_list"], blotter=cc["blotter"], ledger=cc["ledger"],
                skips=cc["skips"], checks=cc_checks,
                scatter=dict(rows=cc["pts"], fit=cc["fit"]), coverage=cc["coverage"]),
        pmcc=dict(summary=pmcc_summ, weeks=pmcc["week_list"], blotter=pmcc["blotter"], ledger=pmcc["ledger"],
                  skips=pmcc["skips"], checks=pmcc_checks),
    )
    with open(OUT_JS, "w") as f:
        f.write("// generated by build_book2.py - do not edit by hand\n")
        f.write("window.PMCC_BOOK = " + json.dumps(book, separators=(",", ":"), allow_nan=False) + ";\n")

    print(f"tz: {cc['tz_note']}")
    print(f"Covered call: start cash ${cc_start_cash:,.0f}  NAV {cc_summ['startNav']:,.2f} -> {cc_summ['endNav']:,.2f} "
          f"({cc_summ['ret']:+.2f}%)  buy&hold {cc_summ['holdRet']:+.2f}%  premium ${cc_summ['premium']:,.2f}")
    print(f"PMCC: long call {long_ric} cost ${pmcc_summ['longCost']:,.2f}  start cash ${pmcc_start_cash:,.0f}  "
          f"NAV {pmcc_summ['startNav']:,.2f} -> {pmcc_summ['endNav']:,.2f} ({pmcc_summ['ret']:+.2f}%)  "
          f"net premium ${pmcc_summ['premium']:,.2f}")
    print(f"Capital required: covered call ${cc_summ['capitalRequired']:,.0f}  vs  PMCC ${pmcc_summ['capitalRequired']:,.0f}  "
          f"({pmcc_summ['capitalRequired'] / cc_summ['capitalRequired'] * 100:.0f}% of it)")
    checks = cc_checks + pmcc_checks
    for c in checks:
        print(("  PASS  " if c["ok"] else "  FAIL  ") + c["name"])
    bad = [c for c in checks if not c["ok"]]
    print(f"wrote {OUT_JS}  ({os.path.getsize(OUT_JS) / 1024:.0f} KB)")
    if args.demo:
        print("\n*** DEMO DATA: synthetic. Do not turn this in. Run fetch_data2.py + build_book2.py for the real thing. ***")
    if bad:
        sys.exit(1)


if __name__ == "__main__":
    main()
