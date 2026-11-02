"""
The wheel's decision rules, as pure functions of price data only - no option prices, no
lookahead. Both fetch_data3.py (which has to decide, in order, which contract to pull
next) and build_book3.py (which independently re-derives every decision from the saved
raw bars for the Checks section) import this file, so the two can never quietly disagree
about what the rule says.

PUT leg (selling cash-secured puts while flat): the strike is set by a 3-tier realized-
vol regime, read off the stock alone, before any option is looked at.
  - Trailing realized vol: stdev of daily log returns over the last VOL_LOOKBACK_DAYS
    trading days (using each day's last regular-session bar as "the close"), annualized
    by sqrt(252). Only uses days strictly before the decision date - no lookahead.
  - Regime: compare this week's reading against the trailing window of up to
    REGIME_HISTORY prior weekly readings (also strictly before this week). Tertile split
    at the 33rd/67th percentile of that history. Fewer than MIN_REGIME_HISTORY prior
    readings (the first couple of weeks) -> default to "normal" (can't rank a
    distribution of 0-2 points).
  - OTM%: PUT_OTM[regime]. "elevated" vol -> close to the money (rich premium, a dip is
    plausible, happy to get assigned). "calm" -> far OTM (premium is thin, no edge in
    forcing an assignment).

CALL leg (selling covered calls after assignment, while holding shares): the strike
depends on the Monday spot vs. the shares' own cost basis (the put strike you were
assigned at) - a different, simpler signal than the vol regime.
  - spot >= cost basis (already profitable on the shares) -> tight OTM%, lean into
    getting called away and restarting the cycle.
  - spot <  cost basis (still underwater) -> wide OTM%, don't hand away the recovery
    cheaply.

Early close (either leg): a common practical risk rule - buy back a short option once
its own price has round-tripped to EARLY_CLOSE_MULT x what you collected for it, rather
than hold a loser to the last few cents of extrinsic value for a shrinking reward/risk.
Checked on every bar of the option's own week, first bar where it fires wins.
"""
import datetime as dt
import math
import pandas as pd

VOL_LOOKBACK_DAYS = 20          # trading days of daily closes behind each Monday decision
REGIME_HISTORY = 12             # weekly vol readings looked back over, for the tertile split
MIN_REGIME_HISTORY = 3          # fewer prior readings than this -> default regime "normal"

# Earnings blackout: don't open a new short option the week OF earnings, or the week before -
# a one-week premium-selling bet doesn't get paid enough to carry single-stock earnings gap
# risk. Dates are Intel's own confirmed IR release dates (after-market) for the quarters that
# fall inside this backtest's window.
EARNINGS_DATES = [dt.date(2026, 7, 23), dt.date(2026, 10, 29)]


def in_earnings_blackout(mon, earnings_dates=EARNINGS_DATES):
    """mon: the trade week's own Monday. True if this week is the week of, or the week
    immediately before, any date in earnings_dates."""
    for e in earnings_dates:
        e_mon = e - dt.timedelta(days=e.weekday())
        if mon == e_mon or mon == e_mon - dt.timedelta(days=7):
            return True
    return False

PUT_OTM = {"calm": 0.07, "normal": 0.045, "elevated": 0.02}
CALL_OTM = {"above_cost_basis": 0.02, "below_cost_basis": 0.06}

EARLY_CLOSE_MULT = 3.0   # tested at 2.0 first: with only a few days of extrinsic value left on a
                         # weekly option, a 2x move happens on a very ordinary swing and ended up
                         # preempting nearly every settlement - the wheel never actually got to
                         # assignment. 3x still closes out genuine blowups while leaving Friday's
                         # own settlement (assignment / expiry / called-away) room to be what
                         # usually decides the week, which is the point of backtesting the wheel.


def daily_closes(stock_df):
    """One row per regular-session day: that day's last bar. stock_df must be sorted,
    tz-naive-or-consistent, hourly, ET. Returns a Series indexed by date."""
    s = stock_df["TRDPRC_1"].dropna()
    if s.empty:
        return s
    by_day = s.groupby(s.index.date).last()
    return by_day


def trailing_realized_vol(closes_by_day, as_of_date, lookback=VOL_LOOKBACK_DAYS):
    """Annualized stdev of daily log returns over the LOOKBACK trading days strictly
    before as_of_date. None if there isn't enough history yet."""
    prior = closes_by_day[closes_by_day.index < as_of_date]
    if len(prior) < lookback + 1:
        return None
    window = prior.iloc[-(lookback + 1):]
    rets = [math.log(window.iloc[i] / window.iloc[i - 1]) for i in range(1, len(window))]
    n = len(rets)
    mean = sum(rets) / n
    var = sum((r - mean) ** 2 for r in rets) / (n - 1) if n > 1 else 0.0
    return math.sqrt(var) * math.sqrt(252)


def classify_regime(vol_today, prior_vol_readings):
    """prior_vol_readings: list of this same trailing-vol number computed at each
    earlier trade-week Monday, oldest first, NOT including today. Uses only the last
    REGIME_HISTORY of them. Tertile split at the 33rd/67th percentile."""
    if vol_today is None:
        return "normal"
    hist = [v for v in prior_vol_readings[-REGIME_HISTORY:] if v is not None]
    if len(hist) < MIN_REGIME_HISTORY:
        return "normal"
    s = sorted(hist)

    def pct(p):
        if len(s) == 1:
            return s[0]
        idx = p * (len(s) - 1)
        lo, hi = int(math.floor(idx)), int(math.ceil(idx))
        if lo == hi:
            return s[lo]
        return s[lo] + (s[hi] - s[lo]) * (idx - lo)

    p33, p67 = pct(1 / 3), pct(2 / 3)
    if vol_today <= p33:
        return "calm"
    if vol_today >= p67:
        return "elevated"
    return "normal"


def put_target_strike(spot, regime):
    return spot * (1 - PUT_OTM[regime])


def call_target_strike(spot, cost_basis):
    tier = "above_cost_basis" if spot >= cost_basis else "below_cost_basis"
    return spot * (1 + CALL_OTM[tier]), tier


def early_close_trigger(week_df, entry_premium, entry_ts, mult=EARLY_CLOSE_MULT):
    """week_df: that contract's own hourly bars for the week (BID/ASK/TRDPRC_1 columns),
    already restricted to bars at/after entry_ts. Returns (ts, mid) of the first bar
    whose mid >= mult x entry_premium, or None. mult defaults to the locked-in
    EARLY_CLOSE_MULT; fetch_data3.py and build_book3.py both call this with no override, so
    they're unaffected - sweep3.py passes an explicit mult to test alternates offline."""
    if entry_premium is None or entry_premium <= 0:
        return None
    after = week_df[week_df.index > entry_ts]
    for ts, row in after.iterrows():
        bid, ask = row.get("BID"), row.get("ASK")
        if pd.isna(bid) or pd.isna(ask):
            continue
        mid = (bid + ask) / 2
        if mid >= mult * entry_premium:
            return ts, mid
    return None
