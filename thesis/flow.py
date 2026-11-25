"""
Two footprints added after the Nvidia reinstatement (Nov 20-23, 2026):

1. Options flow     - near-term contracts trading far above their open interest
                      (new positions), sized in dollars, with a call/put lean.
                      "Against the tape" = the flow leans the opposite way to
                      the stock's own move (calls piling up while it falls).
2. Wallet bets      - big Polymarket bets placed by wallets that look out of
                      place: brand new, dormant for 30+ days, or wallets that
                      normally bet on something else (a sports bettor's first
                      Nvidia bet). Plus Kalshi bursts of large trades.

Everything here is pure functions + thin fetchers, so demo data and tests run
the same code as the nightly job.
"""
from __future__ import annotations

import math
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd

OPTION_SYMS = ["NVDA", "TSLA", "MSFT", "AAPL", "AMZN", "META", "GOOGL", "TSM", "MU", "AMD", "AVGO",
               "SPY", "QQQ", "USO", "XLE", "GLD", "SLV", "TLT"]

OPT = {
    "max_dte": 45,
    "min_dte": 5,                 # skip 0DTE / this-week contracts: volume beats open interest there every day
    "baseline_nights": 8,         # options flow only flags once a name has this many nights of its own history
    "min_volume": 500,            # contracts
    "vol_oi": 1.5,                # volume >= 1.5x open interest = mostly new positions
    "min_contract_prem": 100_000,  # $ traded in that contract today
    "flag_prem": 2_000_000,       # unusual $ needed for a footprint...
    "flag_share": 0.10,           # ...and at least this share of all option $ today (until history exists)
    "lean_strong": 0.4,
    "vol_z": 2.0,                 # or: total option volume 2 std above its own history
}
WALLET = {
    "min_trade": 2_000,           # $ per fill pulled from the market feed
    "min_wallet": 5_000,          # $ a wallet must put into one market to be checked
    "lookback_h": 48,
    "dormant_days": 30,
    "new_days": 7,
    "off_lane_share": 0.10,       # < 10% of past trades on this topic
    "off_lane_min_hist": 20,
    "longshot": 0.20,             # average price paid at or under 20 cents
    "max_price": 0.60,            # buying at 60c+ is betting with the crowd (or parking cash at 95c+), not a tip
    "activity_limit": 1000,
    "max_wallets": 30,
    "kalshi_big": 3_000,
    "kalshi_burst": 5,            # this many $3k+ trades inside 24h
}

TOPICS = [
    ("Sports", r"(counter-strike|\bcs2?\b|\bbo[135]\b|esports|\b(nfl|nba|mlb|nhl|ufc|fifa|premier league|champions league|super bowl|world series|vs\.?|match|game \d|cup|league of legends|dota|valorant)\b)"),
    ("Fed / rates", r"\b(fed|fomc|rate cut|rate hike|interest rate|powell|warsh|hassett|bps)\b"),
    ("Iran / oil / war", r"\b(iran|israel|hormuz|opec|oil|crude|saudi|houthi|ceasefire|strike|military|war|missile|venezuela)\b"),
    ("Russia / Ukraine", r"\b(russia|ukraine|putin|zelensky)\b"),
    ("Trade / China", r"\b(tariffs?|trade deal|china|xi|taiwan|export)\b"),
    ("Companies / CEOs", r"\b(nvidia|jensen|huang|tesla|musk|apple|microsoft|openai|google|alphabet|amazon|meta|ceo)\b"),
    ("Crypto", r"\b(bitcoin|btc|ethereum|eth|crypto|solana)\b"),
    ("Macro data", r"\b(recession|gdp|inflation|cpi|jobs|unemployment|payrolls|shutdown)\b"),
    ("Elections / politics", r"\b(election|senate|house|governor|president|trump|democrat|republican|poll|primary)\b"),
]


def topic(text: str) -> str:
    t = (text or "").lower()
    for name, pat in TOPICS:
        if re.search(pat, t):
            return name
    return "Other"


def _f(x, nd=4):
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return round(x, nd) if math.isfinite(x) else None


