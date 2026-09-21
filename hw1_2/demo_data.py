"""
SYNTHETIC data with the same shape as the LSEG pull, for testing the site only.

    python3 build_book.py --demo

Random-walk AAPL, Black-Scholes call quotes with a made-up spread, sparse trades.
Nothing here is real. build_book.py stamps the output demo=true and the site shows a
red DEMO banner. Never turn this in.
"""
import datetime as dt
import math

import numpy as np
import pandas as pd

from fetch_data import FIELDS, last_expiries, BAND_LO, BAND_HI
from rics import build_call_ric, candidate_strikes

HOLIDAYS = {dt.date(2026, 9, 7)}   # Labor Day
UTC_HOURS = range(13, 20)          # 09:00..15:00 ET while on daylight time


def _ncdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_call(S, K, T, sigma=0.27, r=0.045):
    if T <= 1e-9:
        return max(S - K, 0.0)
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    return S * _ncdf(d1) - K * math.exp(-r * T) * _ncdf(d2)


def make_raw(seed: int = 11):
    rng = np.random.default_rng(seed)
    expiries = last_expiries(10, dt.date(2026, 9, 20))

    # ---- stock ---------------------------------------------------------------
    stamps = []
    for F in expiries:
        for d in (F - dt.timedelta(days=4 - i) for i in range(5)):
            if d in HOLIDAYS:
                continue
            for h in UTC_HOURS:
                stamps.append(pd.Timestamp(d.year, d.month, d.day, h))
    idx = pd.DatetimeIndex(stamps)
    S = np.empty(len(idx))
    px = 228.40
    for i, ts in enumerate(idx):
        sig = 0.0060 if ts.hour == 13 else 0.0032
        px *= math.exp(rng.normal(0, sig))
        S[i] = round(px, 2)
    stock = pd.DataFrame(index=idx, columns=FIELDS, dtype=float)
    stock["TRDPRC_1"] = S
    stock["OPEN_PRC"] = np.r_[S[0], S[:-1]]
    stock["HIGH_1"] = S + rng.uniform(0.05, 0.30, len(S))
    stock["LOW_1"] = S - rng.uniform(0.05, 0.30, len(S))
    stock["BID"] = S - 0.01
    stock["ASK"] = S + 0.01
    stock["ACVOL_UNS"] = rng.integers(2_000_000, 9_000_000, len(S)).astype(float)
    stock["NUM_MOVES"] = rng.integers(8_000, 40_000, len(S)).astype(float)
    stock.index.name = "ts"

    # ---- options -------------------------------------------------------------
    options, tried, weeks = {}, [], {}
    for wi, F in enumerate(expiries):
        mon = F - dt.timedelta(days=4)
        wk = stock[(stock.index.date >= mon) & (stock.index.date <= F)]
        mon_rows = wk[wk.index.date == mon]
        ref = float((mon_rows if len(mon_rows) else wk.iloc[:7])["TRDPRC_1"].median())
        strikes = candidate_strikes(ref * (1 - BAND_LO), ref * (1 + BAND_HI))
        expiry_ts = pd.Timestamp(F.year, F.month, F.day, 20)   # 16:00 ET in UTC
        n_ok = 0
        for K in strikes:
            ric = build_call_ric("AAPL", F, K, expired=True)
            tried.append(ric)
            rows = []
            for ts, srow in wk.iterrows():
                s = srow["TRDPRC_1"]
                T = max((expiry_ts - (ts + pd.Timedelta(hours=1))).total_seconds(), 0) / (365 * 86400)
                mid = bs_call(s, K, T)
                spread = min(0.60, max(0.01, 0.025 * mid + 0.01))
                bid = max(0.0, round(mid - spread / 2, 2))
                ask = max(round(mid + spread / 2, 2), round(bid + 0.01, 2))
                row = dict(BID=bid, ASK=ask, TRDPRC_1=np.nan, OPEN_PRC=np.nan, HIGH_1=np.nan,
                           LOW_1=np.nan, ACVOL_UNS=np.nan, NUM_MOVES=np.nan)
                # random quote gaps; and one whole week (#7) with no quotes at the Monday 10:00 ET bar
                if rng.random() < 0.02 or (wi == 6 and ts.date() == mon and ts.hour == 14):
                    row["BID"] = row["ASK"] = np.nan
                else:
                    p_trade = 0.55 * math.exp(-abs(math.log(K / s)) / 0.03)
                    if rng.random() < p_trade and ask > 0:
                        tr = round(max(0.01, (bid + ask) / 2 + rng.uniform(-spread / 2, spread / 2)), 2)
                        row.update(TRDPRC_1=tr, OPEN_PRC=tr, HIGH_1=tr + 0.01, LOW_1=max(0.01, tr - 0.01),
                                   ACVOL_UNS=float(rng.integers(1, 400)), NUM_MOVES=float(rng.integers(1, 40)))
                rows.append((ts, row))
            df = pd.DataFrame([r for _, r in rows], index=pd.DatetimeIndex([t for t, _ in rows], name="ts"))[FIELDS]
            df = df.dropna(how="all")
            if len(df) and df[["BID", "ASK"]].notna().any().any():
                options[ric] = df
                n_ok += 1
        weeks[F.isoformat()] = {"ref_spot": ref, "strikes_tried": len(strikes), "rics_ok": n_ok}

    meta = {
        "stock_ric": "AAPL.O", "root": "AAPL", "interval": "hourly",
        "expiries": [d.isoformat() for d in expiries],
        "fetched_at": "SYNTHETIC", "source": "SYNTHETIC (demo_data.py)", "demo": True,
        "weeks": weeks, "week_form": {d.isoformat(): "expired" for d in expiries},
    }
    return {"meta": meta, "stock": stock, "options": options, "tried": tried}
