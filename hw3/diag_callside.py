"""
One-off diagnostic, not part of the pipeline. Puts have found nothing for the June 5 2026 expiry
through get_history OR historical_pricing, across every spelling. This is the control: try CALLS
at the exact same expiry, same distance back, same ticker, same candidate strikes/spellings. If
calls resolve here and puts don't, that pins the problem on puts specifically (matching HW1's own
UUUU data: calls worked 3 months back, puts never worked at all) - not on INTC, not on how far
back this is, not on get_history vs historical_pricing.

    python3 diag_callside.py
"""
import datetime as dt
import lseg.data as ld

from rics import build_option_ric, candidate_strikes
from strategy import call_target_strike

ld.open_session()

F = dt.date(2026, 6, 5)
mon = F - dt.timedelta(days=4)
spot = 111.08  # real Monday spot for this week, from raw_data3.pkl

# Mirror the put test's band, just on the call side (a few % OTM above spot) so this is a fair
# same-distance, same-liquidity comparison.
target, _ = call_target_strike(spot, spot)  # spot==cost_basis -> "above_cost_basis" tight 2% OTM tier
lo, hi = spot * 1.01, spot * 1.12
cands = sorted([k for k in candidate_strikes(lo, hi) if k >= target], key=lambda k: k - target)[:8]

n_hit = 0
for K in cands:
    for expired, padded in [(False, False), (True, False), (False, True), (True, True)]:
        ric = build_option_ric("INTC", F, K, "C", expired, padded)
        try:
            df = ld.get_history(universe=ric, fields=["BID", "ASK", "TRDPRC_1"], interval="daily",
                                 start=mon.isoformat(), end=(F + dt.timedelta(days=1)).isoformat(), count=10)
        except Exception:
            df = None
        if df is not None and len(df):
            print(f"{ric:28s} -> HIT, {len(df)} rows")
            n_hit += 1
            break
    else:
        print(f"{ric:28s} x4 spellings -> no hit")
        continue
    break  # stop at first hit for a strike, but keep trying other strikes below

print()
print(f"Summary: {'at least one CALL resolved' if n_hit else 'NO calls resolved either'} for the {F} expiry.")

ld.close_session()
