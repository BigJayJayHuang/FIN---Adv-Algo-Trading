"""
One-off diagnostic, not part of the pipeline. The real fetch found quote data for only the very
last trade week (closest to today) and nothing for the ~21 weeks further back, despite trying
~30 candidate strikes/spellings each. This checks why, on one known week, without burning another
full 22-week pull: does LSEG return rows at all for an old expired option RIC, with hourly vs.
daily bars, and - critically - what's the actual exception/error text when it doesn't (fetch_data3.py
was swallowing that into a variable and never printing it on a whole-week miss).

    python3 diag_week1.py
"""
import lseg.data as ld

ld.open_session()

# Week 1 in the real pull: expiry 2026-06-05, spot ~111.08, put strike band ~1-12% OTM -> roughly
# 98-110. Try a handful of real candidate RICs at both intervals, every spelling.
candidates = []
for K5 in [11000, 10500, 10000, 9500]:   # strike x 100
    for expired_suffix in ["^R26", ""]:
        for day in ["5", "05"]:
            candidates.append(f"INTCR{day}26{K5:05d}.U{expired_suffix}")

for interval in ["daily", "hourly"]:
    print(f"\n=== interval={interval} ===")
    for ric in candidates:
        try:
            df = ld.get_history(universe=ric, fields=["BID", "ASK", "TRDPRC_1"], interval=interval,
                                 start="2026-06-01", end="2026-06-06", count=50)
            n = 0 if df is None else len(df)
            print(f"{ric:28s} rows={n}")
            if n:
                print(df.head(3).to_string())
        except Exception as e:
            print(f"{ric:28s} ERROR: {type(e).__name__}: {str(e)[:200]}")

# Also: does the stock itself (never expires) have hourly bars that far back? Sanity check -
# we already know yes from raw_data3.pkl, but confirm live.
print("\n=== stock sanity check ===")
try:
    df = ld.get_history(universe="INTC.OQ", fields=["BID", "ASK", "TRDPRC_1"], interval="hourly",
                         start="2026-06-01", end="2026-06-06", count=50)
    print("INTC.OQ hourly rows:", 0 if df is None else len(df))
except Exception as e:
    print("INTC.OQ hourly ERROR:", type(e).__name__, str(e)[:200])

ld.close_session()
