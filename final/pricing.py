"""
Pricing ourselves: pick the exact contract tonight and a limit price that
tomorrow's market is likely to trade through.

How Sanji fills a limit (order.html section 10):
  buy / cover : fills at the open if the open is already <= limit,
                otherwise at the limit if the day's LOW trades through it
  sell / short: mirror image, using the day's HIGH
  never trades through -> unfilled, no partials

So the question for every order is: "how far below the mid can I bid and
still have, say, a 60% chance that tomorrow's low reaches me?"

We answer it by simulation. Starting from tonight's close we draw a few
thousand paths for tomorrow: an overnight gap, then the trading day in
15-minute steps. On each path we value the option with Black-Scholes, which
gives us that path's open, high and low for the option. Then for every candidate
limit we count how often Sanji would have filled it and at what price, and
keep the best price that still meets the fill target.

The tilt is the user's idea: if the stock has been falling and we want to
buy calls, selling pressure tends to carry into the open, so the paths drift
lower, the option's expected low is lower, and the bid sits lower. If the
stock is already running our way, the dip is less likely and the model
raises the bid instead of missing the move.

We price off numbers we can see tonight and write a NUMERIC limit, because
Sanji rejects mid/bid/ask limits until he has chain snapshots.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date

import numpy as np

from signals import bs_price

STEPS = 26                 # 15-minute steps in a 6.5h session
SESSION_YEARS = 6.5 / 24 / 365
GAP_VAR_SHARE = 0.30       # share of a day's variance that happens overnight


@dataclass
class PricingConfig:
    n_paths: int = 3000
    continuation: float = 0.30          # this much of today's move carries into tomorrow
    max_tilt: float = 0.02              # cap on that drift, as a fraction of spot
    target_striker: dict = None         # signal points -> fill probability wanted
    target_sleeper: float = 0.45        # selling premium: no hurry, retry next night
    target_patient_exit: float = 0.55   # taking profit
    target_leg_in: float = 0.45         # adding the short leg to a long we already own
    sleeper_style: str = "market"       # "market" | "limit" (limit on the short leg and its take-profit)
    striker_style: str = "market"       # "market" | "protected" | "dip" | "marketable" | "split"
    target_marketable: float = 0.92     # the must-own part of a Striker entry
    target_protected: float = 0.98      # "protected": fill ~always, but cap a runaway gap
    protected_drift: float = 0.5        # ...assuming we're right: drift of this many daily sigmas our way

    def __post_init__(self):
        if self.target_striker is None:
            self.target_striker = {3: 0.60, 4: 0.70, 5: 0.80}


def tick(price: float) -> float:
    """Penny-pilot names (TSLA, NVDA, MSFT): $0.01 under $3, $0.05 above."""
    return 0.01 if price < 3 else 0.05


def round_to_tick(price: float, side: str) -> float:
    t = tick(price)
    f = math.floor if side == "buy" else math.ceil
    return round(max(0.01, f(price / t + 1e-9) * t), 2)


def simulate_bar(spot, strike, days_to_expiry_at_open, iv, is_call, sigma_daily, drift, n_paths, seed=0):
    """Tomorrow's option open/high/low/close on simulated paths."""
    rng = np.random.default_rng(seed)
    g_sd = sigma_daily * math.sqrt(GAP_VAR_SHARE)
    s_sd = sigma_daily * math.sqrt((1 - GAP_VAR_SHARE) / STEPS)
    gap = rng.standard_normal(n_paths) * g_sd + drift * GAP_VAR_SHARE - 0.5 * g_sd * g_sd
    steps = rng.standard_normal((n_paths, STEPS)) * s_sd + drift * (1 - GAP_VAR_SHARE) / STEPS - 0.5 * s_sd * s_sd
    log_path = math.log(spot) + gap[:, None] + np.concatenate([np.zeros((n_paths, 1)), np.cumsum(steps, axis=1)], axis=1)
    S = np.exp(log_path)
    T = np.maximum(days_to_expiry_at_open / 365 - np.arange(STEPS + 1) * SESSION_YEARS / STEPS, 1 / 365 / 24)
    V = bs_price(S, strike, T[None, :], iv, is_call)
    return V[:, 0], V.max(axis=1), V.min(axis=1), V[:, -1]


def sanji_fill(side, limit, o, h, l):
    """Vectorized Sanji rule. Returns (filled mask, fill price)."""
    if side == "buy":
        at_open = o <= limit
        filled = at_open | (l <= limit)
    else:
        at_open = o >= limit
        filled = at_open | (h >= limit)
    price = np.where(at_open, o, limit)
    return filled, price


