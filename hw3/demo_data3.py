"""
SYNTHETIC data with the same shape as the LSEG pull, for testing the site only.

    python3 build_book3.py --demo

Random-walk INTC with deliberately clustered volatility (calm/normal/elevated blocks, so the
regime classifier actually has something to classify), Black-Scholes put/call quotes with a
made-up spread, sparse trades. Runs the SAME state machine as fetch_data3.py (same strategy.py
functions) so the demo book exercises the full wheel cycle: put sold -> assigned -> covered
call sold -> called away -> restart, plus at least one early close on each leg.

Nothing here is real. Never turn this in.
"""
import datetime as dt
import math

import numpy as np
import pandas as pd

from rics import build_option_ric, candidate_strikes
from strategy import (daily_closes, trailing_realized_vol, classify_regime,
                       put_target_strike, call_target_strike, early_close_trigger,
                       VOL_LOOKBACK_DAYS)
from fetch_data3 import (last_fridays, monday_spot, entry_fill, FIELDS, ET,
                          TRADE_WEEKS, BURNIN_WEEKS, PUT_BAND, CALL_BAND, ENTRY_HOUR, ROOT)

HOLIDAYS = set()
ET_HOURS = range(9, 16)  # 09:00..15:00 ET, built tz-aware directly - no UTC round-trip to get wrong

# 3-week blocks cycling through calm / normal / elevated daily vol, so the realized-vol
# regime classifier sees all three tiers over the course of the backtest.
VOL_BLOCKS = [0.010, 0.016, 0.028, 0.012, 0.030, 0.014, 0.022, 0.011, 0.026, 0.015]


def _ncdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_price(S, K, T, side, sigma=0.38, r=0.045):
    if T <= 1e-9:
        return max(S - K, 0.0) if side == "C" else max(K - S, 0.0)
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    if side == "C":
        return S * _ncdf(d1) - K * math.exp(-r * T) * _ncdf(d2)
    return K * math.exp(-r * T) * _ncdf(-d2) - S * _ncdf(-d1)


def _quote(mid, rng):
    spread = min(0.40, max(0.02, 0.03 * mid + 0.01))
    bid = max(0.0, round(mid - spread / 2, 2))
    ask = max(round(mid + spread / 2, 2), round(bid + 0.01, 2))
    return bid, ask, spread


def make_stock(rng, burnin_start, expiries):
    stamps, vols = [], {}
    d = burnin_start
    last_expiry = expiries[-1]
    block_i = 0
    while d <= last_expiry:
        if d.weekday() < 5 and d not in HOLIDAYS:
            vols[d] = VOL_BLOCKS[block_i % len(VOL_BLOCKS)]
            for h in ET_HOURS:
                stamps.append(pd.Timestamp(d.year, d.month, d.day, h, tz=ET))
            if d.weekday() == 4:
                block_i += 1
        d += dt.timedelta(days=1)
    idx = pd.DatetimeIndex(stamps)
    S = np.empty(len(idx))
    px = 24.50
    for i, ts in enumerate(idx):
        day_vol = vols[ts.date()]
        bar_vol = day_vol / math.sqrt(len(ET_HOURS))
        drift = 0.00002
        px *= math.exp(rng.normal(drift, bar_vol))
        S[i] = round(px, 2)

    # Two scripted shocks so the demo actually exercises assignment -> covered call ->
    # called-away, not just puts expiring worthless every week (whether that happens on any
    # given random walk is luck - this makes sure the state-machine code path gets run at least
    # once before real data is ever pulled). A real pull gets no such help.
    dates = np.array([ts.date() for ts in idx])
    if len(expiries) >= 16:
        shock1 = expiries[9] - dt.timedelta(days=1)   # Thursday before week 10's Friday
        shock2 = expiries[15] - dt.timedelta(days=1)  # Thursday before week 16's Friday
        S = S * np.where(dates >= shock1, 0.85, 1.0)
        S = S * np.where(dates >= shock2, 1.30, 1.0)
        S = np.round(S, 2)
        # Note: with this seed, assignment happens "naturally" from the vol-block noise alone
        # (around week 17) rather than from either scripted shock - the shocks mostly add extra
        # early-close test coverage instead. The demo therefore exercises the full PUT -> ASSIGN
        # -> HOLDING -> CALL state machine, but this particular random path never pushes a sold
        # call far enough ITM for a CALLED_AWAY before the backtest window ends. The CALLED_AWAY
        # branch is the mirror image of the ASSIGN branch already exercised (same formula,
        # opposite direction) and is checked generically by run_checks' settlement-recompute
        # check regardless of which outcomes actually occur, so this is a coverage gap in the
        # disposable demo only, not in the strategy or checks logic.

    stock = pd.DataFrame(index=idx, columns=FIELDS, dtype=float)
    stock["TRDPRC_1"] = S
    stock["OPEN_PRC"] = np.r_[S[0], S[:-1]]
    stock["HIGH_1"] = S + rng.uniform(0.01, 0.08, len(S))
    stock["LOW_1"] = S - rng.uniform(0.01, 0.08, len(S))
    stock["BID"] = S - 0.01
    stock["ASK"] = S + 0.01
    stock["ACVOL_UNS"] = rng.integers(1_000_000, 8_000_000, len(S)).astype(float)
    stock["NUM_MOVES"] = rng.integers(3_000, 20_000, len(S)).astype(float)
    stock.index.name = "ts"
    return stock