# ----------------------------------------------------------------------------
# Options flow
# ----------------------------------------------------------------------------
OCC = re.compile(r"^([A-Z.]+?)(\d{6})([CP])(\d{8})$")
UA = {"User-Agent": "Mozilla/5.0 (fintech535 thesis tracker)"}


def cboe_chain(sym: str) -> tuple[pd.DataFrame, float]:
    import requests
    r = requests.get(f"https://cdn.cboe.com/api/global/delayed_quotes/options/{sym}.json", headers=UA, timeout=30)
    r.raise_for_status()
    data = r.json().get("data") or {}
    spot = data.get("current_price") or data.get("close")
    rows = []
    today = date.today()
    for o in data.get("options", []):
        m = OCC.match(str(o.get("option", "")))
        if not m:
            continue
        exp = datetime.strptime(m.group(2), "%y%m%d").date()
        if (exp - today).days > OPT["max_dte"]:
            continue
        rows.append({"expiry": exp, "type": m.group(3), "strike": int(m.group(4)) / 1000.0,
                     "bid": o.get("bid"), "ask": o.get("ask"), "last": o.get("last_trade_price"),
                     "volume": o.get("volume"), "oi": o.get("open_interest"), "iv": o.get("iv")})
    if not rows or not spot:
        raise RuntimeError("CBOE: empty chain")
    return pd.DataFrame(rows), float(spot)


def yf_chain(sym: str) -> tuple[pd.DataFrame, float]:
    import yfinance as yf
    t = yf.Ticker(sym)
    h = t.history(period="5d")
    spot = float(h["Close"].iloc[-1])
    today = date.today()
    frames = []
    for e in t.options:
        exp = datetime.strptime(e, "%Y-%m-%d").date()
        if (exp - today).days > OPT["max_dte"]:
            continue
        ch = t.option_chain(e)
        for typ, df in (("C", ch.calls), ("P", ch.puts)):
            frames.append(pd.DataFrame({"expiry": exp, "type": typ, "strike": df["strike"], "bid": df["bid"],
                                        "ask": df["ask"], "last": df["lastPrice"], "volume": df["volume"],
                                        "oi": df["openInterest"], "iv": df["impliedVolatility"]}))
    if not frames:
        raise RuntimeError("yfinance: empty chain")
    return pd.concat(frames, ignore_index=True), spot


def live_chains(syms=OPTION_SYMS) -> dict:
    out = {}
    for s in syms:
        for fn in (cboe_chain, yf_chain):
            try:
                out[s] = fn(s)
                break
            except Exception as e:                                   # noqa: BLE001
                print(f"  chain {s} via {fn.__name__}: {e}", file=sys.stderr)
    return out


def demo_chains(seed: int = 3) -> dict:
    rng = np.random.default_rng(seed)
    out = {}
    exps = [date(2026, 11, 27), date(2026, 12, 4), date(2026, 12, 18), date(2027, 1, 2)]
    for s in OPTION_SYMS:
        spot = 100.0
        rows = []
        for e in exps:
            for k in np.arange(70, 131, 2.5):
                for typ in "CP":
                    mny = (k - spot) / spot
                    dte = (e - date(2026, 11, 24)).days
                    iv = 0.38 + 0.25 * mny ** 2 - 0.06 * mny + rng.normal(0, 0.004)
                    if s == "NVDA" and dte <= 14:          # event premium up front, calls bid
                        iv += 0.22 + (0.06 if typ == "C" else 0.0)
                    oi = int(rng.integers(200, 6000))
                    vol = int(oi * rng.uniform(0.05, 0.6))
                    mid = float(bs(spot, np.array([k]), np.array([max(dte, 1) / 365]), np.array([iv]), np.array([typ == "C"]))[0][0])
                    mid = max(mid, 0.02)
                    rows.append({"expiry": e, "type": typ, "strike": float(k), "bid": mid * 0.97, "ask": mid * 1.03,
                                 "last": mid, "volume": vol, "oi": oi, "iv": iv})
        df = pd.DataFrame(rows)
        if s == "NVDA":   # Huang-style: far OTM calls, new positions, bought at the ask, on a down day
            m = (df["type"] == "C") & (df["strike"].between(115, 125)) & (df["expiry"] == exps[1])
            df.loc[m, "volume"] = df.loc[m, "oi"] * 6 + 20000
            df.loc[m, "last"] = df.loc[m, "ask"]
        out[s] = (df, spot)
    return out