def price_limit(side: str, row: dict, spot: float, for_day: date, sigma_daily: float, ret1: float | None,
                target: float, pcfg: PricingConfig | None = None, seed: int = 0,
                thesis_drift: float = 0.0, chase: bool = False) -> dict:
    """
    row: the contract from tonight's chain (strike, expiry, is_call, bid, ask, mid, iv)
    side: "buy" (BUY / BTC) or "sell" (SHT / SEL)
    Returns the limit plus what the model expects: fill probability, average fill, vs tonight's mid.
    """
    pcfg = pcfg or PricingConfig()
    mid, bid, ask = float(row["mid"]), float(row.get("bid") or 0), float(row.get("ask") or 0)
    if not ask or ask < mid:
        ask = mid * 1.05
    if not bid or bid > mid:
        bid = mid * 0.95
    tilt = 0.0 if ret1 is None else float(np.clip(pcfg.continuation * ret1, -pcfg.max_tilt, pcfg.max_tilt))
    tilt += thesis_drift
    dte_open = (row["expiry"] - for_day).days + 0.75
    o, h, l, _ = simulate_bar(spot, float(row["strike"]), dte_open, float(row["iv"]), bool(row["is_call"]),
                              sigma_daily, tilt, pcfg.n_paths, seed)

    # candidate prices: never pay above tonight's ask, never sell below tonight's bid
    if side == "buy":
        # chase=True: allowed to pay above tonight's ask (the open can gap through it)
        grid = np.linspace(max(0.01, 0.25 * mid), ask * (2.5 if chase else 1.0), 240)
    else:
        grid = np.linspace(bid, max(ask, mid) * 2.5, 240)[::-1]
    probs = np.array([sanji_fill(side, L, o, h, l)[0].mean() for L in grid])
    # buy: the lowest price that still meets the target; sell: the highest.
    ok = np.where(probs >= target)[0]
    if side == "buy":
        L = grid[ok[0]] if len(ok) else grid[-1]
    else:
        L = grid[ok[0]] if len(ok) else grid[-1]
    L = round_to_tick(L, side)
    filled, px = sanji_fill(side, L, o, h, l)
    p_fill = float(filled.mean())
    exp_fill = float(px[filled].mean()) if filled.any() else None
    edge = None
    if exp_fill is not None:
        edge = (mid - exp_fill) / mid if side == "buy" else (exp_fill - mid) / mid
    return {"limit": L, "fill_prob": round(p_fill, 3), "expected_fill": None if exp_fill is None else round(exp_fill, 3),
            "edge_vs_mid": None if edge is None else round(edge, 4), "tonight_bid": round(bid, 2),
            "tonight_ask": round(ask, 2), "tonight_mid": round(mid, 3), "tilt": round(tilt, 4), "target": target}


def pick_contract(pc, for_day: date, contract: str, pct: float, spot: float, min_days: int = 5,
                  expiry: date | None = None) -> dict | None:
    """The listed contract that Sanji's `5DTE+` and `pct:n` would name, chosen tonight.
    pc is signals.prepare_chain() output."""
    is_call = contract == "call"
    rows = pc[(pc["is_call"] == is_call) & (pc["mid"] > 0)]
    if expiry is None:
        exps = sorted(e for e in rows["expiry"].unique() if (e - for_day).days >= min_days)
        if not exps:
            return None
        expiry = exps[0]
    rows = rows[rows["expiry"] == expiry]
    if rows.empty:
        return None
    target = spot * (1 + pct / 100)
    # nearest; ties go further out of the money (same rule as Sanji's pct:)
    key = (rows["strike"] - target).abs() * 1e6 + (-rows["strike"] if is_call else rows["strike"]) * 1e-6
    r = rows.loc[key.idxmin()]
    return {"strike": float(r["strike"]), "expiry": r["expiry"], "is_call": is_call, "bid": float(r["bid"]),
            "ask": float(r["ask"]), "mid": float(r["mid"]), "iv": float(r["iv"]), "delta": float(r["delta"])}


def find_row(pc, contract: str, expiry: str, strike: float) -> dict | None:
    if pc is None or len(pc) == 0:
        return None
    e = date.fromisoformat(expiry) if isinstance(expiry, str) else expiry
    r = pc[(pc["is_call"] == (contract == "call")) & (pc["expiry"] == e) & ((pc["strike"] - strike).abs() < 1e-6)]
    if r.empty:
        return None
    r = r.iloc[0]
    return {"strike": float(r["strike"]), "expiry": r["expiry"], "is_call": bool(r["is_call"]), "bid": float(r["bid"]),
            "ask": float(r["ask"]), "mid": float(r["mid"]), "iv": float(r["iv"]), "delta": float(r["delta"])}
