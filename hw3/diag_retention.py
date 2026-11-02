"""
One-off diagnostic, not part of the pipeline. diag_week1.py showed the RIC format itself is
right (the real fetch found a genuine put for the Oct 30 expiry using this exact scheme) but
every candidate for the June 5 expiry comes back "universe is not found" on both daily and
hourly bars. That smells like a retention window on LSEG's side for EXPIRED option quote
history, not a format bug. This finds the boundary: for several past Fridays at increasing
distance from today, try ONE near-the-money candidate (both expired-suffix spellings, daily
interval only, to keep this cheap) and report the first one that comes back empty.

    python3 diag_retention.py
"""
import datetime as dt
import lseg.data as ld

ld.open_session()

today = dt.date.today()


def last_friday_weeks_back(n):
    d = today - dt.timedelta(days=1)
    while d.weekday() != 4:
        d -= dt.timedelta(days=1)
    return d - dt.timedelta(days=7 * n)


# Pull a day of INTC stock around each target Friday to get a real spot, so the candidate strike
# is plausible (avoids a false negative from guessing a silly-far-OTM strike).
weeks_back_list = [1, 2, 3, 4, 6, 8, 10, 13, 16, 19, 21]
for wb in weeks_back_list:
    F = last_friday_weeks_back(wb)
    mon = F - dt.timedelta(days=4)
    try:
        sdf = ld.get_history(universe="INTC.OQ", fields=["TRDPRC_1"], interval="daily",
                              start=mon.isoformat(), end=(F + dt.timedelta(days=1)).isoformat(), count=10)
        spot = float(sdf["TRDPRC_1"].dropna().iloc[0]) if sdf is not None and len(sdf) else None
    except Exception as e:
        spot = None
    if spot is None:
        print(f"weeks_back={wb:2d}  F={F}  (no stock spot - skipping)")
        continue
    strike = round(spot * 0.95)  # ~5% OTM put, whole dollar
    code = "MNOPQRSTUVWX"[F.month - 1]
    yy = F.strftime("%y")
    day = f"{F.day:02d}"
    base = f"INTC{code}{day}{yy}{int(strike*100):05d}.U"
    hit = False
    for ric in [f"{base}^{code}{yy}", base]:
        try:
            df = ld.get_history(universe=ric, fields=["BID", "ASK", "TRDPRC_1"], interval="daily",
                                 start=mon.isoformat(), end=(F + dt.timedelta(days=1)).isoformat(), count=10)
            if df is not None and len(df):
                hit = True
                print(f"weeks_back={wb:2d}  F={F}  spot={spot:.2f}  K={strike}  {ric}  -> rows={len(df)}  HIT")
                break
        except Exception as e:
            last_err = str(e)[:120]
    if not hit:
        print(f"weeks_back={wb:2d}  F={F}  spot={spot:.2f}  K={strike}  {base}  -> no data (last error: {last_err})")

ld.close_session()
