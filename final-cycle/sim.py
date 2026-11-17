"""
A fake market for testing: a stock path plus a full option chain every night,
with "irregular" days planted on purpose so we can check the scanner finds them.

Day types
  normal        : ordinary two-sided volume, IV a bit above realized vol
  informed_bull : someone sweeps OTM calls at the ask, IV pops; price usually
                  drifts up over the next ~3 days (FOLLOW_THROUGH of the time)
  informed_bear : same with puts, price usually drifts down
  hedge_noise   : huge two-sided volume (straddle buying) - irregular, but no
                  direction. The algo should stand aside, not strike.
  iv_only       : IV jumps with no unusual flow (think pre-event bid).
                  Should never fire Striker.

This is a sandbox, not a claim about real markets: the informed flow here
predicts the move by construction. It tests that the code catches what it is
supposed to catch, and that orders, fills and exits behave.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, timedelta

import numpy as np
import pandas as pd

from signals import bs_price

DAY_TYPES = ["normal", "informed_bull", "informed_bear", "hedge_noise", "iv_only"]
DAY_PROBS = [0.74, 0.07, 0.07, 0.06, 0.06]
FOLLOW_THROUGH = 0.70
INTRADAY_STEPS = 26


def business_days(start: date, n: int) -> list[date]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


@dataclass
class Day:
    date: date
    open: float
    high: float
    low: float
    close: float
    atm_iv: float
    day_type: str
    worked: bool | None      # informed days: did the price follow?


class Market:
    def __init__(self, seed=0, n_days=260, s0=250.0, base_vol=0.50, strike_step=5.0,
                 start=date(2025, 1, 6), day_types=None):
        self.seed = seed
        self.rng = np.random.default_rng(seed)
        self.step = strike_step
        self.dates = business_days(start, n_days)
        rng = self.rng
        types = day_types or list(rng.choice(DAY_TYPES, size=n_days, p=DAY_PROBS))
        types[:25] = ["normal"] * 25              # warm-up for realized vol / IV history

        self.days: list[Day] = []
        self.paths: list[np.ndarray] = []
        close, iv_level = s0, base_vol * 1.12
        drift_left, drift_dir = 0, 0
        for i, d in enumerate(self.dates):
            t = types[i]
            vol = base_vol * (1.3 if drift_left > 0 else 1.0)
            drift = drift_dir * rng.uniform(0.02, 0.04) if drift_left > 0 else 0.0
            drift_left = max(0, drift_left - 1)
            z = rng.standard_normal()
            ret = drift + vol * z / math.sqrt(252)
            gap = 0.4 * ret + 0.002 * rng.standard_normal()
            o = close * math.exp(gap)
            c = o * math.exp(ret - gap)
            # intraday path: a Brownian bridge from the open to the close, 15-minute steps
            sd = vol / math.sqrt(252) * math.sqrt(0.7 / INTRADAY_STEPS)
            w = np.concatenate([[0.0], np.cumsum(rng.standard_normal(INTRADAY_STEPS) * sd)])
            frac = np.linspace(0, 1, INTRADAY_STEPS + 1)
            bridge = w - frac * w[-1]
            path = np.exp(math.log(o) + frac * (math.log(c) - math.log(o)) + bridge)
            self.paths.append(path)
            hi, lo = float(path.max()), float(path.min())
            close = c

            # IV: mean-reverts to ~12% over base vol, jumps on event nights
            iv_level += 0.3 * (base_vol * 1.12 - iv_level) + 0.01 * rng.standard_normal()
            worked = None
            if t in ("informed_bull", "informed_bear", "iv_only"):
                iv_level *= rng.uniform(1.12, 1.25)
            elif t == "hedge_noise":
                iv_level *= rng.uniform(1.00, 1.05)
            if t in ("informed_bull", "informed_bear"):
                worked = bool(rng.random() < FOLLOW_THROUGH)
                if worked:
                    drift_left, drift_dir = 3, (1 if t == "informed_bull" else -1)
            self.days.append(Day(d, o, hi, lo, c, float(np.clip(iv_level, 0.15, 2.0)), t, worked))

    # ------------------------------------------------------------------
    def expiries(self, i: int) -> list[date]:
        d = self.dates[i]
        fri = d + timedelta(days=(4 - d.weekday()) % 7)
        if fri == d:
            fri += timedelta(days=7)
        return [fri + timedelta(days=7 * k) for k in range(6)]

    def strikes(self, spot: float) -> np.ndarray:
        lo = math.floor(spot * 0.70 / self.step) * self.step
        hi = math.ceil(spot * 1.30 / self.step) * self.step
        return np.arange(lo, hi + self.step / 2, self.step)

    def iv_at(self, atm_iv, spot, k, dte):
        m = math.log(k / spot)
        smile = 1 - 0.8 * m + 1.5 * m * m          # put skew + a little smile
        term = 1 + 0.05 * (7 - min(dte, 35)) / 7    # front a bit richer
        return max(0.08, atm_iv * smile * term)

    def option_value(self, i_day: int, spot: float, k: float, expiry: date, is_call: bool, at_open=False):
        day = self.days[i_day]
        dte = (expiry - day.date).days + (0.75 if at_open else 0.0)
        if dte <= 0:
            return max(0.0, (spot - k) if is_call else (k - spot))
        iv = self.iv_at(day.atm_iv, spot, k, dte)
        return float(bs_price(spot, k, dte / 365, iv, is_call))

    def option_bar(self, i_day: int, k: float, expiry: date, is_call: bool):
        """The option's open / high / low / close on day i, valued along the intraday path."""
        day = self.days[i_day]
        path = self.paths[i_day]
        dte_open = (expiry - day.date).days + 0.75
        if dte_open <= 0.75 and expiry == day.date:
            dte_open = 0.27
        T = np.maximum(dte_open - np.linspace(0, 0.27, len(path)), 1 / 24) / 365
        iv = np.array([self.iv_at(day.atm_iv, s, k, max(dte_open, 0.5)) for s in path])
        v = bs_price(path, k, T, iv, is_call)
        return float(v[0]), float(v.max()), float(v.min()), float(v[-1])

    # ------------------------------------------------------------------
    def chain(self, i: int) -> pd.DataFrame:
        """The option chain as it looks after the close on day i."""
        rng = np.random.default_rng([self.seed, i, 7])
        day = self.days[i]
        S = day.close
        rows = []
        for e in self.expiries(i):
            dte = (e - day.date).days
            for k in self.strikes(S):
                m = math.log(k / S)
                for is_call in (True, False):
                    iv = self.iv_at(day.atm_iv, S, k, dte)
                    mid = float(bs_price(S, k, dte / 365, iv, is_call))
                    if mid < 0.02:
                        continue
                    half = max(0.025, 0.015 * mid)
                    bell = math.exp(-(m / 0.09) ** 2)
                    oi = 6000 * bell / (1 + dte / 20) * rng.uniform(0.6, 1.4)
                    oi *= 2.0 if k % 50 == 0 else 1.0
                    oi *= 1.0 if is_call else 0.6        # call-heavy near spot -> dealers long gamma
                    oi = int(oi) + int(rng.integers(5, 60))
                    vol = int(oi * rng.uniform(0.03, 0.45))
                    agg = rng.uniform(-1, 1)
                    rows.append([e, "C" if is_call else "P", float(k), mid - half, mid + half,
                                 mid + agg * half, vol, oi, iv])
        df = pd.DataFrame(rows, columns=["expiry", "type", "strike", "bid", "ask", "last", "volume", "oi", "iv"])

        # one or two random big prints on any day - noise the scanner must not chase
        for _ in range(int(rng.integers(0, 3))):
            j = int(rng.integers(0, len(df)))
            df.loc[j, "volume"] = int(df.loc[j, "oi"] * rng.uniform(2, 4) + 300)

        t = day.day_type
        near = df[[(e - day.date).days <= 12 for e in df["expiry"]]]
        if t in ("informed_bull", "informed_bear"):
            want_call = t == "informed_bull"
            pool = near[(near["type"] == ("C" if want_call else "P"))
                        & ((near["strike"] > S) if want_call else (near["strike"] < S))
                        & ((near["strike"] - S).abs() <= 6 * self.step)]
            idx = rng.choice(pool.index, size=min(len(pool), int(rng.integers(5, 9))), replace=False)
            for j in idx:
                df.loc[j, "volume"] = int(df.loc[j, "oi"] * rng.uniform(3, 8) + 1500)
                half = (df.loc[j, "ask"] - df.loc[j, "bid"]) / 2
                df.loc[j, "last"] = (df.loc[j, "ask"] + df.loc[j, "bid"]) / 2 + rng.uniform(0.6, 1.0) * half
        elif t == "hedge_noise":
            pool = near[((near["strike"] - S).abs() <= 4 * self.step)]
            ks = rng.choice(pool["strike"].unique(), size=min(5, pool["strike"].nunique()), replace=False)
            for k in ks:
                exps = sorted(pool[pool["strike"] == k]["expiry"].unique())
                e = exps[int(rng.integers(0, min(2, len(exps))))]
                both = df[(df["strike"] == k) & (df["expiry"] == e)].index
                v = int(rng.uniform(4000, 9000))
                for j in both:    # same size on the call and the put: a straddle
                    df.loc[j, "volume"] = int(df.loc[j, "oi"] * 2 + v)
                    half = (df.loc[j, "ask"] - df.loc[j, "bid"]) / 2
                    df.loc[j, "last"] = (df.loc[j, "ask"] + df.loc[j, "bid"]) / 2 + 0.8 * half
        return df

    def closes(self, i: int) -> pd.Series:
        return pd.Series([d.close for d in self.days[: i + 1]], index=[d.date for d in self.days[: i + 1]])
