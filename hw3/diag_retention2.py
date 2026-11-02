"""
diag_retention.py tried exactly ONE guessed strike per week (spot * 0.95, rounded) and that
strike may simply never have been a real LISTED strike at all - weekly options only list a
discrete subset of strikes, so a single guess missing proves nothing. This instead replicates
EXACTLY what fetch_data3.py actually does: strategy.py's real put_target_strike() plus rics.py's
real candidate_strikes() (the full whole-dollar + $2.50 grid), tried closest-to-target-first,
both expired-suffix spellings, for a handful of weeks at increasing distance from today. If a
nearby week finds a hit among ~10 real candidates but a distant week finds nothing among the same
~10, that's retention, not bad luck on a single guess.

    python3 diag_retention2.py
"""
import datetime as dt
import lseg.data as ld

from rics import build_option_ric, candidate_strikes
from strategy import put_target_strike

ld.open_session()
today = dt.date.today()


def last_friday_weeks_back(n):
    d = today - dt.timedelta(days=1)
    while d.weekday() != 4:
        d -= dt.timedelta(days=1)
    return d - dt.timedelta(days=7 * n)


PUT_BAND = (0.01, 0.12)
weeks_back_list = [0, 1, 2, 4, 8, 13, 19, 21]

for wb in weeks_back_list:
    F = last_friday_weeks_back(wb)
    mon = F - dt.timedelta(days=4)
    start, end = mon.isoformat(), (F + dt.timedelta(days=1)).isoformat()
    try:
        sdf = ld.get_history(universe="INTC.OQ", fields=["TRDPRC_1"], interval="daily", start=start, end=end, count=10)
        spot = float(sdf["TRDPRC_1"].dropna().iloc[0]) if sdf is not None and len(sdf) else None
    except Exception:
        spot = None
    if spot is None:
        print(f"weeks_back={wb:2d}  F={F}  (no stock spot - skipping)")
        continue

    target = put_target_strike(spot, "normal")
    lo, hi = spot * (1 - PUT_BAND[1]), spot * (1 - PUT_BAND[0])
    cands = sorted([k for k in candidate_strikes(lo, hi) if k <= target], key=lambda k: target - k)[:10]

    tries, hit_ric, hit_rows = 0, None, 0
    for K in cands:
        for expired, padded in [(False, False), (True, False), (False, True), (True, True)]:
            ric = build_option_ric("INTC", F, K, "P", expired, padded)
            tries += 1
            try:
                df = ld.get_history(universe=ric, fields=["BID", "ASK", "TRDPRC_1"], interval="daily",
                                     start=start, end=end, count=10)
            except Exception:
                df = None
            if df is not None and len(df):
                hit_ric, hit_rows = ric, len(df)
                break
        if hit_ric:
            break

    if hit_ric:
        print(f"weeks_back={wb:2d}  F={F}  spot={spot:7.2f}  {tries:3d} tries  -> HIT {hit_ric} ({hit_rows} rows)")
    else:
        print(f"weeks_back={wb:2d}  F={F}  spot={spot:7.2f}  {tries:3d} tries  -> no hit among {len(cands)} strikes x 4 spellings")

ld.close_session()
