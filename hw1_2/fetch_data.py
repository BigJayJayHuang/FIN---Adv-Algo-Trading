"""
FINTECH 535 HW1.2 - LSEG fetcher (AAPL covered call)
=====================================================
Run on the Mac, with LSEG Workspace open and signed in (same setup as HW1):

    cd site/hw1_2
    python3 fetch_data.py          # real pull  -> raw_data.pkl   (git-ignored)
    python3 build_book.py          # backtest   -> book.js        (this is what the site reads)

What it pulls, for the last N_WEEKS Friday expiries that have already expired:
  * the stock (AAPL.O), hourly bars
  * every call between spot-4% and spot+6% (whole-dollar and $2.50 strikes),
    hourly bars, Monday through the Friday expiry

Fields: BID, ASK (quotes exist even when nothing trades), and, only when a trade
happened in the bar, TRDPRC_1, OPEN_PRC, HIGH_1, LOW_1, ACVOL_UNS, NUM_MOVES.
Stock and options use the same bar size (INTERVAL).

RICs that do not exist just come back empty and are skipped. Nothing is invented.
Single-digit expiry days are tried as "AAPLH72630500" first (assignment spelling), then zero-padded
"AAPLH072630500". A week that already has data in raw_data.pkl is not pulled again, so re-running
only fills the gaps. Use  python fetch_data.py --fresh  to start over.
This file is not graded; the published site is.
"""
import argparse
import datetime as dt
import os
import pickle
import sys
import time
import warnings

import numpy as np
import pandas as pd

from rics import build_call_ric, candidate_strikes

warnings.filterwarnings("ignore", category=FutureWarning, module="lseg.data")

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "raw_data.pkl")

# ---- knobs -----------------------------------------------------------------
STOCK_RIC = "AAPL.O"
ROOT = "AAPL"               # RIC before the dot
INTERVAL = "hourly"         # same bar size for stock and options ("1min" also works, but is heavy)
INTERVAL_FALLBACKS = ["1h", "60min"]   # tried automatically if this LSEG library version rejects "hourly"
N_WEEKS = 10
BAND_LO = 0.04              # fetch strikes from spot*(1-4%) ...
BAND_HI = 0.06              # ... to spot*(1+6%), spot = that Monday's stock level
FIELDS = ["BID", "ASK", "TRDPRC_1", "OPEN_PRC", "HIGH_1", "LOW_1", "ACVOL_UNS", "NUM_MOVES"]
MIN_FIELDS = ["BID", "ASK", "TRDPRC_1"]   # fallback if a field is not entitled
# -----------------------------------------------------------------------------


def last_expiries(n: int, today: dt.date = None):
    """The n most recent Fridays strictly before today (so they are expired)."""
    today = today or dt.date.today()
    d = today - dt.timedelta(days=1)
    while d.weekday() != 4:
        d -= dt.timedelta(days=1)
    return [d - dt.timedelta(days=7 * i) for i in range(n)][::-1]


def tidy(df):
    """Normalise whatever get_history hands back into: index=timestamp, columns=FIELDS."""
    if df is None or len(df) == 0:
        return None
    df = df.copy()
    if isinstance(df.columns, pd.MultiIndex):
        for lvl in range(df.columns.nlevels):
            if set(df.columns.get_level_values(lvl)) & set(FIELDS):
                df.columns = df.columns.get_level_values(lvl)
                break
    df.index = pd.to_datetime(df.index)
    df.index.name = "ts"
    for f in FIELDS:
        if f not in df.columns:
            df[f] = np.nan
    df = df[FIELDS].apply(pd.to_numeric, errors="coerce")
    df = df.dropna(how="all")
    df = df[~df.index.duplicated(keep="last")].sort_index()
    return df if len(df) else None


LAST_ERROR = {"msg": None}


def pull(ld, ric, start, end):
    """One instrument, one window. Returns a tidy frame or None (empty / not found)."""
    attempts = [(FIELDS, True), (FIELDS, False), (MIN_FIELDS, False)]
    for fields, with_count in attempts:
        kw = dict(universe=ric, fields=fields, interval=INTERVAL, start=start, end=end)
        if with_count:
            kw["count"] = 10000          # make sure the window is not cut short by a default row limit
        try:
            df = ld.get_history(**kw)
        except Exception as e:           # bad kwarg / field not entitled / RIC not found: try the next shape
            LAST_ERROR["msg"] = f"{type(e).__name__}: {e}"
            continue
        return tidy(df)
    return None


