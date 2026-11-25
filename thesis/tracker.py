#!/usr/bin/env python3
"""
Footprint Tracker - nightly job behind the FINTECH 535 final-project thesis.

Thesis
    People who know first trade first. They can hide in the *price* (big size,
    small move) but not in the *volume*, the odds on prediction markets, or in
    the way an asset suddenly stops moving with the things it normally moves
    with. When two or more of those footprints line up on linked assets, the
    move that follows is bigger than normal.

What this script does every night
    1. Daily data (2 years) for a cross-asset universe: indexes, rates, dollar,
       commodities, crypto, Mag 7, semis, energy, defense.
    2. Builds the "web": a correlation network (who moves with whom) and a
       lead-lag network (who moves *before* whom, a DAG-like set of arrows).
    3. Daily footprints per asset:
         quiet volume   volume way above normal while the price barely moves
         break          the asset's move disagrees with what its neighbors predict
         upstream       a leader in the lead-lag web had a footprint today
    4. Minute footprints (1-minute bars, last 7 days live / 30 days backfill):
         quiet bursts   a minute with huge volume and almost no price change,
                        then we look at what happened in the next 30/60 minutes.
         Bursts at scheduled times (data releases, settlements, opens/closes)
         are tagged "scheduled" and do not count - everyone knew about those.
    5. Prediction markets (Polymarket + Kalshi): odds that jump >= 8 points
       inside an hour on markets linked to the universe (Fed, Iran/oil,
       tariffs/China, Nvidia, ...).
    6. Confluence score per asset, the Footprint Index (how many assets light
       up each day), and a forward check: after flagged days, are the next
       1-day / 5-day moves actually bigger than normal?

Run
    python thesis/tracker.py            live data, writes thesis/data/*
    python thesis/tracker.py --demo     simulated data, writes to a temp folder
    python thesis/tracker.py --demo --out /some/folder
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import tempfile
import traceback
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import flow  # noqa: E402  (options flow + prediction-market wallets)
NY = ZoneInfo("America/New_York")

# ----------------------------------------------------------------------------
# Universe
# ----------------------------------------------------------------------------
# kind: "price" (use % change) or "yield" (use change in level)
# minute: also scan 1-minute bars for quiet bursts
UNIVERSE = [
    # Indexes and volatility
    {"sym": "SPY", "name": "S&P 500 ETF", "group": "Index", "minute": True},
    {"sym": "QQQ", "name": "Nasdaq 100 ETF", "group": "Index", "minute": True},
    {"sym": "IWM", "name": "Russell 2000 ETF", "group": "Index"},
    {"sym": "DIA", "name": "Dow ETF", "group": "Index"},
    {"sym": "ES=F", "name": "S&P 500 futures", "group": "Index", "minute": True},
    {"sym": "NQ=F", "name": "Nasdaq futures", "group": "Index", "minute": True},
    {"sym": "^VIX", "name": "VIX", "group": "Index"},
    # Rates and dollar
    {"sym": "^TNX", "name": "10Y yield", "group": "Rates", "kind": "yield"},
    {"sym": "^IRX", "name": "13W T-bill yield", "group": "Rates", "kind": "yield"},
    {"sym": "TLT", "name": "20Y+ Treasury ETF", "group": "Rates"},
    {"sym": "ZN=F", "name": "10Y note futures", "group": "Rates", "minute": True},
    {"sym": "DX-Y.NYB", "name": "US dollar index", "group": "Rates"},
    # Commodities
    {"sym": "CL=F", "name": "WTI crude", "group": "Commodity", "minute": True},
    {"sym": "BZ=F", "name": "Brent crude", "group": "Commodity", "minute": True},
    {"sym": "NG=F", "name": "Natural gas", "group": "Commodity", "minute": True},
    {"sym": "GC=F", "name": "Gold", "group": "Commodity", "minute": True},
    {"sym": "SI=F", "name": "Silver", "group": "Commodity", "minute": True},
    {"sym": "HG=F", "name": "Copper", "group": "Commodity"},
    {"sym": "USO", "name": "Oil ETF", "group": "Commodity", "minute": True},
    {"sym": "GLD", "name": "Gold ETF", "group": "Commodity"},
    {"sym": "SLV", "name": "Silver ETF", "group": "Commodity"},
    # Crypto
    {"sym": "BTC-USD", "name": "Bitcoin", "group": "Crypto"},
    # Mag 7
    {"sym": "AAPL", "name": "Apple", "group": "Mag 7"},
    {"sym": "MSFT", "name": "Microsoft", "group": "Mag 7", "minute": True},
    {"sym": "NVDA", "name": "Nvidia", "group": "Mag 7", "minute": True},
    {"sym": "GOOGL", "name": "Alphabet", "group": "Mag 7"},
    {"sym": "AMZN", "name": "Amazon", "group": "Mag 7"},
    {"sym": "META", "name": "Meta", "group": "Mag 7"},
    {"sym": "TSLA", "name": "Tesla", "group": "Mag 7", "minute": True},
    # Semis (Nvidia's web)
    {"sym": "TSM", "name": "TSMC", "group": "Semis", "minute": True},
    {"sym": "MU", "name": "Micron", "group": "Semis", "minute": True},
    {"sym": "AMD", "name": "AMD", "group": "Semis"},
    {"sym": "AVGO", "name": "Broadcom", "group": "Semis"},
    {"sym": "ASML", "name": "ASML", "group": "Semis"},
    {"sym": "SMH", "name": "Semis ETF", "group": "Semis"},
    # Energy, defense, airlines (who reacts to oil / war headlines)
    {"sym": "XLE", "name": "Energy ETF", "group": "Energy"},
    {"sym": "XOM", "name": "Exxon", "group": "Energy"},
    {"sym": "CVX", "name": "Chevron", "group": "Energy"},
    {"sym": "ITA", "name": "Defense ETF", "group": "Defense"},
    {"sym": "LMT", "name": "Lockheed Martin", "group": "Defense"},
    {"sym": "JETS", "name": "Airlines ETF", "group": "Energy"},
]
SYMS = [u["sym"] for u in UNIVERSE]
META = {u["sym"]: u for u in UNIVERSE}
TRADEABLE = ["TSLA", "NVDA", "MSFT"]
SCHEMA = 2      # bump when a signal definition changes; older archive rows are dropped     # what the course server prices today

# ----------------------------------------------------------------------------
# Settings (everything the thesis depends on, in one place)
# ----------------------------------------------------------------------------
CFG = {
    "history_days": 520,        # ~2 years of daily bars
    "vol_window": 20,           # "normal" volume / volatility lookback
    "quiet_vol_z": 2.0,         # volume this many std above normal...
    "quiet_ret_z": 1.0,         # ...while the move is under this many std
    "net_window": 60,           # correlation window for the network
    "edge_corr": 0.55,          # draw an edge above this |corr|
    "neighbors": 4,             # neighbors used to predict an asset's move
    "break_z": 2.5,             # residual this many std = "break from the pack"
    "leadlag_window": 250,
    "leadlag_corr": 0.20,       # ~3 std with 250 obs (41x40 pairs tested, so keep it strict)
    "leadlag_max_in": 2,        # at most this many leaders per asset
    "flag_score": 2.0,          # confluence score to count as a flagged asset
    # minute bursts
    "m_base": 120,              # bars for the typical 1-minute move
    "m_days": 10,               # volume baseline = same minute-of-day over the last 10 sessions
    "m_min_days": 3,
    "m_vol_z": 4.0,             # robust volume z
    "m_ret_z": 1.5,             # |1-min move| under this many std
    "m_min_mult": 8.0,          # and at least 8x the median minute volume
    "m_fwd": [15, 30, 60],      # minutes to look ahead
    "m_follow": 2.0,            # forward move >= 2x a normal move = "followed through"
    "m_merge": 10,              # merge bursts within 10 minutes
    "m_sched_pad": 3,           # minutes around scheduled times
    # prediction markets
    "pm_jump": 0.08,            # 8 points inside the window
    "pm_window_min": 60,
    "pm_min_vol24": 25_000.0,
}

# Scheduled times (New York clock) when big volume is expected and public.
# Volume here is not a secret, so it is not a footprint.
SCHEDULED_ALL = ["08:30", "10:00", "14:00", "18:00"]           # data, FOMC, Globex reopen
SCHEDULED_BY = {
    "equity": ["09:30", "15:50", "15:55", "16:00"],          # open, MOC imbalance, close
    "CL=F": ["10:30", "14:30", "09:00"], "BZ=F": ["10:30", "14:30", "09:00"],
    "USO": ["10:30", "14:30"], "NG=F": ["10:30", "14:30", "09:00"],
    "GC=F": ["13:30", "08:20"], "SI=F": ["13:25", "08:25"],
    "ES=F": ["09:30", "16:00", "17:00"], "NQ=F": ["09:30", "16:00", "17:00"],
    "ZN=F": ["15:00", "08:20", "13:00"],
}

# Prediction-market keywords -> assets they should move
PM_MAP = [
    (r"\b(fed|fomc|rate cut|rate hike|interest rate|powell|warsh|hassett|basis points?|bps)\b", ["^TNX", "^IRX", "TLT", "SPY"]),
    (r"\b(iran|israel|hormuz|opec|oil|crude|saudi|houthi|venezuela|tehran)\b", ["CL=F", "BZ=F", "USO", "XLE", "JETS"]),
    (r"(?<!counter-)\b(ceasefire|airstrikes?|strikes? on|military strike|war|military|invade|invasion|missile)\b", ["CL=F", "GC=F", "ITA", "LMT"]),
    (r"\b(russia|ukraine|putin|zelensky)\b", ["NG=F", "CL=F", "GC=F", "ITA"]),
    (r"\b(tariffs?|trade deal|china|xi|taiwan|export controls?)\b", ["SPY", "QQQ", "TSM", "NVDA", "AAPL"]),
    (r"\b(nvidia|nvda|jensen|huang)\b", ["NVDA", "SMH", "TSM", "MU"]),
    (r"\b(tesla|musk|tsla)\b", ["TSLA"]),
    (r"\b(apple|aapl)\b", ["AAPL"]),
    (r"\b(microsoft|msft|openai)\b", ["MSFT"]),
    (r"\b(google|alphabet)\b", ["GOOGL"]),
    (r"\b(amazon)\b", ["AMZN"]),
    (r"\b(meta|zuckerberg)\b", ["META"]),
    (r"\b(bitcoin|btc|crypto)\b", ["BTC-USD"]),
    (r"\b(gold)\b", ["GC=F", "GLD"]),
    (r"\b(recession|gdp|inflation|cpi|jobs report|unemployment|payrolls)\b", ["SPY", "TLT", "^TNX"]),
    (r"\b(shutdown|debt ceiling|default)\b", ["TLT", "SPY", "^IRX"]),
]
KALSHI_SERIES = ["KXFED", "KXFEDDECISION", "KXCPI", "KXRECSSNBER", "KXWTI", "KXGOLDD"]


# ----------------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------------
def _f(x, nd=4):
    """JSON-safe float."""
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(x):
        return None
    return round(x, nd)


def _num(d: dict, *keys):
    for k in keys:
        v = d.get(k)
        if v is None or v == "":
            continue
        try:
            return float(v)
        except (TypeError, ValueError):
            continue
    return None


def is_equity(sym: str) -> bool:
    return not (sym.endswith("=F") or sym.startswith("^") or "-" in sym or "." in sym)


# ----------------------------------------------------------------------------
# Data: live (yfinance) and demo (simulated)
# ----------------------------------------------------------------------------
def live_daily(days: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    import yfinance as yf
    start = (datetime.now(timezone.utc) - timedelta(days=int(days * 1.5))).date().isoformat()
    raw = yf.download(SYMS, start=start, interval="1d", auto_adjust=True,
                      progress=False, group_by="column", threads=True)
    close = raw["Close"].copy()
    vol = raw["Volume"].copy()
    close.index = pd.to_datetime(close.index).tz_localize(None).normalize()
    vol.index = close.index
    return close, vol


def live_minutes(syms: list[str], days: int = 7) -> dict[str, pd.DataFrame]:
    """1-minute bars. Yahoo keeps ~30 days of 1m data, max 8 days per request."""
    import yfinance as yf
    out = {}
    end = datetime.now(timezone.utc)
    for s in syms:
        frames = []
        left = days
        cur_end = end
        while left > 0:
            span = min(7, left)
            cur_start = cur_end - timedelta(days=span)
            try:
                df = yf.download(s, start=cur_start, end=cur_end, interval="1m",
                                 auto_adjust=False, progress=False, prepost=True)
                if isinstance(df.columns, pd.MultiIndex):
                    df.columns = df.columns.get_level_values(0)
                if len(df):
                    frames.append(df[["Open", "High", "Low", "Close", "Volume"]])
            except Exception as e:                                  # noqa: BLE001
                print(f"  minute {s}: {e}", file=sys.stderr)
            cur_end = cur_start
            left -= span
        if frames:
            df = pd.concat(frames).sort_index()
            df = df[~df.index.duplicated(keep="last")]
            idx = pd.to_datetime(df.index)
            idx = idx.tz_localize("UTC") if idx.tz is None else idx
            df.index = idx.tz_convert(NY)
            out[s] = df
    return out


def demo_daily(days: int, seed: int = 7) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Factor model with a few planted footprints."""
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range(end=pd.Timestamp("2026-11-24"), periods=days)
    n = len(dates)
    f_mkt, f_rate, f_oil, f_gold, f_semi = (rng.normal(0, s, n) for s in (0.01, 0.006, 0.02, 0.009, 0.015))
    load = {
        "Index": (1.0, -0.2, 0.05, 0, 0.2), "Rates": (-0.3, 1.0, 0, 0.1, 0),
        "Commodity": (0.2, 0, 1.0, 0, 0), "Crypto": (1.5, -0.3, 0, 0.2, 0),
        "Mag 7": (1.1, -0.2, 0, 0, 0.5), "Semis": (1.2, -0.2, 0, 0, 1.0),
        "Energy": (0.6, 0, 0.7, 0, 0), "Defense": (0.6, 0, 0.2, 0.2, 0),
    }
    F = np.vstack([f_mkt, f_rate, f_oil, f_gold, f_semi])
    rets, vols = {}, {}
    for u in UNIVERSE:
        b = np.array(load[u["group"]], float)
        if u["sym"] in ("GC=F", "GLD", "SI=F", "SLV"):
            b = np.array((0.1, 0.2, 0.1, 1.0, 0))
        if u["sym"] == "^VIX":
            b = np.array((-4.0, 0, 0, 0, 0))
        if u["sym"] == "JETS":
            b = np.array((1.0, 0, -0.6, 0, 0))
        r = b @ F + rng.normal(0, 0.008, n)
        v = np.exp(rng.normal(15, 0.25, n))
        rets[u["sym"]] = r
        vols[u["sym"]] = v
    R = pd.DataFrame(rets, index=dates)
    V = pd.DataFrame(vols, index=dates)
    # planted footprints: quiet volume a few days before a big move
    for sym, day, move in [("CL=F", n - 40, -0.09), ("NVDA", n - 15, -0.07), ("TSM", n - 15, -0.04),
                           ("GC=F", n - 70, 0.05), ("CL=F", n - 3, 0.0), ("BZ=F", n - 3, 0.0),
                           ("MU", n - 1, 0.0), ("NVDA", n - 1, 0.0)]:
        V.iloc[day - 2: day, V.columns.get_loc(sym)] *= 4.0
        R.iloc[day - 2: day, R.columns.get_loc(sym)] *= 0.2
        if move:
            R.iloc[day, R.columns.get_loc(sym)] += move
    R.iloc[-1, R.columns.get_loc("TSM")] += 0.045          # TSM breaks from its pack today
    # a readable last day for the web demo: chips sell off, money rotates NVDA -> AMD, oil and gold bid
    last = {"SPY": -0.008, "QQQ": -0.013, "ES=F": -0.008, "NQ=F": -0.013, "IWM": -0.004, "DIA": -0.005,
            "NVDA": -0.045, "AMD": 0.028, "MU": -0.035, "AVGO": -0.025, "SMH": -0.03, "ASML": -0.02,
            "AAPL": -0.006, "MSFT": -0.009, "META": -0.012, "GOOGL": -0.01, "AMZN": -0.007, "TSLA": -0.02,
            "XLE": 0.02, "XOM": 0.017, "CVX": 0.015, "JETS": -0.025, "GC=F": 0.012, "GLD": 0.012,
            "SI=F": 0.015, "SLV": 0.015, "TLT": 0.006, "ZN=F": 0.004, "^VIX": 0.09, "ITA": 0.011, "LMT": 0.014}
    for k, v in last.items():
        R.iloc[-1, R.columns.get_loc(k)] = v
    for k in ("NVDA", "AMD"):
        V.iloc[-1, V.columns.get_loc(k)] *= 2.5
    C = 100 * np.exp(R.cumsum())
    for s in ("^TNX", "^IRX"):
        C[s] = 4.2 + R[s].cumsum() * 5
        V[s] = np.nan
    V["^VIX"] = np.nan
    return C, V


