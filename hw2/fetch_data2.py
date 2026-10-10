"""
HW2 - LSEG fetcher (DAL poor man's covered call)
=================================================
Run on the Mac, with LSEG Workspace open and signed in (same setup as HW1/HW1.2):

    cd site/hw2
    python3 fetch_data2.py          # real pull  -> raw_data2.pkl   (git-ignored)
    python3 build_book2.py          # backtest   -> book2.js        (this is what the site reads)

What it pulls:
  * the stock (DAL.N), hourly bars, for the last N_WEEKS expired Fridays (same window as HW1.2)
  * every call near spot+3% (whole-dollar and $2.50 strikes), hourly, for each of those Fridays
    - this is the weekly SHORT call leg, same strike rule and fill convention as HW1.2
  * ONE longer-dated, deep-in-the-money call bought once at the start of the window and held
    through the whole backtest - this is the LONG call leg ("poor man's" stock replacement).
    Target: the 3rd-Friday (standard monthly) expiry closest to LONG_MONTHS_OUT months after the
    window start, strike = the listed strike closest to spot x LONG_ITM_TARGET (~20% in the money).
    LSEG's hourly history has no delta field, so "deep ITM" is approximated by moneyness - the
    write-up says this plainly.

RICs that do not exist just come back empty and are skipped. Nothing is invented.
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
OUT = os.path.join(HERE, "raw_data2.pkl")

# ---- knobs -----------------------------------------------------------------
STOCK_RIC = "DAL.N"         # Delta Air Lines, NYSE. Change here if your feed spells it differently.
ROOT = "DAL"                # RIC root before the month/day/strike
INTERVAL = "hourly"
INTERVAL_FALLBACKS = ["1h", "60min"]
N_WEEKS = 10                 # short-call weeks, same as HW1.2
SHORT_OTM = 0.03             # weekly short call target: first listed strike at/above spot x 1.03
BAND_LO = 0.02               # short-call strike band around spot: spot*(1-LO) ..
BAND_HI = 0.08               # .. spot*(1+HI)  (need headroom above the 3% OTM target)
LONG_MONTHS_OUT = 6          # long call: ~6 months out from the window start
LONG_ITM_TARGET = 0.20       # long call: strike ~20% below spot (deep ITM "stock replacement")
LONG_BAND = (0.10, 0.32)     # try strikes this far ITM if the first target has no data
FIELDS = ["BID", "ASK", "TRDPRC_1", "OPEN_PRC", "HIGH_1", "LOW_1", "ACVOL_UNS", "NUM_MOVES"]
MIN_FIELDS = ["BID", "ASK", "TRDPRC_1"]
# -----------------------------------------------------------------------------


def last_expiries(n: int, today: dt.date = None):
    today = today or dt.date.today()
    d = today - dt.timedelta(days=1)
    while d.weekday() != 4:
        d -= dt.timedelta(days=1)
    return [d - dt.timedelta(days=7 * i) for i in range(n)][::-1]


def third_friday(year: int, month: int) -> dt.date:
    d = dt.date(year, month, 1)
    first_friday = d + dt.timedelta(days=(4 - d.weekday()) % 7)
    return first_friday + dt.timedelta(days=14)


def add_months(d: dt.date, n: int) -> dt.date:
    m = d.month - 1 + n
    y = d.year + m // 12
    m = m % 12 + 1
    return dt.date(y, m, 1)


def tidy(df):
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
    attempts = [(FIELDS, True), (FIELDS, False), (MIN_FIELDS, False)]
    for fields, with_count in attempts:
        kw = dict(universe=ric, fields=fields, interval=INTERVAL, start=start, end=end)
        if with_count:
            kw["count"] = 10000
        try:
            df = ld.get_history(**kw)
        except Exception as e:
            LAST_ERROR["msg"] = f"{type(e).__name__}: {e}"
            continue
        return tidy(df)
    return None


def main():
    global INTERVAL
    ap = argparse.ArgumentParser()
    ap.add_argument("--fresh", action="store_true", help="ignore raw_data2.pkl and pull every week again")
    args = ap.parse_args()
    import lseg.data as ld

    expiries = last_expiries(N_WEEKS)
    window_start = expiries[0] - dt.timedelta(days=4)
    print(f"Short-call expiries: {expiries[0]} .. {expiries[-1]}  ({len(expiries)} weeks), interval={INTERVAL}")
    ld.open_session()

    probe_start = (expiries[0] - dt.timedelta(days=4)).isoformat()
    probe_end = (expiries[0] + dt.timedelta(days=1)).isoformat()
    for cand in [INTERVAL] + INTERVAL_FALLBACKS:
        INTERVAL = cand
        probe = pull(ld, STOCK_RIC, probe_start, probe_end)
        if probe is not None:
            print(f"Interval '{INTERVAL}' works: {len(probe)} stock bars in week 1.")
            break
    else:
        ld.close_session()
        sys.exit(f"No stock bars for {STOCK_RIC} with any interval name. Last error: {LAST_ERROR['msg']}\n"
                  f"If DAL's RIC is spelled differently on your feed (e.g. DAL.OQ), edit STOCK_RIC at the top of this file.")

    stock_parts, options, tried, week_meta = [], {}, [], {}
    long_df, long_meta = None, {}
    if os.path.exists(OUT) and not args.fresh:
        try:
            with open(OUT, "rb") as f:
                old = pickle.load(f)
            stock_parts.append(old["stock"])
            options.update(old["options"])
            tried.extend(old.get("tried", []))
            week_meta.update(old["meta"].get("weeks", {}))
            if old.get("long") is not None:
                long_df = old["long"]["df"]
                long_meta = old["meta"].get("long", {})
                print(f"Resuming from {OUT}: long call and any completed weeks will be kept.")
        except Exception as e:
            print(f"Could not reuse the old raw_data2.pkl ({e}); pulling everything.")
    t0 = time.time()

    # ---- weekly short-call legs (same shape as HW1.2's fetch) -----------------
    for wi, F in enumerate(expiries, 1):
        mon = F - dt.timedelta(days=4)
        start, end = mon.isoformat(), (F + dt.timedelta(days=1)).isoformat()

        if week_meta.get(F.isoformat(), {}).get("rics_ok", 0) > 0:
            print(f"[{wi}/{len(expiries)}] {F}: already have {week_meta[F.isoformat()]['rics_ok']} option RICs, keeping")
            continue

        s = pull(ld, STOCK_RIC, start, end)
        if s is None:
            print(f"[{wi}/{len(expiries)}] {F}: NO STOCK BARS; skipping the week")
            continue
        stock_parts.append(s)

        monday_rows = s[s.index.date == mon]
        ref = monday_rows["TRDPRC_1"].dropna()
        if ref.empty:
            first_day = s.index.date[0]
            ref = s[s.index.date == first_day]["TRDPRC_1"].dropna()
        spot = float(ref.median())
        lo, hi = spot * (1 - BAND_LO), spot * (1 + BAND_HI)
        strikes = candidate_strikes(lo, hi)
        print(f"[{wi}/{len(expiries)}] {F}: stock {len(s)} bars, ref spot {spot:.2f}, trying {len(strikes)} strikes {lo:.1f}-{hi:.1f}")

        n_ok = 0
        spellings = [(e, p) for e in (True, False) for p in ((False, True) if F.day < 10 else (False,))]
        for K in strikes:
            for expired, padded in spellings:
                ric = build_call_ric(ROOT, F, K, expired, padded)
                tried.append(ric)
                df = pull(ld, ric, start, end)
                if df is not None and df[["BID", "ASK"]].notna().any().any():
                    options[ric] = df
                    n_ok += 1
                    break
        if n_ok == 0:
            print(f"        !! nothing resolved for week {wi}.")
        week_meta[F.isoformat()] = {"ref_spot": spot, "strikes_tried": len(strikes), "rics_ok": n_ok}
        print(f"        -> {n_ok} option RICs with data   ({time.time() - t0:.0f}s elapsed)")

    if not stock_parts:
        ld.close_session()
        sys.exit("No stock data came back. Is LSEG Workspace open and signed in?")
    stock = pd.concat(stock_parts).sort_index()
    stock = stock[~stock.index.duplicated(keep="last")]

    # ---- long call leg: one contract, bought once, held the whole window ------
    if long_df is None:
        mon_rows = stock[stock.index.date == window_start]
        ref = mon_rows["TRDPRC_1"].dropna()
        if ref.empty:
            ref = stock[stock.index.date == stock.index.date[0]]["TRDPRC_1"].dropna()
        spot0 = float(ref.median())
        target_month = add_months(window_start, LONG_MONTHS_OUT)
        long_expiry = third_friday(target_month.year, target_month.month)
        target_strike = spot0 * (1 - LONG_ITM_TARGET)
        lo, hi = spot0 * (1 - LONG_BAND[1]), spot0 * (1 - LONG_BAND[0])
        cands = sorted(candidate_strikes(lo, hi), key=lambda k: abs(k - target_strike))
        print(f"\nLong call: spot {spot0:.2f} on {window_start}, target expiry {long_expiry} "
              f"(~{LONG_MONTHS_OUT} months out), target strike ~{target_strike:.2f} (~{LONG_ITM_TARGET*100:.0f}% ITM)")
        l_start, l_end = window_start.isoformat(), (expiries[-1] + dt.timedelta(days=1)).isoformat()
        found = None
        for K in cands:
            for expired, padded in [(False, False), (True, False), (False, True), (True, True)]:
                ric = build_call_ric(ROOT, long_expiry, K, expired, padded)
                tried.append(ric)
                df = pull(ld, ric, l_start, l_end)
                if df is not None and df[["BID", "ASK"]].notna().any().any():
                    found = (ric, K, df)
                    break
            if found:
                break
        if found is None:
            print("        !! no long call resolved in the target band. Try a different LONG_MONTHS_OUT / "
                  "LONG_ITM_TARGET at the top of this file, or widen LONG_BAND.")
        else:
            ric, K, df = found
            long_df = df
            long_meta = {"ric": ric, "strike": K, "expiry": long_expiry.isoformat(),
                         "entry_spot": spot0, "window_start": window_start.isoformat()}
            print(f"        -> long call {ric}  ({len(df)} bars)")

    ld.close_session()

    payload = {
        "meta": {
            "stock_ric": STOCK_RIC, "root": ROOT,
            "interval": "hourly" if INTERVAL in ("hourly", "1h", "60min") else INTERVAL,
            "interval_arg": INTERVAL,
            "expiries": [d.isoformat() for d in expiries],
            "fetched_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "source": "LSEG", "demo": False,
            "weeks": week_meta, "long": long_meta,
            "short_otm": SHORT_OTM,
        },
        "stock": stock, "options": options, "tried": tried,
        "long": None if long_df is None else {"df": long_df},
    }
    with open(OUT, "wb") as f:
        pickle.dump(payload, f)
    print(f"\nDone. {len(stock)} stock bars, {len(options)} option RICs with data out of {len(tried)} lookups.")
    print(f"Long call: {'none resolved - build_book2.py will fail until this is fixed' if long_df is None else long_meta['ric']}")
    print(f"Saved {OUT}\nNext: python3 build_book2.py")


if __name__ == "__main__":
    main()
