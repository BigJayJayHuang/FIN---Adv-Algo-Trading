"""
Wide option-strike fetch for the parameter sweep (sweep3.py). Reuses the stock history already
in raw_data3.pkl (no need to re-pull it) and pulls OPTION quotes for EVERY strike in a generous
band, on BOTH sides (put and call), for EVERY trade week - not just the one contract the baseline
run actually traded. fetch_data3.py stops at the first strike that resolves; this one does not -
it wants the whole available band.

Why both sides, every week, regardless of what the baseline actually did: the wheel is stateful
(this week's side depends on last week's assignment/expiry/called-away outcome), so a different
OTM% or early-close threshold can send a LATER week down a different path that needs the opposite
side or a different strike. Fetching both sides' full band up front means sweep3.py can replay any
parameter combination's walk entirely offline, as long as its targets stay inside this band.

Costs real LSEG query time and does not stop early, so expect this to run noticeably longer than
fetch_data3.py's single-strike-per-week pull - this tries roughly 2x the strikes per week, keeping
every hit instead of stopping at the first one. Checkpoints after every week (like fetch_data3.py)
so a dropped session just resumes.

    python3 fetch_wide3.py           # pulls -> raw_wide3.pkl (git-ignored), resumable
    python3 fetch_wide3.py --fresh   # ignore raw_wide3.pkl and start over
"""
import argparse
import datetime as dt
import os
import pickle
import sys
import time

from rics import build_option_ric, candidate_strikes
import fetch_data3 as fd

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "raw_wide3.pkl")
RAW3 = os.path.join(HERE, "raw_data3.pkl")

# Generous OTM% bands - wide enough to cover every (put_scale, call_scale) combo sweep3.py tries.
# Baseline tiers: put {0.07, 0.045, 0.02}, call {0.02, 0.06}. Sweep scales those by 0.5x-2.0x, so
# the true range needed is put 1%-14%, call 1%-12%; a little margin added on both ends.
PUT_LO, PUT_HI = 0.01, 0.16
CALL_LO, CALL_HI = 0.01, 0.14


def resolve_all(ld, root, F, side, cands, start, end, tried):
    """Like fetch_data3.resolve_contract, but does NOT stop at the first hit - tries every
    candidate strike and returns {ric: df} for every one that resolved to a real quote."""
    found = {}
    for K in cands:
        hit = False
        for expired, padded in [(False, False), (True, False), (False, True), (True, True)]:
            ric = build_option_ric(root, F, K, side, expired, padded)
            tried.append(ric)
            df = fd.pull(ld, ric, start, end)
            if df is not None and df[["BID", "ASK"]].notna().any().any():
                found[ric] = df
                hit = True
                break
        if not hit:
            pass  # strike not listed / no quotes that week - fine, just skip it
    return found


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fresh", action="store_true")
    args = ap.parse_args()
    import lseg.data as ld

    if not os.path.exists(RAW3):
        sys.exit(f"{RAW3} not found - run fetch_data3.py first (this script reuses its stock data).")
    with open(RAW3, "rb") as f:
        base = pickle.load(f)
    stock = base["stock"]
    session_only = fd.session_only_factory(stock.index)
    stock_et_session = session_only(stock)

    expiries = fd.last_fridays(fd.TRADE_WEEKS)
    print(f"Wide fetch: {len(expiries)} weeks, both sides, put {PUT_LO:.0%}-{PUT_HI:.0%} OTM, "
          f"call {CALL_LO:.0%}-{CALL_HI:.0%} OTM.")

    options, tried, done = {}, [], []
    if os.path.exists(OUT) and not args.fresh:
        try:
            with open(OUT, "rb") as f:
                old = pickle.load(f)
            options.update(old["options"])
            tried.extend(old.get("tried", []))
            done = old["meta"].get("weeks_done", [])
            print(f"Resuming from {OUT}: {len(done)} weeks already done, {len(options)} contracts cached.")
        except Exception as e:
            print(f"Could not reuse {OUT} ({e}); starting over.")

    ld.open_session()
    t0 = time.time()
    for wi, F in enumerate(expiries, 1):
        if F.isoformat() in done:
            continue
        mon = F - dt.timedelta(days=4)
        start, end = mon.isoformat(), (F + dt.timedelta(days=1)).isoformat()
        spot, _ = fd.monday_spot(stock_et_session, mon)
        if spot is None:
            print(f"[{wi}/{len(expiries)}] {F}: no Monday stock bars; nothing to fetch this week.")
            done.append(F.isoformat())
            with open(OUT, "wb") as f:
                pickle.dump({"meta": {"weeks_done": done, "put_lo": PUT_LO, "put_hi": PUT_HI,
                                       "call_lo": CALL_LO, "call_hi": CALL_HI,
                                       "fetched_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
                             "options": options, "tried": tried}, f)
            continue

        put_cands = candidate_strikes(spot * (1 - PUT_HI), spot * (1 - PUT_LO))
        call_cands = candidate_strikes(spot * (1 + CALL_LO), spot * (1 + CALL_HI))
        put_hits = resolve_all(ld, fd.ROOT, F, "P", put_cands, start, end, tried)
        call_hits = resolve_all(ld, fd.ROOT, F, "C", call_cands, start, end, tried)
        options.update(put_hits)
        options.update(call_hits)
        print(f"[{wi}/{len(expiries)}] {F}: spot={spot:.2f}  puts {len(put_hits)}/{len(put_cands)} strikes hit, "
              f"calls {len(call_hits)}/{len(call_cands)} strikes hit  ({time.time()-t0:.0f}s)")

        done.append(F.isoformat())
        with open(OUT, "wb") as f:
            pickle.dump({"meta": {"weeks_done": done, "put_lo": PUT_LO, "put_hi": PUT_HI,
                                   "call_lo": CALL_LO, "call_hi": CALL_HI,
                                   "fetched_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
                         "options": options, "tried": tried}, f)

    ld.close_session()
    print(f"\nDone. {len(options)} total contracts cached across {len(done)} weeks.")
    print(f"Saved {OUT}")
    print("Next: python3 sweep3.py")


if __name__ == "__main__":
    main()