def demo_minutes(syms: list[str], seed: int = 11) -> dict[str, pd.DataFrame]:
    rng = np.random.default_rng(seed)
    out = {}
    end = pd.Timestamp("2026-11-24 16:59", tz=NY)
    idx = pd.date_range(end=end, periods=7 * 1380, freq="1min")
    for k, s in enumerate(syms):
        r = rng.normal(0, 0.0004, len(idx))
        v = np.exp(rng.normal(6, 0.5, len(idx)))
        if s in ("CL=F", "BZ=F", "USO"):            # oil-style: big quiet sell, then the drop
            i = len(idx) - 1380 * 2 + 600
            v[i:i + 3] *= 40
            r[i:i + 3] = -0.0003
            r[i + 15:i + 18] = -0.012
        if s == "NVDA":                              # a burst at the close: scheduled, ignored
            i = int(np.where((idx.hour == 16) & (idx.minute == 0))[0][-1])
            v[i] *= 50
        if s == "GC=F":                              # a burst that goes nowhere: noise
            i = len(idx) - 1380 * 4 + 200
            v[i] *= 30
        c = 100 * np.exp(np.cumsum(r))
        o = np.r_[c[0], c[:-1]]
        out[s] = pd.DataFrame({"Open": o, "High": np.maximum(o, c), "Low": np.minimum(o, c),
                               "Close": c, "Volume": v}, index=idx)
    return out


