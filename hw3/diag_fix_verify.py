"""
Verification run for the rics.py fix (expired-put suffix must use the CALL letter for that
month, not the put's own letter). Before burning LSEG query budget on a full 22-week refetch,
this re-tries every case that previously failed zero-for-zero, now with the corrected suffix:

  1. The classmate-confirmed real example: UUUU Aug 14 2026 $15 put -> UUUUT142601500.U^H26
  2. The INTC June 5 2026 case that failed completely before (wide net of strikes, put side),
     including the exact strike (114) proven real via the matching call.
  3. A couple of the other UUUU/SOFI strikes from the earlier diagnostics, put side only.

Prints a clear HIT/no-hit summary per case and an overall verdict at the end.

    python3 diag_fix_verify.py
"""
import datetime as dt
import lseg.data as ld

from rics import build_option_ric

ld.open_session()

results = []


def try_put(root, F, K, label):
    mon = F - dt.timedelta(days=5)
    start, end = mon.isoformat(), (F + dt.timedelta(days=1)).isoformat()
    ric = build_option_ric(root, F, K, "P", expired=True, pad_day=True)
    try:
        df = ld.get_history(universe=ric, fields=["BID", "ASK", "TRDPRC_1"], interval="daily",
                             start=start, end=end, count=10)
        hit = df is not None and len(df) > 0
    except Exception as e:
        hit = False
        df = None
        print(f"  [{label}] {ric}  -> ERROR: {type(e).__name__}: {str(e)[:150]}")
    if hit:
        print(f"  [{label}] {ric}  -> HIT, {len(df)} rows")
        print(df.to_string())
    elif df is not None:
        print(f"  [{label}] {ric}  -> empty response (no rows, no error)")
    results.append((label, ric, hit))
    return hit


print("=== 1. Classmate-confirmed real example: UUUU Aug 14 2026 $15 put ===")
try_put("UUUU", dt.date(2026, 8, 14), 15.0, "UUUU $15p 8/14/26")

print("\n=== 2. INTC June 5 2026 put side, wide net (this expiry was 100% dead before) ===")
for K in [61.0, 72.0, 83.0, 89.0, 94.0, 100.0, 106.0, 111.0, 114.0, 117.0, 122.0, 133.0, 144.0, 156.0, 167.0]:
    try_put("INTC", dt.date(2026, 6, 5), K, f"INTC $%sp 6/5/26" % K)

print("\n=== 3. UUUU June 12 2026 $15 put (matching call side resolved fine before) ===")
try_put("UUUU", dt.date(2026, 6, 12), 15.0, "UUUU $15p 6/12/26")

print("\n=== 4. UUUU Aug 21 2026 $15 put (matching call side resolved fine before) ===")
try_put("UUUU", dt.date(2026, 8, 21), 15.0, "UUUU $15p 8/21/26")

ld.close_session()

n_hit = sum(1 for _, _, h in results if h)
print(f"\n{'=' * 60}")
print(f"VERDICT: {n_hit} of {len(results)} put cases resolved with the corrected suffix.")
if n_hit == len(results):
    print("ALL HIT. The fix looks correct — safe to re-run the full fetch_data3.py pipeline.")
elif n_hit > 0:
    print("PARTIAL — some hit, some didn't. Look at which ones failed before re-running the full fetch.")
else:
    print("STILL ZERO HITS. The suffix fix alone did not solve it — do not re-run the full fetch yet.")
