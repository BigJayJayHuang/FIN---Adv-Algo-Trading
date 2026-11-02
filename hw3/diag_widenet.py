"""
One-off diagnostic, not part of the pipeline. Every put candidate tried so far for the June 5
2026 expiry has been in the 1-12% OTM band (the band the actual strategy would search). This
casts a much wider net - strikes from deep ITM to deep OTM - to tell apart two different
explanations for zero hits:
  (a) puts just aren't retrievable historically on this account/feed at all (any strike, any
      liquidity), vs.
  (b) the specific near-the-money strikes we want happen to have had no recorded trading/quote
      activity that week (plausible for a less-active strike on a single week), and a wider net
      would find a real, if less useful, put somewhere.
Also tries the EXACT proven-real strike from the call side (114, confirmed listed that week via
INTCF052611400.U^F26) on the put side - exchanges list puts and calls at the same strikes, so if
114 is a real strike, the put at 114 should exist too, even if LSEG never recorded a trade on it.

    python3 diag_widenet.py
"""
import datetime as dt
import lseg.data as ld

from rics import build_option_ric

ld.open_session()

F = dt.date(2026, 6, 5)
mon = F - dt.timedelta(days=4)
spot = 111.08

# Deep ITM to deep OTM, $2.50 steps, roughly 50%-150% of spot.
strikes = [round(spot * m, 0) for m in [0.55, 0.65, 0.75, 0.80, 0.85, 0.90, 0.95, 1.0, 1.05, 1.10,
                                          1.15, 1.20, 1.30, 1.40, 1.50]]
strikes = sorted(set(strikes))
strikes.append(114.0)  # the exact proven-real call strike, tested on the put side too

n_hit = 0
for K in strikes:
    found = None
    for expired, padded in [(False, False), (True, False), (False, True), (True, True)]:
        ric = build_option_ric("INTC", F, K, "P", expired, padded)
        try:
            df = ld.get_history(universe=ric, fields=["BID", "ASK", "TRDPRC_1"], interval="daily",
                                 start=mon.isoformat(), end=(F + dt.timedelta(days=1)).isoformat(), count=10)
        except Exception:
            df = None
        if df is not None and len(df):
            found = (ric, len(df))
            break
    if found:
        print(f"K={K:7.2f}  {found[0]:28s} -> HIT, {found[1]} rows")
        n_hit += 1
    else:
        print(f"K={K:7.2f}  x4 spellings -> no hit")

print()
print(f"Summary: {n_hit} of {len(strikes)} put strikes resolved across a wide net "
      f"(55%-150% of spot) for the {F} expiry.")

ld.close_session()
