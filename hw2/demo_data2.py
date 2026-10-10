"""
SYNTHETIC data with the same shape as the LSEG pull, for testing the site only.

    python3 build_book2.py --demo

Random-walk DAL, Black-Scholes call quotes with a made-up spread, sparse trades, plus one
longer-dated deep-ITM call for the long leg. Nothing here is real. Never turn this in.
"""
import datetime as dt
import math

import numpy as np
import pandas as pd

from fetch_data2 import (FIELDS, last_expiries, third_friday, add_months,
                          LONG_MONTHS_OUT, LONG_ITM_TARGET, SHORT_OTM)
from rics import build_call_ric, candidate_strikes

HOLIDAYS = {dt.date(2026, 9, 7)}  # Labor Day
UTC_HOURS = range(13, 20)         # 09:00..15:00 ET while on daylight time


def _ncdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_call(S, K, T, sigma=0.34, r=0.045):
    if T <= 1e-9:
        return max(S - K, 0.0)
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    return S * _ncdf(d1) - K * math.exp(-r * T) * _ncdf(d2)


def _quote(mid, rng):
    spread = min(0.60, max(0.02, 0.03 * mid + 0.01))
    bid = max(0.0, round(mid - spread / 2, 2))
    ask = max(round(mid + spread / 2, 2), round(bid + 0.01, 2))
    return bid, ask, spread