def scan_chain(df: pd.DataFrame, spot: float) -> dict:
    df = df.copy()
    for c in ("bid", "ask", "last", "volume", "oi", "strike"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["volume"] = df["volume"].fillna(0)
    df["oi"] = df["oi"].fillna(0)
    mid = (df["bid"].fillna(0) + df["ask"].fillna(0)) / 2
    px = mid.where(mid > 0, df["last"]).fillna(0)
    df["prem"] = df["volume"] * px * 100
    # side guess from the last print vs the quote: at/above mid = bought, below = sold
    spread = (df["ask"] - df["bid"]).abs().replace(0, np.nan)
    df["aggr"] = ((df["last"] - mid) / spread).clip(-0.5, 0.5).fillna(0) * 2       # -1 sold .. +1 bought
    df["otm"] = np.where(df["type"] == "C", df["strike"] > spot * 1.02, df["strike"] < spot * 0.98)
    dte_ok = df["dte"] >= OPT["min_dte"] if "dte" in df else True
    unusual = (df["volume"] >= OPT["min_volume"]) & (df["volume"] >= OPT["vol_oi"] * df["oi"].clip(lower=1)) \
        & (df["prem"] >= OPT["min_contract_prem"]) & dte_ok
    u = df[unusual]
    tot_prem = float(df["prem"].sum())
    u_prem = float(u["prem"].sum())
    sgn = np.where(u["type"] == "C", 1.0, -1.0) * np.where(u["aggr"] >= 0, 1.0, -1.0)
    lean = float((sgn * u["prem"]).sum() / u_prem) if u_prem > 0 else 0.0
    top = u.sort_values("prem", ascending=False).head(4)
    gk = greeks_flow(df, spot, unusual, df["aggr"]) if "delta" in df else {}
    return {"_greeks": gk,
        "spot": _f(spot, 2), "total_volume": int(df["volume"].sum()), "total_oi": int(df["oi"].sum()),
        "call_volume": int(df.loc[df["type"] == "C", "volume"].sum()),
        "put_volume": int(df.loc[df["type"] == "P", "volume"].sum()),
        "total_prem": _f(tot_prem, 0), "unusual_prem": _f(u_prem, 0), "unusual_n": int(len(u)),
        "unusual_share": _f(u_prem / tot_prem, 3) if tot_prem else 0.0,
        "otm_share": _f(u.loc[u["otm"], "prem"].sum() / u_prem, 3) if u_prem else 0.0,
        "lean": _f(lean, 3),
        "top": [{"contract": f"{r.expiry:%m/%d} {r.strike:g}{r.type}", "volume": int(r.volume), "oi": int(r.oi),
                 "prem": _f(r.prem, 0), "bought": bool(r.aggr >= 0),
                 "iv": _f(getattr(r, "iv", None), 3), "delta": _f(getattr(r, "delta", None), 2)} for r in top.itertuples()],
    }


def options_footprints(chains: dict, day_ret: dict, log: list[dict], asof: str, ret_z: dict | None = None,
                       stock_usd: dict | None = None, earnings: dict | None = None) -> tuple[dict, list[dict]]:
    """Return {sym: features + footprint flags} and the updated nightly log."""
    out = {}
    ad = date.fromisoformat(asof)
    for s, (df, spot) in chains.items():
        try:
            g = add_greeks(df, spot, ad)
            f = scan_chain(g, spot)
            f.update(f.pop("_greeks"))
            f.update(vol_surface(g, spot))
        except Exception as e:                                       # noqa: BLE001
            print(f"  options {s}: {e}", file=sys.stderr)
            continue
        hist = [x for x in log if x["sym"] == s and x["date"] < asof][-20:]
        f.update(vol_footprint(f, hist, (ret_z or {}).get(s), (earnings or {}).get(s), ad))
        su = (stock_usd or {}).get(s)
        f["delta_vs_stock"] = (f["net_delta_usd"] / su) if su else None

        def z(key, now):
            past = np.log1p([x.get(key) or 0 for x in hist])
            sd = past.std()
            return float((math.log1p(now or 0) - past.mean()) / sd) if len(hist) >= 8 and sd > 0 else None
        vz, uz = z("total_volume", f["total_volume"]), z("unusual_prem", f["unusual_prem"])
        f["vol_z"], f["unusual_z"] = _f(vz, 2), _f(uz, 2)
        big = (f["unusual_prem"] or 0) >= OPT["flag_prem"] and (f["unusual_share"] or 0) >= OPT["flag_share"]
        f["baseline_nights"] = len(hist)
        if uz is None:          # first ~2 weeks: build this name's own baseline, don't flag yet
            f["footprint"] = False
        else:                   # after that: unusual compared with this name's own normal
            f["footprint"] = bool((f["unusual_prem"] or 0) >= OPT["flag_prem"] / 4 and
                                  (uz >= OPT["vol_z"] or (vz is not None and vz >= OPT["vol_z"])))
        r = day_ret.get(s)
        f["against_tape"] = bool(f["footprint"] and r is not None and abs(f["lean"] or 0) >= OPT["lean_strong"]
                                 and np.sign(f["lean"]) == -np.sign(r) and r != 0)
        out[s] = f
        log = [x for x in log if not (x["sym"] == s and x["date"] == asof)]
        log.append({"date": asof, "sym": s, "v": 2, "total_volume": f["total_volume"], "total_oi": f["total_oi"],
                    "unusual_prem": f["unusual_prem"], "lean": f["lean"], "atm_iv": f.get("atm_iv"),
                    "back_iv": f.get("back_iv"), "iv_spread": f.get("iv_spread"), "smirk": f.get("smirk"),
                    "gex": f.get("gex")})
        for k in ("atm_iv", "back_iv", "term", "iv_spread", "smirk", "atm_iv_chg", "iv_spread_chg", "smirk_chg",
                  "delta_vs_stock"):
            f[k] = _f(f.get(k), 4)
        for k in ("net_delta_usd", "net_vega_usd", "net_gamma_usd", "gex"):
            f[k] = _f(f.get(k), 0)
    return out, log[-6000:]


# ----------------------------------------------------------------------------
# Prediction-market wallets
# ----------------------------------------------------------------------------
def _get(url, params=None, timeout=20):
    import requests
    for i in range(3):
        r = requests.get(url, params=params, timeout=timeout, headers=UA)
        if r.status_code == 429:
            time.sleep(2 * (i + 1))
            continue
        r.raise_for_status()
        return r.json()
    r.raise_for_status()


def _rows(payload):
    if isinstance(payload, dict):
        return payload.get("data") or []
    return payload or []


def profile_wallet(acts: list[dict], market_condition: str, market_topic: str, first_ts: int) -> dict:
    """What did this wallet do before this bet?"""
    acts = [a for a in acts if str(a.get("type", "TRADE")).upper() == "TRADE"]
    prior = [a for a in acts if int(a.get("timestamp", 0)) < first_ts and a.get("condition_id") != market_condition]
    ts = sorted(int(a.get("timestamp", 0)) for a in acts)
    first_seen = ts[0] if ts else first_ts
    last_prior = max((int(a["timestamp"]) for a in prior), default=None)
    same = sum(topic(a.get("title", "")) == market_topic for a in prior)
    tops = pd.Series([topic(a.get("title", "")) for a in prior]).value_counts()
    return {
        "prior_trades": len(prior),
        "days_since_prior": _f((first_ts - last_prior) / 86400, 1) if last_prior else None,
        "age_days": _f((first_ts - first_seen) / 86400, 1),
        "same_topic_share": _f(same / len(prior), 3) if prior else None,
        "usual_topic": tops.index[0] if len(tops) else None,
    }


def judge_wallet(p: dict, notional: float, avg_price: float) -> list[str]:
    tags = []
    if p["prior_trades"] < 10 and (p["age_days"] is None or p["age_days"] <= WALLET["new_days"]):
        tags.append("new wallet")
    elif p["days_since_prior"] is not None and p["days_since_prior"] >= WALLET["dormant_days"]:
        tags.append(f"dormant {p['days_since_prior']:.0f}d")
    if p["prior_trades"] >= WALLET["off_lane_min_hist"] and (p["same_topic_share"] or 0) < WALLET["off_lane_share"] \
            and p.get("usual_topic") not in (None, "Other"):
        tags.append(f"usually bets {p['usual_topic']}")
    if avg_price is not None and avg_price <= WALLET["longshot"]:
        tags.append("longshot")
    return tags


def scan_wallets(markets: list[dict], fetch_trades, fetch_activity, now_ts: int) -> list[dict]:
    """markets: gamma rows (need conditionId, question). fetchers are injected for demo/tests."""
    since = now_ts - WALLET["lookback_h"] * 3600
    found, checked = [], 0
    for m in markets:
        cond = m.get("conditionId") or m.get("condition_id")
        if not cond:
            continue
        mtopic = topic(m.get("question", ""))
        try:
            trades = [t for t in _rows(fetch_trades(cond)) if int(t.get("timestamp", 0)) >= since]
        except Exception as e:                                       # noqa: BLE001
            print(f"  wallet trades {m.get('slug')}: {e}", file=sys.stderr)
            continue
        agg = {}
        for t in trades:
            if str(t.get("side", "BUY")).upper() != "BUY":
                continue
            w = t.get("proxy_wallet") or t.get("proxyWallet")
            usd = float(t.get("size", 0)) * float(t.get("price", 0))
            a = agg.setdefault((w, t.get("outcome")), {"usd": 0.0, "shares": 0.0, "first": 10**12, "last": 0,
                                                      "name": t.get("pseudonym") or t.get("name")})
            a["usd"] += usd
            a["shares"] += float(t.get("size", 0))
            a["first"] = min(a["first"], int(t["timestamp"]))
            a["last"] = max(a["last"], int(t["timestamp"]))
        for (w, outcome), a in sorted(agg.items(), key=lambda kv: -kv[1]["usd"]):
            if a["usd"] < WALLET["min_wallet"] or checked >= WALLET["max_wallets"]:
                continue
            checked += 1
            try:
                acts = _rows(fetch_activity(w))
            except Exception as e:                                   # noqa: BLE001
                print(f"  wallet activity {w}: {e}", file=sys.stderr)
                continue
            p = profile_wallet(acts, cond, mtopic, a["first"])
            avg = a["usd"] / a["shares"] if a["shares"] else None
            tags = judge_wallet(p, a["usd"], avg)
            suspicious = bool(avg is not None and avg <= WALLET["max_price"]
                              and any(t.startswith(("new", "dormant", "usually")) for t in tags))
            found.append({
                "question": m.get("question"), "slug": m.get("slug"), "outcome": outcome,
                "wallet": (w or "")[:6] + "..." + (w or "")[-4:], "name": a["name"],
                "usd": _f(a["usd"], 0), "avg_price": _f(avg, 3),
                "first": datetime.fromtimestamp(a["first"], timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                "tags": tags, "suspicious": bool(suspicious), **p,
            })
    found.sort(key=lambda r: (not r["suspicious"], -(r["usd"] or 0)))
    return found


def live_wallets(markets: list[dict], now_ts: int) -> list[dict]:
    base = "https://data-api.polymarket.com/v2"
    return scan_wallets(
        markets,
        lambda cond: _get(f"{base}/trades", {"condition": cond, "limit": 1000, "filter_type": "CASH",
                                              "filter_amount": WALLET["min_trade"], "taker_only": "true"}),
        lambda user: _get(f"{base}/activity", {"user": user, "type": "TRADE", "limit": 1000, "start": 1}),
        now_ts)


def demo_wallets(now_ts: int) -> list[dict]:
    cond = "0xhuang"
    mk = [{"conditionId": cond, "question": "Who will be the next CEO of NVIDIA? - No Change", "slug": "nvda-ceo"}]
    trades = [
        {"proxy_wallet": "0xdormant000000000000000000000000000000b1", "side": "BUY", "size": 90000, "price": 0.2,
         "timestamp": now_ts - 20 * 3600, "outcome": "Yes", "pseudonym": "bbbbbbbbzzz"},
        {"proxy_wallet": "0xsports0000000000000000000000000000000b2", "side": "BUY", "size": 100000, "price": 0.19,
         "timestamp": now_ts - 6 * 3600, "outcome": "Yes", "pseudonym": "BrotherObama"},
        {"proxy_wallet": "0xregular000000000000000000000000000000b3", "side": "BUY", "size": 30000, "price": 0.2,
         "timestamp": now_ts - 3 * 3600, "outcome": "Yes", "pseudonym": "NvdaWatcher"},
    ]
    acts = {
        "0xdormant000000000000000000000000000000b1": [{"type": "TRADE", "timestamp": now_ts - 340 * 86400, "title": "Will Nvidia hit $5T?", "condition_id": "x"}],
        "0xsports0000000000000000000000000000000b2": [{"type": "TRADE", "timestamp": now_ts - i * 3600, "title": "Lakers vs. Celtics", "condition_id": f"s{i}"} for i in range(30, 300)],
        "0xregular000000000000000000000000000000b3": [{"type": "TRADE", "timestamp": now_ts - i * 7200, "title": "Will Nvidia beat earnings? CEO Huang", "condition_id": f"t{i}"} for i in range(5, 60)] + [{"type": "TRADE", "timestamp": now_ts - 200 * 86400, "title": "Nvidia above $200?", "condition_id": "old"}],
    }
    return scan_wallets(mk, lambda c: {"data": trades}, lambda u: {"data": acts.get(u, [])}, now_ts)


# ----------------------------------------------------------------------------
# Kalshi: bursts of big trades (accounts are anonymous there)
# ----------------------------------------------------------------------------
def kalshi_bursts(tickers: list[tuple[str, str]], now_ts: int) -> list[dict]:
    out = []
    for ticker, title in tickers:
        try:
            d = _get("https://api.elections.kalshi.com/trade-api/v2/markets/trades",
                     {"ticker": ticker, "limit": 1000, "min_ts": now_ts - 86400})
        except Exception as e:                                       # noqa: BLE001
            print(f"  kalshi trades {ticker}: {e}", file=sys.stderr)
            continue
        big, usd = 0, 0.0
        for t in d.get("trades", []):
            n = float(t.get("count_fp") or t.get("count") or 0)
            side = t.get("taker_side", "yes")
            px = t.get(f"{side}_price_dollars")
            px = float(px) if px is not None else float(t.get(f"{side}_price", 0)) / 100
            v = n * px
            if v >= WALLET["kalshi_big"]:
                big += 1
                usd += v
        if big:
            out.append({"ticker": ticker, "question": title, "big_trades": big, "usd": _f(usd, 0),
                        "flagged": big >= WALLET["kalshi_burst"]})
    out.sort(key=lambda r: -r["big_trades"])
    return out


# ----------------------------------------------------------------------------
# Greeks and the vol surface
# ----------------------------------------------------------------------------
from scipy.special import ndtr  # noqa: E402

RF = 0.04
VOLS = {
    "iv_pop": 0.10,          # front ATM IV up 10%+ vs last night...
    "quiet_rz": 1.0,         # ...while the stock moved under 1 std
    "term_inv": 0.10,        # front IV 10+ vol points over the back month, no earnings in between
    "ivs_chg": 0.03,         # call-minus-put IV spread moves 3+ vol points in a night
    "smirk_chg": 0.03,       # put smirk moves 3+ vol points in a night
    "ivs_abs": 0.05,         # first nights only (no history): spread this far from zero
    "z": 2.0,                # once 8+ nights of history: same tests as z-scores
}


def _npdf(x):
    return np.exp(-0.5 * x * x) / np.sqrt(2 * np.pi)


def bs(S, K, T, iv, call):
    T = np.maximum(T, 1 / 365)
    iv = np.maximum(iv, 1e-4)
    sq = iv * np.sqrt(T)
    d1 = (np.log(S / K) + (RF + 0.5 * iv * iv) * T) / sq
    d2 = d1 - sq
    price = np.where(call, S * ndtr(d1) - K * np.exp(-RF * T) * ndtr(d2),
                     K * np.exp(-RF * T) * ndtr(-d2) - S * ndtr(-d1))
    delta = np.where(call, ndtr(d1), ndtr(d1) - 1)
    gamma = _npdf(d1) / (S * sq)
    vega = S * _npdf(d1) * np.sqrt(T) / 100            # $ per 1 vol point
    return price, delta, gamma, vega


def solve_iv(price, S, K, T, call):
    lo, hi = np.full(len(K), 0.01), np.full(len(K), 5.0)
    for _ in range(60):
        mid = (lo + hi) / 2
        p, *_ = bs(S, K, T, mid, call)
        up = p > price
        hi = np.where(up, mid, hi)
        lo = np.where(up, lo, mid)
    return (lo + hi) / 2


def add_greeks(df: pd.DataFrame, spot: float, asof: date) -> pd.DataFrame:
    df = df.copy()
    for c in ("bid", "ask", "last", "volume", "oi", "strike", "iv"):
        if c not in df:
            df[c] = np.nan
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["dte"] = [(e - asof).days if isinstance(e, date) else np.nan for e in df["expiry"]]
    df = df[df["dte"] >= 0].copy()
    df["T"] = df["dte"].clip(lower=1) / 365
    call = (df["type"] == "C").values
    mid = (df["bid"].fillna(0) + df["ask"].fillna(0)) / 2
    px = mid.where((df["bid"] > 0) & (df["ask"] > 0), df["last"]).fillna(0)
    # Yahoo's implied-vol column is too noisy to trust; solve IV ourselves from the mid (or last trade).
    intrinsic = np.where(call, np.maximum(spot - df["strike"], 0), np.maximum(df["strike"] - spot, 0))
    ok = (px > intrinsic + 0.01) & (px > 0)
    iv = pd.Series(np.nan, index=df.index)
    if ok.any():
        iv.loc[ok] = solve_iv(px[ok].values, spot, df.loc[ok, "strike"].values, df.loc[ok, "T"].values, call[ok.values])
    df["iv"] = iv.where((iv > 0.02) & (iv < 4.9))
    _, d, g, v = bs(spot, df["strike"].values, df["T"].values, df["iv"].fillna(0.3).values, call)
    df["delta"], df["gamma"], df["vega"] = d, g, v
    return df


def _atm_iv(df, spot, exp):
    e = df[(df["expiry"] == exp) & df["iv"].notna()]
    if e.empty:
        return None
    k = e.iloc[(e["strike"] - spot).abs().argsort()[:1]]["strike"].iloc[0]
    v = e[e["strike"] == k]["iv"].mean()
    return float(v) if np.isfinite(v) else None


def vol_surface(df: pd.DataFrame, spot: float) -> dict:
    exps = sorted(df.groupby("expiry")["dte"].first().items(), key=lambda kv: kv[1])
    front = next((e for e, d in exps if d >= 5), None)
    back = min((e for e, d in exps if d >= 21), key=lambda e: abs(dict(exps)[e] - 35), default=None)
    out = {"front_exp": str(front) if front else None, "back_exp": str(back) if back else None}
    out["atm_iv"] = _atm_iv(df, spot, front) if front else None
    out["back_iv"] = _atm_iv(df, spot, back) if back else None
    out["term"] = (out["atm_iv"] - out["back_iv"]) if (out["atm_iv"] and out["back_iv"]) else None
    ivs = smirk = None
    if front is not None:
        e = df[(df["expiry"] == front) & df["iv"].notna()]
        c = e[e["type"] == "C"].set_index("strike")
        p = e[e["type"] == "P"].set_index("strike")
        ks = c.index.intersection(p.index)
        ks = [k for k in ks if abs(k / spot - 1) <= 0.05 and c.loc[k, "oi"] >= 100 and p.loc[k, "oi"] >= 100]
        if ks:
            w = (c.loc[ks, "oi"].fillna(0) + p.loc[ks, "oi"].fillna(0)).values + 1
            ivs = float(np.average(c.loc[ks, "iv"].values - p.loc[ks, "iv"].values, weights=w))
        otm_p = e[(e["type"] == "P") & (e["strike"] < spot)]
        atm_c = e[e["type"] == "C"]
        if len(otm_p) and len(atm_c):
            kp = otm_p.iloc[(otm_p["strike"] / spot - 0.95).abs().argsort()[:1]]
            kc = atm_c.iloc[(atm_c["strike"] / spot - 1.0).abs().argsort()[:1]]
            smirk = float(kp["iv"].iloc[0] - kc["iv"].iloc[0])
    out["iv_spread"], out["smirk"] = ivs, smirk
    # dealer gamma: customers usually buy both calls and puts, so dealers are short both;
    # standard convention: +call gamma, -put gamma; $ change in dealer delta per 1% move
    sign = np.where(df["type"] == "C", 1.0, -1.0)
    out["gex"] = float((sign * df["gamma"] * df["oi"].fillna(0) * 100 * spot * spot * 0.01).sum())
    return out


def greeks_flow(df: pd.DataFrame, spot: float, unusual: pd.Series, aggr: pd.Series) -> dict:
    u = df[unusual]
    side = np.where(aggr[unusual] >= 0, 1.0, -1.0)
    ddelta = float((u["volume"] * u["delta"] * 100 * spot * side).sum())          # $ of stock-equivalent exposure
    dvega = float((u["volume"] * u["vega"] * 100 * side).sum())                   # $ per vol point
    dgamma = float((u["volume"] * u["gamma"] * 100 * spot * spot * 0.01 * side).sum())
    return {"net_delta_usd": ddelta, "net_vega_usd": dvega, "net_gamma_usd": dgamma}


def earnings_dates(syms) -> dict:
    out = {}
    try:
        import yfinance as yf
    except Exception:                                                # noqa: BLE001
        return out
    for s in syms:
        try:
            cal = yf.Ticker(s).calendar or {}
            ds = cal.get("Earnings Date") if isinstance(cal, dict) else None
            if ds:
                out[s] = [str(d) for d in (ds if isinstance(ds, (list, tuple)) else [ds])]
        except Exception:                                            # noqa: BLE001
            pass
    return out


def vol_footprint(f: dict, hist: list[dict], rz: float | None, earnings: list[str] | None, asof: date) -> dict:
    prev = hist[-1] if hist else None
    reasons, lean = [], 0.0

    def z(key):
        xs = [h.get(key) for h in hist[-20:] if h.get(key) is not None]
        d = np.diff(xs) if len(xs) >= 9 else []
        if len(d) < 8 or f.get(key) is None or prev is None or prev.get(key) is None:
            return None
        sd = np.std(d)
        return float((f[key] - prev[key]) / sd) if sd > 0 else None

    # 1) implied vol jumps while the stock stays quiet
    if prev and f.get("atm_iv") and prev.get("atm_iv"):
        chg = f["atm_iv"] / prev["atm_iv"] - 1
        f["atm_iv_chg"] = chg
        zz = z("atm_iv")
        if (chg >= VOLS["iv_pop"] or (zz is not None and zz >= VOLS["z"])) and (rz is None or abs(rz) <= VOLS["quiet_rz"]):
            reasons.append(f"IV +{chg * 100:.0f}% on a quiet day")
    # 2) event premium in the front month with no scheduled event
    front = f.get("front_exp")
    has_earn = False
    if earnings and front:
        has_earn = any(str(asof) <= d[:10] <= front for d in earnings)
    f["earnings_in_front"] = has_earn
    if f.get("term") is not None and f["term"] >= VOLS["term_inv"] and not has_earn:
        reasons.append(f"front IV {f['term'] * 100:.0f} pts over back month, no earnings")
    # 3) skew moves: calls getting expensive vs puts (bullish) or puts bid (bearish)
    for key, thr, sgn, label in (("iv_spread", VOLS["ivs_chg"], 1, "calls bid vs puts"),
                                 ("smirk", VOLS["smirk_chg"], -1, "put skew steeper")):
        if f.get(key) is None:
            continue
        if prev and prev.get(key) is not None:
            d = f[key] - prev[key]
            f[key + "_chg"] = d
            zz = z(key)
            if abs(d) >= thr or (zz is not None and abs(zz) >= VOLS["z"]):
                reasons.append(f"{label if d * sgn > 0 else ('puts bid vs calls' if key == 'iv_spread' else 'put skew flatter')} ({d * 100:+.1f} pts)")
                lean += sgn * np.sign(d)
    return {"vol_footprint": bool(reasons), "vol_reasons": reasons, "vol_lean": float(np.sign(lean))}