def demo_polymarket() -> list[dict]:
    now = int(datetime(2026, 11, 24, 21, tzinfo=timezone.utc).timestamp())
    hist_flat = [{"t": now - 600 * i, "p": 0.31} for i in range(144, 0, -1)]
    hist_jump = [{"t": now - 600 * i, "p": 0.22 if i > 30 else 0.41} for i in range(144, 0, -1)]
    return [
        {"question": "Will Iran and the US announce a nuclear deal by December 31?", "slug": "iran-us-deal",
         "volume24hr": 410000, "outcomePrices": '["0.41","0.59"]', "outcomes": '["Yes","No"]',
         "_history": hist_jump},
        {"question": "Fed decision in December: 25 bps cut?", "slug": "fed-dec",
         "volume24hr": 2_300_000, "outcomePrices": '["0.31","0.69"]', "outcomes": '["Yes","No"]',
         "_history": hist_flat},
        {"question": "Will Jensen Huang return as Nvidia CEO in 2026?", "slug": "huang-return", "conditionId": "0xhuang",
         "volume24hr": 95000, "outcomePrices": '["0.12","0.88"]', "outcomes": '["Yes","No"]',
         "_history": [{"t": h["t"], "p": 0.12} for h in hist_flat]},
    ]


# ----------------------------------------------------------------------------
# Prediction markets (live)
# ----------------------------------------------------------------------------
def _get(url, params=None, timeout=20):
    import requests
    r = requests.get(url, params=params, timeout=timeout, headers={"User-Agent": "fintech535-thesis"})
    r.raise_for_status()
    return r.json()


