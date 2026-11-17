"""
Sleeper / Striker: the scanner and the decision rule.

Pure functions only. Nothing in here touches the network or the disk, so the
nightly job (algo.py), the backtest (backtest.py) and the tests all run the
exact same code.

    chain  (one row per option contract, see CHAIN_COLUMNS)
      -> scan()    : greeks + flow + vol + gamma features for one ticker
      -> decide()  : STRIKER / SLEEPER / IDLE, direction, size, structure

The idea in one paragraph: most nights nothing unusual is happening, and the
best thing a vol trader can do is sell rich premium (Sleeper). Some nights,
somebody is clearly paying up for one side - lots of volume landing on strikes
that had little open interest, filled at the ask, all leaning the same way.
That is "irregular order flow". When it shows up, and enough of the greeks /
vol picture agrees, the algo stops collecting theta and buys options in the
direction of that flow (Striker).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, asdict
from datetime import date

import numpy as np
import pandas as pd
from scipy.special import ndtr

RISK_FREE = 0.04

# expiry: datetime.date, type: "C"/"P", iv: decimal (0.55 = 55%)
CHAIN_COLUMNS = ["expiry", "type", "strike", "bid", "ask", "last", "volume", "oi", "iv"]


@dataclass
class Config:
    # which part of the chain we look at
    min_dte: int = 1
    max_dte: int = 30
    moneyness_band: float = 0.25          # strikes within +/-25% of spot
    target_dte: int = 7                   # "the" expiry for ATM IV / skew (close to 5DTE+)

    # irregular (unusual) flow, per contract
    uoa_min_volume: int = 250
    uoa_vol_oi: float = 1.5               # volume must be >= 1.5x open interest
    uoa_min_premium: float = 50_000.0     # $ traded in that contract today

    # Striker trigger
    uoa_share: float = 0.20               # unusual premium / all premium
    uoa_min_count: int = 3                # at least this many unusual contracts
    flow_min: float = 0.35                # |delta-weighted flow score| needed
    iv_pop: float = 0.08                  # ATM IV up >= 8% vs last night
    striker_min_points: int = 3
    rich_iv_rank: float = 80.0            # above this, buy a debit spread instead
    rich_iv_over_rv: float = 1.6          # same idea when there is no IV history yet

    # Sleeper (sell theta)
    sleeper_enabled: bool = True          # set False to run Striker-only (stand aside on quiet nights)
    vrp_min: float = 0.03                 # ATM IV - 20d realized vol, in vol points
    vrp_big: float = 0.08                 # sell 2 instead of 1 above this
    gex_floor: float = -0.15              # don't sell premium when dealers are short gamma
    earnings_days: int = 10

    # where the strikes go, in units of the expected move to the target expiry
    # (expected move = ATM IV * sqrt(target_dte / 365)); written as Sanji pct: tokens
    striker_long_em: float = 0.25         # long option a little out of the money
    spread_short_em: float = 1.00         # short leg of a Striker debit spread
    sleeper_short_em: float = 1.00        # short leg of a Sleeper credit spread (~16-20 delta)
    sleeper_wing_em: float = 1.80         # protective wing

    def to_dict(self):
        return asdict(self)


# ----------------------------------------------------------------------------
# Black-Scholes
# ----------------------------------------------------------------------------
def _d1d2(S, K, T, iv, r):
    T = np.maximum(np.asarray(T, float), 1.0 / 365.0)
    iv = np.clip(np.asarray(iv, float), 0.01, 5.0)
    srt = iv * np.sqrt(T)
    d1 = (np.log(S / np.asarray(K, float)) + (r + 0.5 * iv * iv) * T) / srt
    return d1, d1 - srt, T, iv


def bs_price(S, K, T, iv, is_call, r=RISK_FREE):
    d1, d2, T, iv = _d1d2(S, K, T, iv, r)
    K = np.asarray(K, float)
    disc = np.exp(-r * T)
    call = S * ndtr(d1) - K * disc * ndtr(d2)
    put = K * disc * ndtr(-d2) - S * ndtr(-d1)
    return np.where(is_call, call, put)


def bs_greeks(S, K, T, iv, is_call, r=RISK_FREE):
    """delta, gamma (per $1), vega (per 1 vol pt), theta (per calendar day)."""
    d1, d2, T, iv = _d1d2(S, K, T, iv, r)
    K = np.asarray(K, float)
    pdf = np.exp(-0.5 * d1 * d1) / math.sqrt(2 * math.pi)
    sqT = np.sqrt(T)
    disc = np.exp(-r * T)
    delta = np.where(is_call, ndtr(d1), ndtr(d1) - 1.0)
    gamma = pdf / (S * iv * sqT)
    vega = S * pdf * sqT / 100.0
    t_common = -S * pdf * iv / (2 * sqT)
    theta = np.where(is_call, t_common - r * K * disc * ndtr(d2),
                     t_common + r * K * disc * ndtr(-d2)) / 365.0
    return delta, gamma, vega, theta


def implied_vol(price, S, K, T, is_call, r=RISK_FREE):
    """Bisection. Used only when a data source hands us a missing / junk IV."""
    lo, hi = 0.01, 5.0
    intrinsic = max(0.0, (S - K) if is_call else (K - S))
    if not (price > intrinsic * 0.999) or price <= 0:
        return float("nan")
    for _ in range(60):
        mid = 0.5 * (lo + hi)
        p = float(bs_price(S, K, T, mid, is_call, r))
        if p > price:
            hi = mid
        else:
            lo = mid
    return 0.5 * (lo + hi)


# ----------------------------------------------------------------------------
# the scanner
# ----------------------------------------------------------------------------
def _f(x, n=4):
    if x is None:
        return None
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return None if not math.isfinite(x) else round(x, n)


def price_features(closes: pd.Series):
    """Realized vol, trend and momentum from daily closes (oldest -> newest)."""
    c = pd.Series(closes, dtype=float).dropna()
    out = {"rv20": None, "sma20": None, "ret5": None, "ret1": None, "n_closes": int(len(c))}
    if len(c) >= 2:
        out["ret1"] = float(c.iloc[-1] / c.iloc[-2] - 1.0)
    if len(c) >= 16:
        lr = np.log(c).diff().dropna().tail(20)
        out["rv20"] = float(lr.std(ddof=1) * math.sqrt(252))
    if len(c) >= 20:
        out["sma20"] = float(c.tail(20).mean())
    if len(c) >= 6:
        out["ret5"] = float(c.iloc[-1] / c.iloc[-6] - 1.0)
    return out


def prepare_chain(chain: pd.DataFrame, spot: float, asof: date, cfg: Config) -> pd.DataFrame:
    """Filter to the near, liquid part of the chain and add mid / greeks / flow columns."""
    df = chain.copy()
    for col in CHAIN_COLUMNS:
        if col not in df.columns:
            df[col] = np.nan
    df["expiry"] = pd.to_datetime(df["expiry"]).dt.date
    df["dte"] = [(e - asof).days for e in df["expiry"]]
    df = df[(df["dte"] >= cfg.min_dte) & (df["dte"] <= cfg.max_dte)]
    df = df[(df["strike"] >= spot * (1 - cfg.moneyness_band)) & (df["strike"] <= spot * (1 + cfg.moneyness_band))]
    df = df.copy()
    for col in ["bid", "ask", "last", "volume", "oi", "iv", "strike"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["volume"] = df["volume"].fillna(0)
    df["oi"] = df["oi"].fillna(0)
    two_sided = (df["bid"] > 0) & (df["ask"] > 0) & (df["ask"] >= df["bid"])
    df["mid"] = np.where(two_sided, (df["bid"] + df["ask"]) / 2, df["last"])
    df = df[df["mid"] > 0].copy()
    if df.empty:
        return df
    df["is_call"] = df["type"].str.upper().str.startswith("C")
    df["T"] = df["dte"] / 365.0

    # fill junk IVs from the mid
    bad = ~(df["iv"] > 0.01) | ~(df["iv"] < 5)
    for i in df.index[bad]:
        r = df.loc[i]
        df.at[i, "iv"] = implied_vol(r["mid"], spot, r["strike"], r["T"], bool(r["is_call"]))
    df = df[df["iv"] > 0.01].copy()

    d, g, v, th = bs_greeks(spot, df["strike"].values, df["T"].values, df["iv"].values, df["is_call"].values)
    df["delta"], df["gamma"], df["vega"], df["theta"] = d, g, v, th

    df["premium"] = df["volume"] * df["mid"] * 100.0
    # Aggressor: where did the last trade print inside the spread?
    # +1 = at/above the ask (someone bought), -1 = at/below the bid (someone sold).
    half = np.maximum((df["ask"] - df["bid"]) / 2, 0.01)
    agg = ((df["last"] - df["mid"]) / half).clip(-1, 1)
    df["aggressor"] = agg.where(two_sided & (df["last"] > 0), 0.0).fillna(0.0)
    side = np.where(df["is_call"], 1.0, -1.0)            # calls bullish, puts bearish
    df["direction"] = df["aggressor"] * side             # bought calls / sold puts -> +
    df["unusual"] = ((df["volume"] >= cfg.uoa_min_volume)
                     & (df["volume"] >= cfg.uoa_vol_oi * np.maximum(df["oi"], 1))
                     & (df["premium"] >= cfg.uoa_min_premium))
    # A call and a put at the same strike/expiry both hit hard = a straddle: a bet on
    # size of move, not direction. It still counts as irregular, but carries no direction.
    u = df[df["unusual"]]
    both = u.groupby(["expiry", "strike"])["is_call"].nunique()
    pairs = set(both[both == 2].index)
    df["paired"] = [(e, k) in pairs for e, k in zip(df["expiry"], df["strike"])] if pairs else False
    return df


def _gex(df, S):
    sign = np.where(df["is_call"], 1.0, -1.0)
    # $ change in dealer delta for a 1% move, assuming dealers are long calls / short puts
    return sign * df["gamma"].values * df["oi"].values * 100 * S * S * 0.01


def gamma_flip(df, spot, r=RISK_FREE):
    """Spot level where total dealer gamma changes sign (None if it doesn't nearby)."""
    if df.empty:
        return None
    grid = np.linspace(spot * 0.85, spot * 1.15, 61)
    tot = []
    for s in grid:
        _, g, _, _ = bs_greeks(s, df["strike"].values, df["T"].values, df["iv"].values, df["is_call"].values, r)
        sign = np.where(df["is_call"], 1.0, -1.0)
        tot.append(float(np.sum(sign * g * df["oi"].values * 100 * s * s * 0.01)))
    tot = np.array(tot)
    best = None
    for i in range(len(grid) - 1):
        if tot[i] == 0 or np.sign(tot[i]) != np.sign(tot[i + 1]):
            x = grid[i] - tot[i] * (grid[i + 1] - grid[i]) / (tot[i + 1] - tot[i])
            if best is None or abs(x - spot) < abs(best - spot):
                best = x
    return best


def scan(chain: pd.DataFrame, spot: float, asof: date, closes=None, iv_history=None,
         cfg: Config | None = None, next_earnings: date | None = None) -> dict:
    """All features for one ticker on one night."""
    cfg = cfg or Config()
    feat = {"asof": asof.isoformat(), "spot": _f(spot, 2)}
    feat.update({k: _f(v) for k, v in price_features(closes if closes is not None else []).items()})
    df = prepare_chain(chain, spot, asof, cfg)
    feat["n_contracts"] = int(len(df))
    if df.empty:
        feat["ok"] = False
        return feat
    feat["ok"] = True

    # --- flow ---------------------------------------------------------------
    tot_prem = float(df["premium"].sum())
    u = df[df["unusual"]]
    w_all = df["premium"] * df["delta"].abs()
    w_u = u["premium"] * u["delta"].abs()
    feat["total_premium"] = _f(tot_prem, 0)
    feat["n_unusual"] = int(len(u))
    feat["unusual_premium"] = _f(u["premium"].sum(), 0)
    feat["unusual_share"] = _f(u["premium"].sum() / tot_prem if tot_prem > 0 else 0.0)
    d_u = u["direction"].where(~u["paired"], 0.0)
    feat["n_straddle_legs"] = int(u["paired"].sum())
    feat["flow_score"] = _f((d_u * w_u).sum() / w_u.sum() if w_u.sum() > 0 else 0.0)
    feat["flow_score_all"] = _f((df["direction"] * w_all).sum() / w_all.sum() if w_all.sum() > 0 else 0.0)
    cv, pv = df.loc[df["is_call"], "volume"].sum(), df.loc[~df["is_call"], "volume"].sum()
    feat["put_call_vol"] = _f(pv / cv if cv > 0 else None)
    # net delta and vega the flow put on, signed by aggressor ($ per 1% move / per vol pt)
    feat["flow_delta_usd"] = _f((u["aggressor"] * u["delta"] * u["volume"] * 100 * spot * 0.01).sum(), 0)
    feat["flow_vega_usd"] = _f((u["aggressor"] * u["vega"] * u["volume"] * 100).sum(), 0)
    top = u.sort_values("premium", ascending=False).head(8)
    feat["top_unusual"] = [{
        "expiry": r.expiry.isoformat(), "type": "C" if r.is_call else "P", "strike": _f(r.strike, 2),
        "volume": int(r.volume), "oi": int(r.oi), "vol_oi": _f(r.volume / max(r.oi, 1), 1),
        "premium": _f(r.premium, 0), "iv": _f(r.iv), "delta": _f(r.delta, 3),
        "aggressor": _f(r.aggressor, 2),
    } for r in top.itertuples()]

    # --- vol surface --------------------------------------------------------
    exps = sorted(df.loc[df["dte"] >= 3, "expiry"].unique()) or sorted(df["expiry"].unique())
    e = min(exps, key=lambda x: abs((x - asof).days - cfg.target_dte))
    sl = df[df["expiry"] == e]
    k_atm = sl.iloc[(sl["strike"] - spot).abs().argsort()]["strike"].iloc[0]
    atm_iv = float(sl.loc[sl["strike"] == k_atm, "iv"].mean())
    feat["atm_expiry"] = e.isoformat()
    feat["atm_iv"] = _f(atm_iv)
    puts, calls = sl[~sl["is_call"]], sl[sl["is_call"]]
    if len(puts) and len(calls):
        p25 = puts.iloc[(puts["delta"] + 0.25).abs().argsort()].iloc[0]
        c25 = calls.iloc[(calls["delta"] - 0.25).abs().argsort()].iloc[0]
        feat["skew25"] = _f(p25["iv"] - c25["iv"])
    else:
        feat["skew25"] = None
    rv = feat.get("rv20")
    feat["vrp"] = _f(atm_iv - rv) if rv is not None else None
    feat["iv_over_rv"] = _f(atm_iv / rv) if rv else None

    hist = [h for h in (iv_history or []) if h.get("date") and h["date"] < asof.isoformat() and h.get("atm_iv")]
    hist.sort(key=lambda h: h["date"])
    feat["iv_change_1d"] = _f(atm_iv / hist[-1]["atm_iv"] - 1.0) if hist else None
    vals = [h["atm_iv"] for h in hist[-252:]]
    feat["iv_rank"] = _f(100.0 * np.mean([v <= atm_iv for v in vals]), 1) if len(vals) >= 10 else None
    feat["iv_history_n"] = len(vals)

    # --- dealer gamma -------------------------------------------------------
    g = _gex(df, spot)
    feat["gex_usd"] = _f(g.sum(), 0)
    feat["gex_norm"] = _f(g.sum() / np.abs(g).sum() if np.abs(g).sum() > 0 else 0.0)
    feat["gamma_flip"] = _f(gamma_flip(df, spot), 2)
    above = df[df["is_call"] & (df["strike"] >= spot)]
    below = df[~df["is_call"] & (df["strike"] <= spot)]
    feat["call_wall"] = _f(above.groupby("strike")["oi"].sum().idxmax(), 2) if len(above) else None
    feat["put_wall"] = _f(below.groupby("strike")["oi"].sum().idxmax(), 2) if len(below) else None

    feat["days_to_earnings"] = (next_earnings - asof).days if next_earnings else None
    return feat


# ----------------------------------------------------------------------------
# the decision
# ----------------------------------------------------------------------------
QTY_BY_POINTS = {3: 2, 4: 3, 5: 5}


def expected_move(feat, cfg):
    return float(feat["atm_iv"]) * math.sqrt(cfg.target_dte / 365.0)


def pct_token(sign, em, mult, floor=1):
    """Sanji pct: token, whole percent, at least `floor`% away from the open."""
    n = max(floor, int(round(100 * em * mult)))
    return f"pct:{sign * n}"


def _check(name, passed, value, need):
    return {"name": name, "passed": bool(passed), "value": value, "need": need}


def decide(feat: dict, cfg: Config | None = None) -> dict:
    cfg = cfg or Config()
    out = {"mode": "IDLE", "direction": 0, "points": 0, "qty": 0, "structure": None,
           "striker_checks": [], "sleeper_checks": [], "reason": ""}
    if not feat.get("ok"):
        out["reason"] = "no usable option chain"
        return out

    flow = feat.get("flow_score") or 0.0
    direction = 1 if flow > 0 else (-1 if flow < 0 else 0)
    out["direction"] = direction
    ivc, ret5 = feat.get("iv_change_1d"), feat.get("ret5")

    c = [
        _check("Irregular flow", feat["unusual_share"] >= cfg.uoa_share and feat["n_unusual"] >= cfg.uoa_min_count,
               f'{feat["unusual_share"]:.0%} of premium, {feat["n_unusual"]} contracts',
               f">= {cfg.uoa_share:.0%} and >= {cfg.uoa_min_count}"),
        _check("Flow is one-sided", abs(flow) >= cfg.flow_min, f"{flow:+.2f}", f"|score| >= {cfg.flow_min}"),
        _check("IV popped", ivc is not None and ivc >= cfg.iv_pop,
               "n/a (no history yet)" if ivc is None else f"{ivc:+.1%}", f">= +{cfg.iv_pop:.0%} vs last night"),
        _check("Dealers short gamma", (feat.get("gex_norm") or 0) < 0, f'{feat.get("gex_norm") or 0:+.2f}', "< 0"),
        _check("Price agrees with flow", ret5 is not None and direction != 0 and np.sign(ret5) == direction,
               "n/a" if ret5 is None else f"{ret5:+.1%} (5d)", "same sign as flow"),
    ]
    out["striker_checks"] = c
    points = sum(x["passed"] for x in c)
    out["points"] = points

    if c[0]["passed"] and c[1]["passed"] and points >= cfg.striker_min_points:
        rank, ivr = feat.get("iv_rank"), feat.get("iv_over_rv")
        rich = (rank is not None and rank >= cfg.rich_iv_rank) or (rank is None and ivr is not None and ivr >= cfg.rich_iv_over_rv)
        em = expected_move(feat, cfg)
        strikes = {"long": pct_token(direction, em, cfg.striker_long_em)}
        if rich:
            strikes["short"] = pct_token(direction, em, cfg.spread_short_em, floor=abs(int(strikes["long"].split(":")[1])) + 2)
        out.update(mode="STRIKER", qty=QTY_BY_POINTS[min(points, 5)], strikes=strikes,
                   expected_move=round(em, 4),
                   structure="debit_spread" if rich else "long_option",
                   reason=(f"{'Bullish' if direction > 0 else 'Bearish'} irregular flow, {points}/5 signals agree"
                           + ("; IV is rich so the long option is paired with a short one" if rich else "")))
        return out

    # ---- Sleeper ----
    if not cfg.sleeper_enabled:
        out["reason"] = "No Striker trigger (Sleeper is switched off)"
        out["direction"] = 0
        return out
    vrp, rank = feat.get("vrp"), feat.get("iv_rank")
    dte_e = feat.get("days_to_earnings")
    rich_vol = (vrp is not None and vrp >= cfg.vrp_min) or (vrp is None and rank is not None and rank >= 50)
    up = feat.get("sma20") is None or feat["spot"] >= feat["sma20"]
    side = 1 if up else -1
    s = [
        _check("Quiet tape", not c[0]["passed"], f'{feat["unusual_share"]:.0%} irregular', f"< {cfg.uoa_share:.0%}"),
        _check("Flow not leaning against the spread", flow * side > -cfg.flow_min, f"{flow:+.2f}",
               f"not {'<=' if side > 0 else '>='} {-side * cfg.flow_min:+.2f}"),
        _check("IV rich vs realized", rich_vol, "n/a" if vrp is None else f"{vrp:+.1%} pts",
               f">= +{cfg.vrp_min:.0%} pts"),
        _check("Dealers not short gamma", (feat.get("gex_norm") or 0) >= cfg.gex_floor,
               f'{feat.get("gex_norm") or 0:+.2f}', f">= {cfg.gex_floor}"),
        _check("No earnings soon", dte_e is None or dte_e > cfg.earnings_days,
               "unknown" if dte_e is None else f"{dte_e} days", f"> {cfg.earnings_days} days"),
    ]
    out["sleeper_checks"] = s
    if all(x["passed"] for x in s):
        em = expected_move(feat, cfg)
        short = pct_token(-side, em, cfg.sleeper_short_em)
        wing = pct_token(-side, em, cfg.sleeper_wing_em, floor=abs(int(short.split(":")[1])) + 2)
        out.update(mode="SLEEPER", direction=side, strikes={"short": short, "wing": wing},
                   expected_move=round(em, 4),
                   qty=2 if (vrp or 0) >= cfg.vrp_big else 1,
                   structure="bull_put_spread" if up else "bear_call_spread",
                   reason=("Quiet tape and rich IV: sell a " + ("put spread under the market (trend up)" if up
                           else "call spread over the market (trend down)")))
        return out

    if c[0]["passed"]:
        out["reason"] = "Heavy irregular flow but no clear direction / not enough confirmation: stand aside"
    else:
        failed = [x["name"] for x in s if not x["passed"]]
        out["reason"] = "No trigger, and no edge selling premium (" + ", ".join(failed) + ")"
    out["direction"] = 0
    return out
