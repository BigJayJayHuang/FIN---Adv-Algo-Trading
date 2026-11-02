"""
One-off diagnostic, not part of the pipeline. Brute-force guessing RIC strings for puts isn't
working (except right at expiry); your teammates got real put data some other way, so there's
presumably a proper chain/search lookup instead of guessing symbol strings. This explores what's
actually available in your installed lseg.data library and tries the most likely real methods -
first on week 22 (Oct 30 expiry), the one case we KNOW has a real put (INTCV302608300.U, strike
83) so we have ground truth to check any new method against, then on week 1 (June 5 expiry),
which has never resolved anything.

    python3 diag_chain.py

This only prints things - it doesn't write any files.
"""
import lseg.data as ld

ld.open_session()

print("=" * 70)
print("What's available under lseg.data and lseg.data.content:")
print("=" * 70)
print("ld:", [x for x in dir(ld) if not x.startswith("_")])
try:
    import lseg.data.content as content
    print("ld.content:", [x for x in dir(content) if not x.startswith("_")])
except Exception as e:
    print("no ld.content:", e)
try:
    import lseg.data.discovery as discovery
    print("ld.discovery:", [x for x in dir(discovery) if not x.startswith("_")])
except Exception as e:
    print("no ld.discovery:", e)

print()
print("=" * 70)
print("Attempt 1: chain RIC via ld.get_data (active/current chain only, likely)")
print("=" * 70)
for chain_ric in ["0#INTC*.U", "0#INTC*.OQ"]:
    try:
        resp = ld.get_data(universe=[chain_ric], fields=["DSPLY_NAME"])
        print(chain_ric, "->")
        print(resp)
    except Exception as e:
        print(chain_ric, "-> ERROR:", type(e).__name__, str(e)[:200])
    print("---")

print()
print("=" * 70)
print("Attempt 2: ld.discovery.search for INTC option instruments (if it exists)")
print("=" * 70)
try:
    import lseg.data.discovery as discovery
    resp = discovery.search(
        view=discovery.SearchViews.SEARCH_ALL,
        query="INTC option put",
        top=20,
    )
    print(resp)
except Exception as e:
    print("ERROR:", type(e).__name__, str(e)[:300])

print()
print("=" * 70)
print("Attempt 3: ld.content.search.Definition, filtered for INTC options (if it exists)")
print("=" * 70)
try:
    from lseg.data.content import search
    resp = search.Definition(
        view=search.Views.SEARCH_ALL,
        query="INTC",
        filter="RCSAssetCategoryLeaf eq 'Equity Option' and UnderlyingQuoteRIC eq 'INTC.OQ'",
        top=20,
    ).get_data()
    print(resp.data.df if resp is not None else "no response")
except Exception as e:
    print("ERROR:", type(e).__name__, str(e)[:300])

print()
print("=" * 70)
print("Attempt 4: known-good RIC (week 22, confirmed real put) via content.historical_pricing, "
      "if that module exists - to see what a WORKING call looks like through a different API")
print("=" * 70)
try:
    from lseg.data.content.historical_pricing import events
    resp = events.Definition(universe="INTCV302608300.U").get_data()
    print(resp.data.df if resp is not None else "no response")
except Exception as e:
    print("ERROR:", type(e).__name__, str(e)[:300])

print()
print("=" * 70)
print("Attempt 5: get_data reference fields directly on the known-good RIC AND an old guessed "
      "one, to compare - does get_data (not get_history) know about the old one at all?")
print("=" * 70)
for ric in ["INTCV302608300.U", "INTCR052610300.U", "INTCR052610300.U^R26"]:
    try:
        resp = ld.get_data(universe=[ric], fields=["DSPLY_NAME", "EXPIR_DATE", "STRIKE_PRC", "PUTCALLIND"])
        print(ric, "->")
        print(resp)
    except Exception as e:
        print(ric, "-> ERROR:", type(e).__name__, str(e)[:200])
    print("---")

ld.close_session()