def make_raw(seed: int = 7):
    rng = np.random.default_rng(seed)
    expiries = last_expiries(10, dt.date(2026, 10, 2))
    window_start = expiries[0] - dt.timedelta(days=4)

    # ---- stock -----------------------------------------------------------
    stamps = []
    for F in expiries:
        for d in (F - dt.timedelta(days=4 - i) for i in range(5)):
            if d in HOLIDAYS:
                continue
            for h in UTC_HOURS:
                stamps.append(pd.Timestamp(d.year, d.month, d.day, h))
    idx = pd.DatetimeIndex(stamps)
    S = np.empty(len(idx))
    px = 58.40
    for i, ts in enumerate(idx):
        sig = 0.0075 if ts.hour == 13 else 0.0040
        px *= math.exp(rng.normal(0.00035, sig))
        S[i] = round(px, 2)
    stock = pd.DataFrame(index=idx, columns=FIELDS, dtype=float)
    stock["TRDPRC_1"] = S
    stock["OPEN_PRC"] = np.r_[S[0], S[:-1]]
    stock["HIGH_1"] = S + rng.uniform(0.03, 0.18, len(S))
    stock["LOW_1"] = S - rng.uniform(0.03, 0.18, len(S))
    stock["BID"] = S - 0.01
    stock["ASK"] = S + 0.01
    stock["ACVOL_UNS"] = rng.integers(500_000, 4_000_000, len(S)).astype(float)
    stock["NUM_MOVES"] = rng.integers(2_000, 15_000, len(S)).astype(float)
    stock.index.name = "ts"

    # ---- weekly short-call legs -------------------------------------------
    options, tried, weeks = {}, [], {}
    for wi, F in enumerate(expiries):
        mon = F - dt.timedelta(days=4)
        wk = stock[(stock.index.date >= mon) & (stock.index.date <= F)]
        mon_rows = wk[wk.index.date == mon]
        ref = float((mon_rows if len(mon_rows) else wk.iloc[:7])["TRDPRC_1"].median())
        strikes = candidate_strikes(ref * 0.98, ref * 1.08)
        expiry_ts = pd.Timestamp(F.year, F.month, F.day, 20)
        n_ok = 0
        for K in strikes:
            ric = build_call_ric("DAL", F, K, expired=True)
            tried.append(ric)
            rows = []
            for ts, srow in wk.iterrows():
                s = srow["TRDPRC_1"]
                T = max((expiry_ts - (ts + pd.Timedelta(hours=1))).total_seconds(), 0) / (365 * 86400)
                mid = bs_call(s, K, T)
                bid, ask, spread = _quote(mid, rng)
                row = dict(BID=bid, ASK=ask, TRDPRC_1=np.nan, OPEN_PRC=np.nan, HIGH_1=np.nan,
                           LOW_1=np.nan, ACVOL_UNS=np.nan, NUM_MOVES=np.nan)
                if rng.random() < 0.02 or (wi == 5 and ts.date() == mon and ts.hour == 14):
                    row["BID"] = row["ASK"] = np.nan
                else:
                    p_trade = 0.5 * math.exp(-abs(math.log(K / s)) / 0.03)
                    if rng.random() < p_trade and ask > 0:
                        tr = round(max(0.01, (bid + ask) / 2 + rng.uniform(-spread / 2, spread / 2)), 2)
                        row.update(TRDPRC_1=tr, OPEN_PRC=tr, HIGH_1=tr + 0.01, LOW_1=max(0.01, tr - 0.01),
                                   ACVOL_UNS=float(rng.integers(1, 300)), NUM_MOVES=float(rng.integers(1, 30)))
                rows.append((ts, row))
            df = pd.DataFrame([r for _, r in rows], index=pd.DatetimeIndex([t for t, _ in rows], name="ts"))[FIELDS]
            df = df.dropna(how="all")
            if len(df) and df[["BID", "ASK"]].notna().any().any():
                options[ric] = df
                n_ok += 1
        weeks[F.isoformat()] = {"ref_spot": ref, "strikes_tried": len(strikes), "rics_ok": n_ok}

    # ---- long call leg: one contract, whole window -------------------------
    mon_rows = stock[stock.index.date == window_start]
    spot0 = float((mon_rows if len(mon_rows) else stock.iloc[:7])["TRDPRC_1"].median())
    target_month = add_months(window_start, LONG_MONTHS_OUT)
    long_expiry = third_friday(target_month.year, target_month.month)
    target_strike = spot0 * (1 - LONG_ITM_TARGET)
    long_strike = min(candidate_strikes(target_strike - 2, target_strike + 2), key=lambda k: abs(k - target_strike))
    long_ric = build_call_ric("DAL", long_expiry, long_strike, expired=False)
    expiry_ts = pd.Timestamp(long_expiry.year, long_expiry.month, long_expiry.day, 20)
    rows = []
    for ts, srow in stock.iterrows():
        s = srow["TRDPRC_1"]
        T = max((expiry_ts - (ts + pd.Timedelta(hours=1))).total_seconds(), 0) / (365 * 86400)
        mid = bs_call(s, long_strike, T)
        bid, ask, spread = _quote(mid, rng)
        row = dict(BID=bid, ASK=ask, TRDPRC_1=np.nan, OPEN_PRC=np.nan, HIGH_1=np.nan, LOW_1=np.nan,
                   ACVOL_UNS=np.nan, NUM_MOVES=np.nan)
        if rng.random() < 0.01:
            row["BID"] = row["ASK"] = np.nan
        else:
            tr = round(max(0.01, mid + rng.uniform(-spread / 2, spread / 2)), 2)
            row.update(TRDPRC_1=tr, OPEN_PRC=tr, HIGH_1=tr + 0.02, LOW_1=max(0.01, tr - 0.02),
                       ACVOL_UNS=float(rng.integers(1, 150)), NUM_MOVES=float(rng.integers(1, 15)))
        rows.append((ts, row))
    long_df = pd.DataFrame([r for _, r in rows], index=pd.DatetimeIndex([t for t, _ in rows], name="ts"))[FIELDS]
    long_df = long_df.dropna(how="all")
    long_meta = {"ric": long_ric, "strike": long_strike, "expiry": long_expiry.isoformat(),
                 "entry_spot": spot0, "window_start": window_start.isoformat()}

    meta = {
        "stock_ric": "DAL.N", "root": "DAL", "interval": "hourly",
        "expiries": [d.isoformat() for d in expiries],
        "fetched_at": "SYNTHETIC", "source": "SYNTHETIC (demo_data2.py)", "demo": True,
        "weeks": weeks, "long": long_meta, "short_otm": SHORT_OTM,
    }
    return {"meta": meta, "stock": stock, "options": options, "tried": tried, "long": {"df": long_df}}
