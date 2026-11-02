"""
One-off diagnostic, not part of the pipeline. get_history can't see puts (or maybe anything)
more than ~1 week past expiry. But lseg.data.content.historical_pricing.events found real tick
data for a known-good RIC (INTCV302608300.U) even when get_data's reference lookup couldn't -
it's a different backend. This checks whether historical_pricing has LONGER retention than plain
get_history for an option that expired months ago (week 1: June 5 2026), using the real candidate
strikes (same construction logic already confirmed correct against the live chain pull).

    python3 diag_historical.py
"""
import datetime as dt
import lseg.data as ld

from rics import build_option_ric, candidate_strikes
from strategy import put_target_strike

ld.open_session()

F = dt.date(2026, 6, 5)
mon = F - dt.timedelta(days=4)
spot = 111.08  # from the real raw_data3.pkl Monday spot for this week

target = put_target_strike(spot, "normal")
lo, hi = spot * (1 - 0.12), spot * (1 - 0.01)
cands = sorted([k for k in candidate_strikes(lo, hi) if k <= target], key=lambda k: target - k)[:8]
rics = []
for K in cands:
    for expired, padded in [(False, False), (True, False), (False, True), (True, True)]:
        rics.append(build_option_ric("INTC", F, K, "P", expired, padded))

print(f"Testing {len(rics)} candidate RICs for the {F} expiry via historical_pricing.events and .summaries\n")

from lseg.data.content.historical_pricing import events, summaries

for ric in rics:
    hit = False
    try:
        resp = events.Definition(universe=ric, start=mon.isoformat(), end=(F + dt.timedelta(days=1)).isoformat()).get_data()
        df = resp.data.df if resp is not None else None
        if df is not None and len(df):
            print(f"{ric:28s} events -> HIT, {len(df)} rows")
            hit = True
    except Exception as e:
        pass
    if not hit:
        try:
            resp = summaries.Definition(universe=ric, start=mon.isoformat(), end=(F + dt.timedelta(days=1)).isoformat(),
                                         interval=summaries.Intervals.DAILY).get_data()
            df = resp.data.df if resp is not None else None
            if df is not None and len(df):
                print(f"{ric:28s} summaries -> HIT, {len(df)} rows")
                hit = True
        except Exception as e:
            pass
    if not hit:
        print(f"{ric:28s} no hit (events or daily summaries)")

ld.close_session()
