"""
Where the nightly job gets its data. Every function returns plain pandas /
python objects and raises on failure, so algo.py can try the next source.

Option chains (IV, open interest, volume, bid/ask/last):
  1. CBOE delayed quotes  (free JSON, whole chain in one call)
  2. yfinance             (fallback)

Daily closes (realized vol, trend):
  1. price_history.json   (the course server's IB bars - same prices Sanji fills on)
  2. LSEG                 (only if LSEG_APP_KEY / LSEG_USERNAME / LSEG_PASSWORD secrets exist)
  3. yfinance             (fallback)
"""
from __future__ import annotations

import os
import re
from datetime import date, datetime, timedelta

import pandas as pd

UA = {"User-Agent": "Mozilla/5.0 (FINTECH535 nightly; educational use)"}
OCC = re.compile(r"^([A-Z.]+?)(\d{6})([CP])(\d{8})$")


# ----------------------------------------------------------------------------
# option chains
# ----------------------------------------------------------------------------
def cboe_chain(symbol: str, max_dte: int = 40) -> dict:
    import requests
    url = f"https://cdn.cboe.com/api/global/delayed_quotes/options/{symbol.upper()}.json"
    r = requests.get(url, headers=UA, timeout=30)
    r.raise_for_status()
    data = r.json().get("data") or {}
    spot = data.get("current_price") or data.get("close")
    if not spot:
        raise RuntimeError("CBOE: no underlying price")
    today = date.today()
    rows = []
    for o in data.get("options", []):
        m = OCC.match(str(o.get("option", "")))
        if not m:
            continue
        exp = datetime.strptime(m.group(2), "%y%m%d").date()
        if (exp - today).days > max_dte:
            continue
        rows.append({
            "expiry": exp, "type": m.group(3), "strike": int(m.group(4)) / 1000.0,
            "bid": o.get("bid"), "ask": o.get("ask"), "last": o.get("last_trade_price"),
            "volume": o.get("volume"), "oi": o.get("open_interest"), "iv": o.get("iv"),
        })
    if not rows:
        raise RuntimeError("CBOE: empty chain")
    return {"chain": pd.DataFrame(rows), "spot": float(spot), "source": "CBOE delayed quotes",
            "extra": {"iv30": data.get("iv30"), "iv30_change": data.get("iv30_change"),
                      "prev_close": data.get("prev_day_close"),
                      "quote_time": data.get("last_trade_time") or r.json().get("timestamp")}}


def yf_chain(symbol: str, max_dte: int = 40) -> dict:
    import yfinance as yf
    t = yf.Ticker(symbol)
    hist = t.history(period="5d")
    if hist.empty:
        raise RuntimeError("yfinance: no price")
    spot = float(hist["Close"].iloc[-1])
    today = date.today()
    frames = []
    for e in t.options:
        exp = datetime.strptime(e, "%Y-%m-%d").date()
        if (exp - today).days > max_dte:
            continue
        ch = t.option_chain(e)
        for typ, df in (("C", ch.calls), ("P", ch.puts)):
            frames.append(pd.DataFrame({
                "expiry": exp, "type": typ, "strike": df["strike"], "bid": df["bid"], "ask": df["ask"],
                "last": df["lastPrice"], "volume": df["volume"], "oi": df["openInterest"],
                "iv": df["impliedVolatility"]}))
    if not frames:
        raise RuntimeError("yfinance: empty chain")
    return {"chain": pd.concat(frames, ignore_index=True), "spot": spot, "source": "yfinance", "extra": {}}


def get_chain(symbol: str) -> dict:
    errors = []
    for fn in (cboe_chain, yf_chain):
        try:
            return fn(symbol)
        except Exception as exc:          # noqa: BLE001 - try the next source
            errors.append(f"{fn.__name__}: {exc}")
    raise RuntimeError("; ".join(errors))


# ----------------------------------------------------------------------------
# daily closes
# ----------------------------------------------------------------------------
def server_closes(price_history: dict, conid: int) -> pd.Series:
    bars = (price_history.get(str(conid)) or {}).get("price_history") or {}
    s = pd.Series({date.fromisoformat(d): v.get("close") for d, v in bars.items() if v.get("close")}, dtype=float)
    return s.sort_index()


def lseg_available() -> bool:
    return all(os.environ.get(k) for k in ("LSEG_APP_KEY", "LSEG_USERNAME", "LSEG_PASSWORD"))


_lseg_session = None


def _lseg():
    """Platform session, same pattern as the course's nightly_refresh.py."""
    global _lseg_session
    import lseg.data as ld
    if _lseg_session is None:
        _lseg_session = ld.session.platform.Definition(
            app_key=os.environ["LSEG_APP_KEY"],
            grant=ld.session.platform.GrantPassword(username=os.environ["LSEG_USERNAME"],
                                                    password=os.environ["LSEG_PASSWORD"]),
            signon_control=True,
        ).get_session()
        _lseg_session.open()
        ld.session.set_default(_lseg_session)
    return ld


def lseg_close_session():
    global _lseg_session
    if _lseg_session is not None:
        try:
            _lseg_session.close()
        finally:
            _lseg_session = None


def lseg_closes(ric: str, days: int = 120) -> pd.Series:
    ld = _lseg()
    df = ld.get_history(universe=ric, fields=["TRDPRC_1"], interval="daily",
                        start=(date.today() - timedelta(days=days)).isoformat(), end=date.today().isoformat())
    if df is None or df.empty:
        raise RuntimeError(f"LSEG: no history for {ric}")
    col = df.columns[0]
    s = pd.to_numeric(df[col], errors="coerce").dropna()
    s.index = [pd.Timestamp(i).date() for i in s.index]
    return s


def yf_closes(symbol: str) -> pd.Series:
    import yfinance as yf
    h = yf.Ticker(symbol).history(period="6mo")
    if h.empty:
        raise RuntimeError("yfinance: no history")
    s = h["Close"].astype(float)
    s.index = [pd.Timestamp(i).date() for i in s.index]
    return s


def get_closes(symbol: str, ric: str | None, price_history: dict, conid: int) -> tuple[pd.Series, list[str]]:
    """Server bars first; older days filled from LSEG or yfinance."""
    srv = server_closes(price_history, conid)
    used = ["price_history.json"] if len(srv) else []
    if len(srv) >= 25:
        return srv, used
    other, notes = None, []
    if lseg_available() and ric:
        try:
            other = lseg_closes(ric)
            used.append("LSEG")
        except Exception as exc:          # noqa: BLE001
            notes.append(f"LSEG failed: {exc}")
    if other is None:
        try:
            other = yf_closes(symbol)
            used.append("yfinance")
        except Exception as exc:          # noqa: BLE001
            notes.append(f"yfinance failed: {exc}")
    if other is None:
        return srv, used + notes
    merged = pd.concat([other[~other.index.isin(srv.index)], srv]).sort_index()
    return merged, used + notes


def next_earnings(symbol: str) -> date | None:
    try:
        import yfinance as yf
        cal = yf.Ticker(symbol).calendar
        ds = cal.get("Earnings Date") if isinstance(cal, dict) else None
        ds = [pd.Timestamp(d).date() for d in (ds or [])]
        ds = [d for d in ds if d >= date.today()]
        return min(ds) if ds else None
    except Exception:                     # noqa: BLE001 - optional
        return None