def main():
    global INTERVAL
    ap = argparse.ArgumentParser()
    ap.add_argument("--fresh", action="store_true", help="ignore raw_data.pkl and pull every week again")
    args = ap.parse_args()
    import lseg.data as ld

    expiries = last_expiries(N_WEEKS)
    print(f"Expiries: {expiries[0]} .. {expiries[-1]}  ({len(expiries)} weeks), interval={INTERVAL}")
    ld.open_session()

    probe_start = (expiries[0] - dt.timedelta(days=4)).isoformat()
    probe_end = (expiries[0] + dt.timedelta(days=1)).isoformat()
    for cand in [INTERVAL] + INTERVAL_FALLBACKS:
        INTERVAL = cand
        probe = pull(ld, STOCK_RIC, probe_start, probe_end)
        if probe is not None:
            print(f"Interval '{INTERVAL}' works: {len(probe)} stock bars in week 1.")
            print(f"  first stamps: {[str(t) for t in probe.index[:3]]}   (checked later: UTC vs ET is auto-detected)")
            print(f"  last  stamps: {[str(t) for t in probe.index[-2:]]}")
            break
    else:
        ld.close_session()
        sys.exit(f"No stock bars for {STOCK_RIC} with any interval name. Last error: {LAST_ERROR['msg']}")

    stock_parts, options, tried, week_form, week_meta = [], {}, [], {}, {}
    if os.path.exists(OUT) and not args.fresh:
        try:
            with open(OUT, "rb") as f:
                old = pickle.load(f)
            stock_parts.append(old["stock"])
            options.update(old["options"])
            tried.extend(old.get("tried", []))
            week_meta.update(old["meta"].get("weeks", {}))
            for k, v in old["meta"].get("week_form", {}).items():
                if isinstance(v, str):          # older raw_data.pkl stored "expired" / "live"
                    v = (v == "expired", False)
                week_form[dt.date.fromisoformat(k)] = tuple(v)
            print(f"Resuming from {OUT}: weeks that already have option data will be kept.")
        except Exception as e:
            print(f"Could not reuse the old raw_data.pkl ({e}); pulling everything.")
    t0 = time.time()

    for wi, F in enumerate(expiries, 1):
        mon = F - dt.timedelta(days=4)
        start, end = mon.isoformat(), (F + dt.timedelta(days=1)).isoformat()

        if week_meta.get(F.isoformat(), {}).get("rics_ok", 0) > 0:
            print(f"[{wi}/{len(expiries)}] {F}: already have {week_meta[F.isoformat()]['rics_ok']} option RICs, keeping")
            continue

        s = pull(ld, STOCK_RIC, start, end)
        if s is None:
            print(f"[{wi}/{len(expiries)}] {F}: NO STOCK BARS for {STOCK_RIC}; skipping the week")
            continue
        stock_parts.append(s)

        monday_rows = s[s.index.date == mon]
        ref = monday_rows["TRDPRC_1"].dropna()
        if ref.empty:                      # Monday holiday: use the first day we have
            first_day = s.index.date[0]
            ref = s[s.index.date == first_day]["TRDPRC_1"].dropna()
        spot = float(ref.median())
        lo, hi = spot * (1 - BAND_LO), spot * (1 + BAND_HI)
        strikes = candidate_strikes(lo, hi)
        print(f"[{wi}/{len(expiries)}] {F}: stock {len(s)} bars, ref spot {spot:.2f}, trying {len(strikes)} strikes {lo:.1f}-{hi:.1f}")

        n_ok = 0
        # spellings to try: (expired ^ suffix?, zero-padded day?). The one that works first sticks for the week.
        spellings = [(e, p) for e in (True, False) for p in ((False, True) if F.day < 10 else (False,))]
        for K in strikes:
            forms = [week_form[F]] if F in week_form else spellings
            for expired, padded in forms:
                ric = build_call_ric(ROOT, F, K, expired, padded)
                tried.append(ric)
                df = pull(ld, ric, start, end)
                if df is not None and df[["BID", "ASK"]].notna().any().any():
                    options[ric] = df
                    week_form[F] = (expired, padded)
                    n_ok += 1
                    break
        if n_ok == 0:
            print(f"        !! nothing resolved. Example RICs tried: "
                  f"{build_call_ric(ROOT, F, round(spot / 2.5) * 2.5, True, False)}, "
                  f"{build_call_ric(ROOT, F, round(spot / 2.5) * 2.5, True, True)}, "
                  f"{build_call_ric(ROOT, F, round(spot / 2.5) * 2.5, False, False)}")
        week_meta[F.isoformat()] = {"ref_spot": spot, "strikes_tried": len(strikes), "rics_ok": n_ok}
        print(f"        -> {n_ok} option RICs with data   ({time.time() - t0:.0f}s elapsed)")

    ld.close_session()

    if not stock_parts:
        sys.exit("No stock data came back. Is LSEG Workspace open and signed in?")

    stock = pd.concat(stock_parts).sort_index()
    stock = stock[~stock.index.duplicated(keep="last")]

    payload = {
        "meta": {
            "stock_ric": STOCK_RIC,
            "root": ROOT,
            "interval": "hourly" if INTERVAL in ("hourly", "1h", "60min") else INTERVAL,
            "interval_arg": INTERVAL,
            "expiries": [d.isoformat() for d in expiries],
            "fetched_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "source": "LSEG",
            "demo": False,
            "weeks": week_meta,
            "week_form": {k.isoformat(): v for k, v in week_form.items()},
        },
        "stock": stock,
        "options": options,
        "tried": tried,
    }
    with open(OUT, "wb") as f:
        pickle.dump(payload, f)
    print(f"\nDone. {len(stock)} stock bars, {len(options)} option RICs with data out of {len(tried)} lookups.")
    print(f"Saved {OUT}\nNext: python3 build_book.py")


if __name__ == "__main__":
    main()