def live_polymarket(max_markets: int = 40) -> list[dict]:
    mk = []
    for off in (0, 500):
        try:
            mk += _get("https://gamma-api.polymarket.com/markets",
                       {"active": "true", "closed": "false", "limit": 500, "offset": off,
                        "order": "volume24hr", "ascending": "false"})
        except Exception as e:                                       # noqa: BLE001
            print(f"  gamma page {off}: {e}", file=sys.stderr)
    picked = []
    for m in mk:
        if not pm_assets(m.get("question", "")):
            continue
        if (_num(m, "volume24hr") or 0) < CFG["pm_min_vol24"]:
            continue
        picked.append(m)
        if len(picked) >= max_markets:
            break
    for m in picked:
        try:
            tok = json.loads(m.get("clobTokenIds") or "[]")
            if tok:
                h = _get("https://clob.polymarket.com/prices-history",
                         {"market": tok[0], "interval": "1d", "fidelity": 10})
                m["_history"] = h.get("history", [])
        except Exception as e:                                       # noqa: BLE001
            m["_history"] = []
            print(f"  polymarket history {m.get('slug')}: {e}", file=sys.stderr)
    return picked


def live_kalshi() -> list[dict]:
    rows = []
    for series in KALSHI_SERIES:
        try:
            d = _get("https://api.elections.kalshi.com/trade-api/v2/markets",
                     {"series_ticker": series, "status": "open", "limit": 100})
        except Exception as e:                                       # noqa: BLE001
            print(f"  kalshi {series}: {e}", file=sys.stderr)
            continue
        for m in d.get("markets", []):
            last = _num(m, "last_price_dollars")
            if last is None:
                last = _num(m, "last_price")
                last = last / 100 if last is not None else None
            prev = _num(m, "previous_price_dollars")
            if prev is None:
                prev = _num(m, "previous_price")
                prev = prev / 100 if prev is not None else None
            v24 = _num(m, "volume_24h_fp", "volume_24h") or 0
            if last is None:
                continue
            title = (m.get("title") or "") + " " + (m.get("yes_sub_title") or m.get("subtitle") or "")
            rows.append({"series": series, "ticker": m.get("ticker"), "title": title.strip(),
                         "last": last, "prev": prev, "vol24": v24})
    return rows


def pm_assets(text: str) -> list[str]:
    t = (text or "").lower()
    out = []
    for pat, syms in PM_MAP:
        if re.search(pat, t):
            out += [s for s in syms if s not in out]
    return out


def pm_skip(m: dict, price) -> bool:
    """Not a tip: markets settling (odds pinned near 0/1), resolving within 2 days, or sports/esports."""
    if flow.topic(m.get("question", "")) == "Sports":
        return True
    if price is not None and (price >= 0.97 or price <= 0.03):
        return True
    end = m.get("endDate") or m.get("end_date_iso")
    try:
        if end and (datetime.fromisoformat(str(end).replace("Z", "+00:00")) - datetime.now(timezone.utc)).total_seconds() < 2 * 86400:
            return True
    except ValueError:
        pass
    return bool(re.search(r"\bon (january|february|march|april|may|june|july|august|september|october|november|december) \d", (m.get("question") or "").lower()))


def scan_polymarket(markets: list[dict]) -> list[dict]:
    """Odds that jump >= pm_jump inside pm_window_min, after a quiet stretch."""
    rows = []
    win = CFG["pm_window_min"] * 60
    for m in markets:
        h = sorted(m.get("_history") or [], key=lambda x: x["t"])
        try:
            price = float(json.loads(m.get("outcomePrices") or "[]")[0])
        except Exception:                                            # noqa: BLE001
            price = h[-1]["p"] if h else None
        best = {"jump": 0.0, "t": None, "from": None, "to": None}
        ts = np.array([x["t"] for x in h], float)
        ps = np.array([x["p"] for x in h], float)
        for i in range(len(ts)):
            j = np.searchsorted(ts, ts[i] - win)
            if j >= i:
                continue
            k = j + int(np.argmax(np.abs(ps[i] - ps[j:i])))
            d = ps[i] - ps[k]
            if abs(d) > abs(best["jump"]):
                best = {"jump": float(d), "t": int(ts[i]), "from": float(ps[k]), "to": float(ps[i])}
        quiet_before = None
        if best["t"] is not None:
            pre = ps[(ts < best["t"] - win) & (ts >= best["t"] - win - 6 * 3600)]
            quiet_before = float(np.std(pre)) if len(pre) > 3 else None
        flagged = abs(best["jump"]) >= CFG["pm_jump"] and not pm_skip(m, price)
        rows.append({
            "source": "Polymarket", "question": m.get("question"), "slug": m.get("slug"),
            "price": _f(price, 3), "vol24": _f(_num(m, "volume24hr"), 0),
            "jump": _f(best["jump"], 3), "jump_from": _f(best["from"], 3), "jump_to": _f(best["to"], 3),
            "jump_time": datetime.fromtimestamp(best["t"], NY).strftime("%Y-%m-%d %H:%M") if best["t"] else None,
            "quiet_before": _f(quiet_before, 3), "flagged": bool(flagged),
            "assets": pm_assets(m.get("question", "")),
        })
    rows.sort(key=lambda r: (not r["flagged"], -(abs(r["jump"] or 0))))
    return rows


def scan_kalshi(rows: list[dict]) -> list[dict]:
    out = []
    for r in rows:
        jump = (r["last"] - r["prev"]) if r.get("prev") is not None else None
        flagged = jump is not None and abs(jump) >= CFG["pm_jump"] and r["vol24"] > 0
        out.append({"source": "Kalshi", "question": r["title"], "slug": r["ticker"],
                    "price": _f(r["last"], 3), "vol24": _f(r["vol24"], 0), "jump": _f(jump, 3),
                    "jump_time": None, "flagged": bool(flagged),
                    "assets": pm_assets(r["title"] + " " + r["series"].replace("KX", ""))
                    or (["^TNX", "TLT"] if "FED" in r["series"] else [])})
    out.sort(key=lambda r: (not r["flagged"], -(abs(r["jump"] or 0))))
    return out


# ----------------------------------------------------------------------------
# Daily footprints and the web
# ----------------------------------------------------------------------------
def daily_returns(close: pd.DataFrame) -> pd.DataFrame:
    close = close.dropna(how="all")
    out = {}
    for s in close.columns:
        c = close[s].ffill()
        if META.get(s, {}).get("kind") == "yield":
            out[s] = c.diff() / 10.0           # in percentage points, roughly comparable size
        else:
            out[s] = np.log(c).diff()
    R = pd.DataFrame(out)
    # keep NYSE days (SPY trades) so crypto/futures line up with stocks
    if "SPY" in close:
        R = R.loc[close["SPY"].dropna().index.intersection(R.index)]
    return R.iloc[1:]


