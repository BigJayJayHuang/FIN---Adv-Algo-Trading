"""
One-off diagnostic, not part of the pipeline. UUUU calls resolved at strike 15.00 for both
2026-08-21 and 2026-06-12 expiries (UUUUH212601500.U^H26 and UUUUF122601500.U^F26) - so 15.00 was
a real, listed strike both weeks. This tries the SAME exact strike on the PUT side for both
weeks, printing every RIC string attempted and the raw response/error, so we can both run the
test and eyeball the literal symbols for anything off.

    python3 diag_uuuu_15.py
"""
import datetime as dt
import lseg.data as ld

from rics import build_option_ric

ld.open_session()

cases = [
    (dt.date(2026, 8, 21), "H", "26"),  # matches the working call's month/year codes
    (dt.date(2026, 6, 12), "F", "26"),
]

for F, call_code, yy in cases:
    mon = F - dt.timedelta(days=4)
    start, end = mon.isoformat(), (F + dt.timedelta(days=1)).isoformat()
    print(f"\n=== expiry {F}  (call code was {call_code}) ===")
    for expired, padded in [(False, False), (True, False), (False, True), (True, True)]:
        ric = build_option_ric("UUUU", F, 15.0, "P", expired, padded)
        print(f"  trying: {ric!r}")
        try:
            df = ld.get_history(universe=ric, fields=["BID", "ASK", "TRDPRC_1"], interval="daily",
                                 start=start, end=end, count=10)
            if df is not None and len(df):
                print(f"    -> HIT, {len(df)} rows:")
                print(df.to_string())
            else:
                print(f"    -> empty response (no exception, but no rows)")
        except Exception as e:
            print(f"    -> ERROR: {type(e).__name__}: {str(e)[:250]}")

    # also print what the matching CALL ric looked like, for a direct side-by-side
    call_ric_expired = build_option_ric("UUUU", F, 15.0, "C", True, True)
    call_ric_plain = build_option_ric("UUUU", F, 15.0, "C", False, False)
    print(f"  (for reference, the matching call RICs this builds: {call_ric_expired!r} / {call_ric_plain!r})")

ld.close_session()
