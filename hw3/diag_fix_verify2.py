"""
Offline re-verification of the fetch_data3.py UTC->ET conversion fix for option-side timing
(entry fill + early close), run against the ALREADY-FETCHED raw_data3.pkl - no new LSEG queries.
Replays fetch's corrected logic for every option RIC already in the pickle and compares the
resulting entry_premium/early-close to build_book3.py's independent recompute (the ground truth).

    python3 diag_fix_verify2.py
"""
import pickle
import pandas as pd
import fetch_data3 as fd
import build_book3 as bb

with open("raw_data3.pkl", "rb") as f:
    payload = pickle.load(f)

stock = payload["stock"]
et_conv = fd.detect_et(stock.index)


def session_only(df):
    d = df.copy()
    d.index = et_conv(d.index)
    m = d.index.hour * 60 + d.index.minute
    return d[(m >= fd.SESSION_LO) & (m <= fd.SESSION_HI) & (d.index.dayofweek < 5)]


# Ground truth from build's own independent recompute.
out, *_ = bb.resolve_weeks(payload, "INTC")
truth = {w["ric"]: w for w in out if w.get("ric")}

n_match, n_total = 0, 0
for ric, odf in payload["options"].items():
    t = truth.get(ric)
    if t is None:
        continue
    mon = t["mon"]
    df_session = session_only(odf)
    entry_target = pd.Timestamp(mon.year, mon.month, mon.day, fd.ENTRY_HOUR, tz=fd.ET)
    entry_ts, entry_premium = fd.entry_fill(df_session, entry_target)
    early = fd.early_close_trigger(df_session, entry_premium, entry_ts) if entry_ts is not None else None
    n_total += 1
    ep_match = entry_premium is not None and t["entry_premium"] is not None and abs(entry_premium - t["entry_premium"]) < 1e-6
    early_match = (early is None) == (t.get("early_ts") is None)
    ok = ep_match and early_match
    n_match += ok
    flag = "OK" if ok else "MISMATCH"
    print(f"{ric:28s} fetch_entry={entry_premium}  build_entry={t['entry_premium']}  "
          f"fetch_early={'yes' if early else 'no'}  build_early={'yes' if t.get('early_ts') else 'no'}  {flag}")

print(f"\n{n_match}/{n_total} contracts match build's independent recompute exactly.")
