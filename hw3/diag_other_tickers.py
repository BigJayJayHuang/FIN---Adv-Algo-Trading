"""
One-off diagnostic, not part of the pipeline. Re-checks the puts-vs-calls pattern on two other
tickers (SOFI and UUUU, this time pulled fresh/live rather than from HW1's old cached pickle) at
a past expiry (~10 weeks back), to see if the INTC result generalizes or was somehow INTC-specific.
For each ticker: pull a real spot, generate real candidate put AND call strikes with the same
logic already proven correct, and report hits for both sides side by side.

    python3 diag_other_tickers.py
"""
import datetime as dt
import lseg.data as ld

from rics import build_option_ric, candidate_strikes
from strategy import put_target_strike, call_target_strike

ld.open_session()

today = dt.date.today()


def last_friday_weeks_back(n):
    d = today - dt.timedelta(days=1)
    while d.weekday() != 4:
        d -= dt.timedelta(days=1)
    return d - dt.timedelta(days=7 * n)


def try_side(root, F, K, side, start, end):
    for expired, padded in [(False, False), (True, False), (False, True), (True, True)]:
        ric = build_option_ric(root, F, K, side, expired, padded)
        try:
            df = ld.get_history(universe=ric, fields=["BID", "ASK", "TRDPRC_1"], interval="daily",
                                 start=start, end=end, count=10)
        except Exception:
            df = None
        if df is not None and len(df):
            return ric, len(df)
    return None


for stock_ric, root in [("SOFI", "SOFI"), ("UUUU.K", "UUUU")]:
    print("=" * 70)
    print(f"{root}  ({stock_ric})")
    print("=" * 70)
    for wb in [10, 20]:
        F = last_friday_weeks_back(wb)
        mon = F - dt.timedelta(days=4)
        start, end = mon.isoformat(), (F + dt.timedelta(days=1)).isoformat()
        try:
            sdf = ld.get_history(universe=stock_ric, fields=["TRDPRC_1"], interval="daily", start=start, end=end, count=10)
            spot = float(sdf["TRDPRC_1"].dropna().iloc[0]) if sdf is not None and len(sdf) else None
        except Exception as e:
            spot = None
            print(f"  weeks_back={wb}: stock pull failed: {type(e).__name__}: {str(e)[:150]}")
            continue
        if spot is None:
            print(f"  weeks_back={wb}  F={F}: no stock spot, skipping")
            continue

        put_target = put_target_strike(spot, "normal")
        lo, hi = spot * (1 - 0.12), spot * (1 - 0.01)
        put_cands = sorted([k for k in candidate_strikes(lo, hi) if k <= put_target], key=lambda k: put_target - k)[:5]

        call_target, _ = call_target_strike(spot, spot)
        lo2, hi2 = spot * 1.01, spot * 1.12
        call_cands = sorted([k for k in candidate_strikes(lo2, hi2) if k >= call_target], key=lambda k: k - call_target)[:5]

        put_hit = None
        for K in put_cands:
            put_hit = try_side(root, F, K, "P", start, end)
            if put_hit:
                break
        call_hit = None
        for K in call_cands:
            call_hit = try_side(root, F, K, "C", start, end)
            if call_hit:
                break

        print(f"  weeks_back={wb:2d}  F={F}  spot={spot:7.2f}")
        print(f"    put  ({len(put_cands)} strikes tried):  {'HIT ' + str(put_hit) if put_hit else 'no hit'}")
        print(f"    call ({len(call_cands)} strikes tried):  {'HIT ' + str(call_hit) if call_hit else 'no hit'}")

ld.close_session()