def daily_footprints(R: pd.DataFrame, V: pd.DataFrame) -> dict[str, pd.DataFrame]:
    w = CFG["vol_window"]
    sd = R.rolling(w, min_periods=w // 2).std().shift(1)
    rz = R / sd
    V = V.reindex(R.index)
    lv = np.log(V.where(V > 0))
    vz = (lv - lv.rolling(w, min_periods=w // 2).mean().shift(1)) / lv.rolling(w, min_periods=w // 2).std().shift(1)
    quiet = (vz >= CFG["quiet_vol_z"]) & (rz.abs() <= CFG["quiet_ret_z"])

    # break from the pack: predict each asset from its closest neighbors
    nw, k = CFG["net_window"], CFG["neighbors"]
    Z = rz.clip(-8, 8)
    brk = pd.DataFrame(np.nan, index=R.index, columns=R.columns)
    Rv = R.values
    cols = list(R.columns)
    for t in range(nw + w, len(R)):
        win = Rv[t - nw:t]
        good = ~np.isnan(win).any(axis=0)
        if good.sum() < k + 2:
            continue
        C = np.corrcoef(win[:, good].T)
        gcols = np.where(good)[0]
        for a_pos, a in enumerate(gcols):
            if np.isnan(Rv[t, a]):
                continue
            order = np.argsort(-np.abs(C[a_pos]))
            nb = [gcols[j] for j in order if gcols[j] != a][:k]
            nb = [j for j in nb if not np.isnan(Rv[t, j])]
            if len(nb) < 2:
                continue
            X = np.c_[np.ones(nw), win[:, nb]]
            y = win[:, a]
            beta, *_ = np.linalg.lstsq(X, y, rcond=None)
            resid = y - X @ beta
            s = resid.std(ddof=len(nb) + 1)
            if s <= 0:
                continue
            pred = beta[0] + Rv[t, nb] @ beta[1:]
            brk.iat[t, a] = (Rv[t, a] - pred) / s
    return {"rz": rz, "vz": vz, "quiet": quiet, "brk": brk, "Z": Z}


def build_network(R: pd.DataFrame) -> dict:
    win = R.iloc[-CFG["net_window"]:].dropna(axis=1, thresh=int(CFG["net_window"] * 0.8))
    C = win.corr()
    edges = []
    cols = list(C.columns)
    for i, a in enumerate(cols):
        for b in cols[i + 1:]:
            c = C.loc[a, b]
            if np.isfinite(c) and abs(c) >= CFG["edge_corr"]:
                edges.append({"a": a, "b": b, "corr": _f(c, 3)})
    # lead-lag arrows: yesterday's move in A vs today's move in B
    L = R.iloc[-CFG["leadlag_window"]:]
    lead = []
    for b in L.columns:
        cands = []
        yb = L[b]
        for a in L.columns:
            if a == b:
                continue
            xa = L[a].shift(1)
            m = xa.notna() & yb.notna()
            if m.sum() < 120:
                continue
            c = np.corrcoef(xa[m], yb[m])[0, 1]
            if np.isfinite(c) and abs(c) >= CFG["leadlag_corr"]:
                cands.append((abs(c), a, c))
        cands.sort(reverse=True)
        for _, a, c in cands[:CFG["leadlag_max_in"]]:
            lead.append({"from": a, "to": b, "corr": _f(c, 3)})
    return {"edges": edges, "leadlag": lead, "corr_window": CFG["net_window"]}


def score_days(fp: dict, leadlag: list[dict]) -> pd.DataFrame:
    quiet = fp["quiet"].astype(float)
    brk = (fp["brk"].abs() >= CFG["break_z"]).astype(float)
    base = quiet + brk
    up = pd.DataFrame(0.0, index=base.index, columns=base.columns)
    for e in leadlag:
        if e["from"] in base and e["to"] in up:
            up[e["to"]] = np.maximum(up[e["to"]], (base[e["from"]].shift(0) >= 1).astype(float) * 0.5)
    return base + up


def forward_check(R: pd.DataFrame, fp: dict, score: pd.DataFrame) -> dict:
    """Are moves after flagged days bigger than normal? (the live alpha check)"""
    sd = R.rolling(CFG["vol_window"], min_periods=10).std().shift(1)
    f1 = (R.shift(-1) / sd)
    f5 = (R.rolling(5).sum().shift(-5) / (sd * np.sqrt(5)))
    flag = score >= CFG["flag_score"]
    any_fp = score >= 1.0
    out = {}
    for name, mask in (("flagged", flag), ("any_footprint", any_fp)):
        m1 = f1.where(mask).stack().abs()
        m5 = f5.where(mask).stack().abs()
        out[name] = {"n": int(m5.notna().sum()), "abs_fwd1": _f(m1.mean(), 3), "abs_fwd5": _f(m5.mean(), 3)}
    base1 = f1.stack().abs().mean()
    base5 = f5.stack().abs().mean()
    out["baseline"] = {"abs_fwd1": _f(base1, 3), "abs_fwd5": _f(base5, 3)}
    out["move_ratio_5d"] = _f((out["flagged"]["abs_fwd5"] or np.nan) / base5, 3) if out["flagged"]["n"] else None
    out["move_ratio_1d"] = _f((out["flagged"]["abs_fwd1"] or np.nan) / base1, 3) if out["flagged"]["n"] else None
    # direction: after a break, does the move continue (informed) or snap back (noise)?
    b = fp["brk"]
    bmask = b.abs() >= CFG["break_z"]
    cont = (np.sign(b) * np.sign(R.rolling(5).sum().shift(-5))).where(bmask).stack().dropna()
    cont = cont[cont != 0]
    out["break_continue_rate"] = _f((cont > 0).mean(), 3) if len(cont) else None
    out["break_n"] = int(len(cont))
    return out


# ----------------------------------------------------------------------------
# Minute footprints
# ----------------------------------------------------------------------------
def _sched_set(sym: str) -> set[int]:
    times = list(SCHEDULED_ALL)
    times += SCHEDULED_BY.get(sym, [])
    if is_equity(sym):
        times += SCHEDULED_BY["equity"]
    out = set()
    pad = CFG["m_sched_pad"]
    for t in times:
        h, m = map(int, t.split(":"))
        for d in range(-pad, pad + 1):
            out.add((h * 60 + m + d) % 1440)
    return out


def scan_minutes(bars: dict[str, pd.DataFrame]) -> list[dict]:
    events = []
    B = CFG["m_base"]
    for sym, df in bars.items():
        df = df.dropna(subset=["Close"])
        df = df[df["Volume"] > 0]
        if len(df) < B + 70:
            continue
        c = df["Close"].astype(float)
        v = df["Volume"].astype(float)
        r = np.log(c).diff()
        lv = np.log1p(v)
        # volume has a strong time-of-day shape (thin pre-market, heavy open/close), so compare each minute
        # with the SAME minute on prior sessions, not with the minutes just before it
        mod = pd.Series(df.index.hour * 60 + df.index.minute, index=df.index)
        roll = lambda x: x.shift(1).rolling(CFG["m_days"], min_periods=CFG["m_min_days"]).median()   # noqa: E731
        med = lv.groupby(mod).transform(roll)
        dev = (lv - med).abs()
        mad = dev.groupby(mod).transform(roll).fillna(dev.expanding(min_periods=30).median().shift(1)).clip(lower=0.25) * 1.4826
        vz = (lv - med) / mad.replace(0, np.nan)
        mult = v / np.expm1(med)
        rsd = r.rolling(B, min_periods=B // 2).std().shift(1)
        rz = r / rsd
        hit = (vz >= CFG["m_vol_z"]) & (mult >= CFG["m_min_mult"]) & (rz.abs() <= CFG["m_ret_z"])
        sched = _sched_set(sym)
        last_t = None
        idx = df.index
        cv = c.values
        for i in np.where(hit.fillna(False).values)[0]:
            t = idx[i]
            if last_t is not None and (t - last_t) <= pd.Timedelta(minutes=CFG["m_merge"]):
                continue
            last_t = t
            mod = t.hour * 60 + t.minute
            fw = {}
            for h in CFG["m_fwd"]:
                j = idx.searchsorted(t + pd.Timedelta(minutes=h))
                if j < len(idx) and (idx[min(j, len(idx) - 1)] - t) <= pd.Timedelta(minutes=h + 30):
                    path = np.log(cv[i + 1:j + 1] / cv[i]) if j > i else np.array([])
                    if len(path):
                        k = int(np.argmax(np.abs(path)))
                        fw[h] = float(path[k])
            typ = float(rsd.iloc[i] * np.sqrt(30)) if np.isfinite(rsd.iloc[i]) else None
            f30 = fw.get(30)
            follow = (abs(f30) / typ) if (f30 is not None and typ) else None
            bar_dir = float(np.sign(cv[i] - df["Open"].values[i])) or float(np.sign(r.iloc[i] or 0))
            events.append({
                "sym": sym, "time": t.strftime("%Y-%m-%d %H:%M"), "ts": int(t.timestamp()),
                "vol_mult": _f(mult.iloc[i], 1), "vol_z": _f(vz.iloc[i], 1), "ret_z": _f(rz.iloc[i], 2),
                "fwd15": _f(fw.get(15), 5), "fwd30": _f(f30, 5), "fwd60": _f(fw.get(60), 5),
                "follow": _f(follow, 2),
                "followed": bool(follow is not None and follow >= CFG["m_follow"]),
                "same_dir": (bool(np.sign(f30) == bar_dir) if (f30 and bar_dir) else None),
                "scheduled": mod in sched,
                "pending": f30 is None,
            })
    # network bursts: linked markets bursting within 5 minutes of each other
    events.sort(key=lambda e: e["ts"])
    for e in events:
        e["cluster"] = sorted({x["sym"] for x in events
                               if abs(x["ts"] - e["ts"]) <= 300 and not x["scheduled"]})
    return events


def merge_events(old: list[dict], new: list[dict], cap: int = 6000) -> list[dict]:
    by = {(e["sym"], e["ts"]): e for e in old}
    for e in new:
        prev = by.get((e["sym"], e["ts"]))
        if prev is None or prev.get("pending") or not e.get("pending"):
            by[(e["sym"], e["ts"])] = e
    out = sorted(by.values(), key=lambda e: e["ts"])
    return out[-cap:]


def minute_stats(events: list[dict]) -> dict:
    real = [e for e in events if not e["scheduled"] and not e["pending"]]
    sched = [e for e in events if e["scheduled"] and not e["pending"]]
    f = lambda L: (sum(e["followed"] for e in L) / len(L)) if L else None   # noqa: E731
    sd = [e["same_dir"] for e in real if e["same_dir"] is not None and e["followed"]]
    clustered = [e for e in real if len(e.get("cluster", [])) >= 2]
    return {
        "n_unscheduled": len(real), "n_scheduled": len(sched),
        "follow_rate_unscheduled": _f(f(real), 3), "follow_rate_scheduled": _f(f(sched), 3),
        "follow_rate_clustered": _f(f(clustered), 3), "n_clustered": len(clustered),
        "same_dir_rate": _f(sum(sd) / len(sd), 3) if sd else None,
        "first": real[0]["time"] if real else None, "last": real[-1]["time"] if real else None,
    }


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def load_json(path, default):
    try:
        with open(path) as fh:
            return json.load(fh)
    except Exception:                                                # noqa: BLE001
        return default


def run(demo: bool, out_dir: str, minute_days: int) -> dict:
    os.makedirs(out_dir, exist_ok=True)
    notes = []
    print("daily data ...")
    close, vol = demo_daily(CFG["history_days"]) if demo else live_daily(CFG["history_days"])
    R = daily_returns(close)
    R = R.iloc[-CFG["history_days"]:]
    fp = daily_footprints(R, vol)
    net = build_network(R)
    score = score_days(fp, net["leadlag"])

    print("minute data ...")
    msyms = [u["sym"] for u in UNIVERSE if u.get("minute")]
    old_events = [e for e in load_json(os.path.join(out_dir, "events.json"), []) if e.get("v") == SCHEMA]
    days = minute_days if (minute_days or not old_events) else 12
    days = days or (28 if not old_events else 12)
    try:
        bars = demo_minutes(msyms) if demo else live_minutes(msyms, days)
        new_events = scan_minutes(bars)
    except Exception as e:                                           # noqa: BLE001
        traceback.print_exc()
        new_events, bars = [], {}
        notes.append(f"minute scan failed: {e}")
    for e in new_events:
        e["v"] = SCHEMA
    events = merge_events(old_events, new_events)

    print("prediction markets ...")
    pm_rows, pm_markets = [], []
    try:
        pm_markets = demo_polymarket() if demo else live_polymarket()
        pm_rows += scan_polymarket(pm_markets)
    except Exception as e:                                           # noqa: BLE001
        traceback.print_exc()
        notes.append(f"Polymarket failed: {e}")
    if not demo:
        try:
            pm_rows += scan_kalshi(live_kalshi())
        except Exception as e:                                       # noqa: BLE001
            notes.append(f"Kalshi failed: {e}")
    # ---- options flow (new positions in near-term contracts) ----
    print("options flow ...")
    asof_s = R.index[-1].strftime("%Y-%m-%d")
    day_ret = {s: float(R[s].iloc[-1]) for s in R.columns if np.isfinite(R[s].iloc[-1])}
    opt_log = [x for x in load_json(os.path.join(out_dir, "options_log.json"), []) if x.get("v") == SCHEMA]
    try:
        chains = flow.demo_chains() if demo else flow.live_chains()
        rz_now = {s: float(fp["rz"][s].iloc[-1]) for s in R.columns if np.isfinite(fp["rz"][s].iloc[-1])}
        vlast = vol.reindex(R.index).iloc[-1]
        clast = close.reindex(R.index).iloc[-1]
        stock_usd = {s: float(vlast[s] * clast[s]) for s in chains if s in vlast and np.isfinite(vlast[s] * clast[s]) and vlast[s] > 0}
        earn = {} if demo else flow.earnings_dates([s for s in chains if is_equity(s) and s not in ("SPY", "QQQ")])
        if demo and not opt_log:      # demo: pretend each name already has two weeks of quiet history
            rng_ = np.random.default_rng(5)
            for s_, (df_, _) in chains.items():
                for k in range(10, 0, -1):
                    d_ = (R.index[-1] - pd.tseries.offsets.BDay(k)).strftime("%Y-%m-%d")
                    opt_log.append({"date": d_, "sym": s_, "v": SCHEMA, "unusual_prem": float(rng_.uniform(5e5, 1.5e6)),
                                    "total_volume": int(pd.to_numeric(df_["volume"]).sum() * rng_.uniform(0.85, 1.15))})
        opts, opt_log = flow.options_footprints(chains, day_ret, opt_log, asof_s, rz_now, stock_usd, earn)
    except Exception as e:                                           # noqa: BLE001
        traceback.print_exc()
        opts = {}
        notes.append(f"options scan failed: {e}")

    # ---- who is betting: out-of-place wallets on Polymarket, big-trade bursts on Kalshi ----
    print("prediction-market wallets ...")
    now_ts = int(datetime(2026, 11, 24, 21, tzinfo=timezone.utc).timestamp()) if demo else int(datetime.now(timezone.utc).timestamp())
    wallets, kb = [], []
    try:
        if demo:
            wallets = flow.demo_wallets(now_ts)
        else:
            linked = [m for m in pm_markets if pm_assets(m.get("question", "")) and flow.topic(m.get("question", "")) != "Sports"]
            linked.sort(key=lambda m: -(_num(m, "volume24hr") or 0))
            wallets = flow.live_wallets(linked[:15], now_ts)
    except Exception as e:                                           # noqa: BLE001
        traceback.print_exc()
        notes.append(f"wallet scan failed: {e}")
    if not demo:
        try:
            tick = [(r["slug"], r["question"]) for r in pm_rows if r["source"] == "Kalshi" and (r["vol24"] or 0) > 0]
            kb = flow.kalshi_bursts(tick[:15], now_ts)
        except Exception as e:                                       # noqa: BLE001
            notes.append(f"Kalshi trades failed: {e}")
    for w in wallets:
        w["assets"] = pm_assets(w["question"] or "")
    for k in kb:
        k["assets"] = pm_assets(k["question"] or "")
    wallet_log = [x for x in load_json(os.path.join(out_dir, "wallet_log.json"), []) if x.get("v") == SCHEMA]
    seen_w = {(x["wallet"], x["slug"], x["first"]) for x in wallet_log}
    for w in wallets:
        if w["suspicious"] and (w["wallet"], w["slug"], w["first"]) not in seen_w:
            wallet_log.append({**w, "v": SCHEMA, "logged": datetime.now(NY).strftime("%Y-%m-%d %H:%M")})
    wallet_log = wallet_log[-2000:]

    pm_log = [x for x in load_json(os.path.join(out_dir, "pm_log.json"), []) if x.get("v") == SCHEMA]
    seen = {(x["slug"], x.get("jump_time")) for x in pm_log}
    for p in pm_rows:
        if p["flagged"] and (p["slug"], p.get("jump_time")) not in seen:
            pm_log.append({**p, "v": SCHEMA, "logged": datetime.now(NY).strftime("%Y-%m-%d %H:%M")})
    pm_log = pm_log[-2000:]

    # ---- today's confluence ----
    asof = R.index[-1]
    last_session = asof.date()
    today_minute = {}
    for e in events:
        if e["scheduled"]:
            continue
        d = datetime.fromtimestamp(e["ts"], NY).date()
        if (last_session - d).days <= 1:
            today_minute.setdefault(e["sym"], []).append(e)
    pm_hits, wal_hits = {}, {}
    for p in pm_rows:
        if p["flagged"]:
            for s in p["assets"]:
                pm_hits.setdefault(s, []).append(p["question"])
    for w in wallets:
        if w["suspicious"]:
            for s in w["assets"]:
                wal_hits.setdefault(s, []).append(f"{w['name'] or w['wallet']}: ${w['usd']:,.0f} ({', '.join(w['tags'])})")
    for k in kb:
        if k["flagged"]:
            for s in k["assets"]:
                wal_hits.setdefault(s, []).append(f"Kalshi: {k['big_trades']} trades over $3k on {k['question']}")
    lead_in = {}
    for e in net["leadlag"]:
        lead_in.setdefault(e["to"], []).append(e["from"])

    rows = []
    for s in R.columns:
        q = bool(fp["quiet"][s].iloc[-1])
        bz = fp["brk"][s].iloc[-1]
        b = bool(np.isfinite(bz) and abs(bz) >= CFG["break_z"])
        mins = today_minute.get(s, [])
        pm = pm_hits.get(s, [])
        wal = wal_hits.get(s, [])
        op = opts.get(s, {})
        ofp = bool(op.get("footprint"))
        vfp = bool(op.get("vol_footprint"))
        ups = [a for a in lead_in.get(s, []) if (score[a].iloc[-1] if a in score else 0) >= 1]
        sc = q + b + (1 if mins else 0) + (1 if (pm or wal) else 0) + (1.5 if (ofp and vfp) else 1 if (ofp or vfp) else 0) + (0.5 if ups else 0)
        direction = 0
        if ofp and abs(op.get("lean") or 0) >= flow.OPT["lean_strong"]:
            direction = int(np.sign(op["lean"]))       # money in options beats the stock's own move
        elif vfp and op.get("vol_lean"):
            direction = int(op["vol_lean"])
        elif b:
            direction = int(np.sign(bz))
        elif mins:
            fl = [m for m in mins if m.get("fwd30")]
            direction = int(np.sign(sum(m["fwd30"] for m in fl))) if fl else 0
        rows.append({
            "sym": s, "name": META[s]["name"], "group": META[s]["group"],
            "tradeable": s in TRADEABLE,
            "ret": _f(R[s].iloc[-1], 5), "ret_z": _f(fp["rz"][s].iloc[-1], 2),
            "vol_z": _f(fp["vz"][s].iloc[-1], 2), "quiet": q,
            "break_z": _f(bz, 2), "break": b,
            "minute_bursts": len(mins), "pm": pm[:3], "wallets": wal[:3], "upstream": ups,
            "options": ({"lean": op.get("lean"), "unusual_prem": op.get("unusual_prem"),
                         "against_tape": op.get("against_tape"), "vol_z": op.get("vol_z")} if ofp else None),
            "vol": (op.get("vol_reasons") if vfp else None),
            "score": sc, "direction": direction,
            "pre_weekend": bool(sc >= 1 and asof.weekday() == 4),   # Friday footprint, weekend news risk
        })
    rows.sort(key=lambda r: (-r["score"], r["sym"]))

    # ---- history for the chart (daily components only, consistent back in time) ----
    flagged = (score >= CFG["flag_score"]).sum(axis=1)
    anyfp = (score >= 1).sum(axis=1)
    hist = {
        "dates": [d.strftime("%Y-%m-%d") for d in R.index],
        "flagged": [int(x) for x in flagged],
        "any": [int(x) for x in anyfp],
        "spy": [_f(x, 2) for x in close["SPY"].reindex(R.index)],
        "oil": [_f(x, 2) for x in close["CL=F"].reindex(R.index)],
        "top": [", ".join(score.loc[d][score.loc[d] >= CFG["flag_score"]].sort_values(ascending=False).index[:4])
                for d in R.index],
    }
    # today also counts minute + prediction-market footprints
    hist["flagged_today_full"] = int(sum(r["score"] >= CFG["flag_score"] for r in rows))

    stats = forward_check(R, fp, score)
    mstats = minute_stats(events)

    # ---- the web as a heat map: move size vs what options priced, dollar volume, today's co-moves ----
    vlast = vol.reindex(R.index).iloc[-1]
    clast = close.reindex(R.index).iloc[-1]
    by = {r["sym"]: r for r in rows}
    nodes = []
    for r in rows:
        s_ = r["sym"]
        iv = (opts.get(s_) or {}).get("atm_iv")
        if iv and r["ret"] is not None:
            mz, basis = r["ret"] / (iv / math.sqrt(252)), "IV"       # vs the 1-day move options priced
        else:
            mz, basis = r["ret_z"], "20d vol"
        dv = float(vlast.get(s_, np.nan) * clast.get(s_, np.nan)) if s_ in vlast else float("nan")
        tags = [t for t, on in (("quiet volume", r["quiet"]), ("break", r["break"]),
                                ("minute bursts", r["minute_bursts"]), ("odds jump", bool(r["pm"])),
                                ("odd bettors", bool(r["wallets"])), ("options flow", bool(r["options"])),
                                ("vol surface", bool(r["vol"])), ("upstream", bool(r["upstream"]))) if on]
        nodes.append({"sym": s_, "name": r["name"], "group": r["group"], "score": r["score"],
                      "ret": r["ret"], "move_z": _f(mz, 2), "basis": basis, "vol_z": r["vol_z"],
                      "dollar_vol": _f(dv, 0) if np.isfinite(dv) and dv > 0 else None, "tags": tags,
                      "spark": [_f(x, 4) for x in close[s_].reindex(R.index).iloc[-30:]] if s_ in close else []})
    nmz = {n["sym"]: n["move_z"] for n in nodes}
    for e in net["edges"]:
        a_, b_ = nmz.get(e["a"]), nmz.get(e["b"])
        if a_ is None or b_ is None or abs(a_) < 0.3 or abs(b_) < 0.3:
            e["today"] = "flat"
        elif np.sign(a_) == np.sign(b_):
            e["today"] = "up" if a_ > 0 else "down"
        else:
            e["today"] = "split"
        va, vb = by[e["a"]]["vol_z"], by[e["b"]]["vol_z"]
        # opposite moves, both on heavy volume and both real moves = money likely rotating between them
        e["rotation"] = bool(e["today"] == "split" and (va or 0) >= 1 and (vb or 0) >= 1
                             and abs(a_) >= 1 and abs(b_) >= 1)
    tracker = {
        "generated": datetime.now(NY).strftime("%Y-%m-%d %H:%M %Z"),
        "asof": asof.strftime("%Y-%m-%d"), "demo": demo, "notes": notes, "config": CFG,
        "today": rows, "network": {**net, "nodes": nodes},
        "stats": stats, "minute_stats": mstats,
        "pm": pm_rows[:40],
        "events_recent": [e for e in events if not e["scheduled"]][-60:],
        "pm_log": pm_log[-40:],
        "options": [{"sym": k, **v} for k, v in sorted(opts.items(), key=lambda kv: -(kv[1].get("unusual_prem") or 0))],
        "wallets": wallets[:25], "kalshi_bursts": kb[:15], "wallet_log": wallet_log[-40:],
        "history": hist,
    }
    with open(os.path.join(out_dir, "tracker.json"), "w") as fh:
        json.dump(tracker, fh, separators=(",", ":"))
    with open(os.path.join(out_dir, "tracker_data.js"), "w") as fh:
        fh.write("window.TRACKER = ")
        json.dump(tracker, fh, separators=(",", ":"))
        fh.write(";\n")
    with open(os.path.join(out_dir, "events.json"), "w") as fh:
        json.dump(events, fh, separators=(",", ":"))
    with open(os.path.join(out_dir, "pm_log.json"), "w") as fh:
        json.dump(pm_log, fh, separators=(",", ":"))
    with open(os.path.join(out_dir, "options_log.json"), "w") as fh:
        json.dump(opt_log, fh, separators=(",", ":"))
    with open(os.path.join(out_dir, "wallet_log.json"), "w") as fh:
        json.dump(wallet_log, fh, separators=(",", ":"))
    print(f"asof {tracker['asof']}: {hist['flagged_today_full']} flagged, "
          f"{len(events)} minute events on file, {sum(p['flagged'] for p in pm_rows)} odds jumps, "
          f"{sum(v['footprint'] for v in opts.values())} options-flow + {sum(v.get('vol_footprint', False) for v in opts.values())} vol footprints, {sum(w['suspicious'] for w in wallets)} odd wallets")
    return tracker


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true", help="simulated data")
    ap.add_argument("--out", default=None, help="output folder")
    ap.add_argument("--minute-days", type=int, default=0, help="days of 1m bars (max ~28)")
    a = ap.parse_args()
    out = a.out or (tempfile.mkdtemp(prefix="thesis-demo-") if a.demo else os.path.join(HERE, "data"))
    run(a.demo, out, a.minute_days)
    print("wrote", out)


if __name__ == "__main__":
    main()