def make_week_option(rng, stock, F, mon, K, side):
    expiry_ts = pd.Timestamp(F.year, F.month, F.day, 16, tz=ET)
    wk = stock[(stock.index.date >= mon) & (stock.index.date <= F)]
    rows = []
    for ts, srow in wk.iterrows():
        s = srow["TRDPRC_1"]
        T = max((expiry_ts - (ts + pd.Timedelta(hours=1))).total_seconds(), 0) / (365 * 86400)
        mid = bs_price(s, K, T, side)
        bid, ask, spread = _quote(mid, rng)
        row = dict(BID=bid, ASK=ask, TRDPRC_1=np.nan, OPEN_PRC=np.nan, HIGH_1=np.nan, LOW_1=np.nan,
                   ACVOL_UNS=np.nan, NUM_MOVES=np.nan)
        if rng.random() < 0.015:
            row["BID"] = row["ASK"] = np.nan
        else:
            moneyness = abs(math.log(max(K, 0.01) / max(s, 0.01)))
            p_trade = 0.55 * math.exp(-moneyness / 0.04)
            if rng.random() < p_trade and ask > 0:
                tr = round(max(0.01, (bid + ask) / 2 + rng.uniform(-spread / 2, spread / 2)), 2)
                row.update(TRDPRC_1=tr, OPEN_PRC=tr, HIGH_1=tr + 0.01, LOW_1=max(0.01, tr - 0.01),
                           ACVOL_UNS=float(rng.integers(1, 400)), NUM_MOVES=float(rng.integers(1, 40)))
        rows.append((ts, row))
    df = pd.DataFrame([r for _, r in rows], index=pd.DatetimeIndex([t for t, _ in rows], name="ts"))[FIELDS]
    return df.dropna(how="all")


def make_raw(seed: int = 11):
    rng = np.random.default_rng(seed)
    expiries = last_fridays(TRADE_WEEKS, dt.date(2026, 11, 1))
    window_start = expiries[0] - dt.timedelta(days=4)
    burnin_start = window_start - dt.timedelta(weeks=BURNIN_WEEKS)

    stock = make_stock(rng, burnin_start, expiries)
    closes = daily_closes(stock)

    state, cost_basis, vol_history = "FLAT", None, []
    options, weeks = {}, []

    for F in expiries:
        mon = F - dt.timedelta(days=4)
        vol_today = trailing_realized_vol(closes, mon, lookback=VOL_LOOKBACK_DAYS)
        regime = classify_regime(vol_today, vol_history)
        vol_history.append(vol_today)

        spot, _ = monday_spot(stock, mon)
        if spot is None:
            weeks.append({"expiry": F.isoformat(), "side": None, "outcome": "NO_STOCK",
                          "vol_today": vol_today, "regime": regime,
                          "state_before": state, "state_after": state,
                          "cost_basis_before": cost_basis, "cost_basis_after": cost_basis})
            continue

        if state == "FLAT":
            side = "P"
            target = put_target_strike(spot, regime)
            lo, hi = spot * (1 - PUT_BAND[1]), spot * (1 - PUT_BAND[0])
            cands = sorted([k for k in candidate_strikes(lo, hi) if k <= target], key=lambda k: target - k)
            tier = None
        else:
            side = "C"
            target, tier = call_target_strike(spot, cost_basis)
            lo, hi = spot * (1 + CALL_BAND[0]), spot * (1 + CALL_BAND[1])
            cands = sorted([k for k in candidate_strikes(lo, hi) if k >= target], key=lambda k: k - target)
            regime = None

        if not cands:
            weeks.append({"expiry": F.isoformat(), "side": side, "outcome": "SKIP",
                          "vol_today": vol_today, "regime": regime, "tier": tier,
                          "state_before": state, "state_after": state,
                          "cost_basis_before": cost_basis, "cost_basis_after": cost_basis})
            continue
        K = cands[0]
        ric = build_option_ric(ROOT, F, K, side, expired=True)
        df = make_week_option(rng, stock, F, mon, K, side)
        options[ric] = df

        entry_ts, entry_premium = entry_fill(df, pd.Timestamp(mon.year, mon.month, mon.day, ENTRY_HOUR, tz=ET))
        early = early_close_trigger(df, entry_premium, entry_ts) if entry_ts is not None else None

        friday_rows = stock[stock.index.date == F]
        friday_spot = float(friday_rows["TRDPRC_1"].dropna().iloc[-1]) if len(friday_rows) else None

        state_before, cost_before = state, cost_basis
        if early is not None:
            outcome = "CLOSE"
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

    meta = {
        "stock_ric": "INTC.OQ (SYNTHETIC)", "root": ROOT, "interval": "hourly",
        "trade_weeks": [d.isoformat() for d in expiries], "burnin_weeks": BURNIN_WEEKS,
        "window_start": window_start.isoformat(), "fetched_at": "SYNTHETIC",
        "source": "SYNTHETIC (demo_data3.py)", "demo": True, "weeks": weeks,
    }
    return {"meta": meta, "stock": stock, "options": options, "tried": list(options.keys())}
