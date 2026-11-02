"""
One-off diagnostic, not part of the pipeline. New lead: maybe a put RIC doesn't resolve as a
lone, standalone get_history query, but DOES resolve when requested together with its matching
call in the same batched universe=[...] list - consistent with "the put data sits behind the
call data" in the returned structure. Tests the known-good UUUU case (call strike 15.00, expiry
2026-08-21, already confirmed alone) together with its matching put at the same strike/expiry, in
ONE combined call, and prints the full result so we can see exactly how it comes back.

    python3 diag_batched.py
"""
import datetime as dt
import lseg.data as ld

from rics import build_option_ric

ld.open_session()

F = dt.date(2026, 8, 21)
mon = F - dt.timedelta(days=4)
start, end = mon.isoformat(), (F + dt.timedelta(days=1)).isoformat()

call_ric = build_option_ric("UUUU", F, 15.0, "C", expired=True, pad_day=True)   # UUUUH212601500.U^H26
put_ric = build_option_ric("UUUU", F, 15.0, "P", expired=True, pad_day=True)    # UUUUT212601500.U^T26

print("call_ric:", call_ric)
print("put_ric :", put_ric)

print("\n=== BOTH together, one get_history call, universe=[call, put] ===")
try:
    df = ld.get_history(universe=[call_ric, put_ric], fields=["BID", "ASK", "TRDPRC_1"],
                         interval="daily", start=start, end=end, count=10)
    print("columns:", list(df.columns) if df is not None else None)
    print(df)
except Exception as e:
    print("ERROR:", type(e).__name__, str(e)[:300])

print("\n=== put alone again, for direct comparison in the same run ===")
try:
    df2 = ld.get_history(universe=[put_ric], fields=["BID", "ASK", "TRDPRC_1"],
                          interval="daily", start=start, end=end, count=10)
    print(df2)
except Exception as e:
    print("ERROR:", type(e).__name__, str(e)[:300])

print("\n=== put with a THIRD, unrelated real call thrown in too (bigger batch) ===")
call_ric2 = build_option_ric("UUUU", dt.date(2026, 6, 12), 15.0, "C", expired=True, pad_day=True)
try:
    df3 = ld.get_history(universe=[call_ric, put_ric, call_ric2], fields=["BID", "ASK", "TRDPRC_1"],
                          interval="daily", start="2026-06-01", end="2026-08-22", count=20)
    print("columns:", list(df3.columns) if df3 is not None else None)
    print(df3)
except Exception as e:
    print("ERROR:", type(e).__name__, str(e)[:300])

ld.close_session()
