"""
Offline re-verification of the vol_history ordering fix, run against the ALREADY-FETCHED
raw_data3.pkl - no new LSEG queries. Replays fetch's full walk-forward vol/regime sequence for
every trade week (using the corrected order: check Monday bar first, then compute vol_today) and
compares every week's regime/target against build_book3.py's independent recompute.

    python3 diag_fix_verify3.py
"""
import datetime as dt
import pickle
import pandas as pd
import fetch_data3 as fd
import build_book3 as bb
from strategy import trailing_realized_vol, classify_regime, VOL_LOOKBACK_DAYS, daily_closes

with open("raw_data3.pkl", "rb") as f:
    payload = pickle.load(f)

stock = payload["stock"]
et_conv = fd.detect_et(stock.index)


def session_only(df):
    d = df.copy()
    d.index = et_conv(d.index)
    m = d.index.hour * 60 + d.index.minute
    return d[(m >= fd.SESSION_LO) & (m <= fd.SESSION_HI) & (d.index.dayofweek < 5)]


stock_et_session = session_only(stock)
closes = daily_closes(stock_et_session)

expiries = [dt.date.fromisoformat(w["expiry"]) for w in payload["meta"]["weeks"]]

vol_history = []
fetch_regime = {}
for F in expiries:
    mon = F - dt.timedelta(days=4)
    spot, _ = fd.monday_spot(stock_et_session, mon)
    if spot is None:
        vol_history.append(None)
        continue
    vol_today = trailing_realized_vol(closes, mon, lookback=VOL_LOOKBACK_DAYS)
    regime = classify_regime(vol_today, vol_history)
    vol_history.append(vol_today)
    fetch_regime[F.isoformat()] = (vol_today, regime)

out, *_ = bb.resolve_weeks(payload, "INTC")
n_match, n_total = 0, 0
for w in out:
    exp = w["expiry"].isoformat()
    if w["side"] != "P":
        continue
    n_total += 1
    fv = fetch_regime.get(exp)
    ok = fv is not None and fv[1] == w["regime"]
    n_match += ok
    print(f"{exp}  fetch_regime={fv[1] if fv else None}  build_regime={w['regime']}  "
          f"fetch_vol={round(fv[0],4) if fv and fv[0] is not None else None}  build_vol={w['vol_today']}  "
          f"{'OK' if ok else 'MISMATCH'}")

print(f"\n{n_match}/{n_total} put-week regimes match build's independent recompute.")
