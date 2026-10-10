"""
Option RIC helpers (OPRA, calls only).

    {ROOT}{MONTH}{DAY}{YY}{STRIKE}.U{^MONTHYY if expired}

    MONTH  = call letter A..L for Jan..Dec (puts would be M..X, not used here)
    DAY    = day of month, NOT zero-padded
    YY     = two-digit year
    STRIKE = strike x 100, 5 digits, zero-padded
    .U     = OPRA
    expired options add ^{MONTH}{YY}, same letter

Examples from the assignment:
    AAPLF52619000.U^F26    AAPL Jun  5 2026, 190 call (expired)
    AAPLG172620000.U^G26   AAPL Jul 17 2026, 200 call (expired)
    AAPLH72620500.U^H26    AAPL Aug  7 2026, 205 call (expired)
"""
import datetime as dt
import re

CALL_LETTERS = "ABCDEFGHIJKL"


def call_letter(month: int) -> str:
    return CALL_LETTERS[month - 1]


def build_call_ric(root: str, expiry: dt.date, strike: float, expired: bool = True, pad_day: bool = False) -> str:
    """pad_day=True writes the day as two digits ("07"). The assignment says not padded, but if a
    feed only knows the padded spelling for days 1-9, fetch_data.py falls back to it."""
    code = call_letter(expiry.month)
    yy = expiry.strftime("%y")
    day = f"{expiry.day:02d}" if pad_day else f"{expiry.day}"
    base = f"{root}{code}{day}{yy}{int(round(strike * 100)):05d}.U"
    return base + (f"^{code}{yy}" if expired else "")


_RIC_RE = re.compile(
    r"^(?P<root>[A-Z]+?)(?P<code>[A-L])(?P<day>\d{1,2})(?P<yy>\d{2})(?P<strike>\d{5})\.U(?:\^(?P<ecode>[A-L])(?P<eyy>\d{2}))?$"
)


def parse_call_ric(ric: str):
    """-> dict(root, expiry, strike, expired) or None if it is not a call RIC."""
    m = _RIC_RE.match(ric)
    if not m:
        return None
    month = CALL_LETTERS.index(m.group("code")) + 1
    try:
        expiry = dt.date(2000 + int(m.group("yy")), month, int(m.group("day")))
    except ValueError:
        return None
    return {
        "root": m.group("root"),
        "expiry": expiry,
        "strike": int(m.group("strike")) / 100.0,
        "expired": m.group("ecode") is not None,
    }


def candidate_strikes(lo: float, hi: float):
    """Strikes worth trying between lo and hi.

    Union of the whole-dollar grid and the $2.50 grid (2.5, 7.5, 12.5 ... i.e.
    x.5 where x mod 5 == 2). A RIC that does not exist just returns nothing, so
    it is safe to over-ask; it is not safe to under-ask (we would miss the true
    nearest OTM strike).
    """
    out = []
    k = int(lo)
    while k <= hi + 1:
        if lo <= k <= hi:
            out.append(float(k))
        if lo <= k + 0.5 <= hi and (k % 5) == 2:
            out.append(k + 0.5)
        k += 1
    return out


if __name__ == "__main__":
    # self-test against the assignment's own examples
    assert build_call_ric("UUUU", dt.date(2026, 8, 21), 14.5) == "UUUUH212601450.U^H26"
    assert build_call_ric("AAPL", dt.date(2026, 6, 5), 190) == "AAPLF52619000.U^F26"
    assert build_call_ric("AAPL", dt.date(2026, 7, 17), 200) == "AAPLG172620000.U^G26"
    assert build_call_ric("AAPL", dt.date(2026, 8, 7), 205) == "AAPLH72620500.U^H26"
    assert build_call_ric("AAPL", dt.date(2026, 8, 7), 205, pad_day=True) == "AAPLH072620500.U^H26"
    p = parse_call_ric("AAPLH072620500.U^H26")
    assert p["strike"] == 205.0 and p["expiry"] == dt.date(2026, 8, 7)
    p = parse_call_ric("AAPLG172620000.U^G26")
    assert p["strike"] == 200.0 and p["expiry"] == dt.date(2026, 7, 17) and p["expired"]
    p = parse_call_ric("AAPLH72620500.U")
    assert p["strike"] == 205.0 and p["expiry"] == dt.date(2026, 8, 7) and not p["expired"]
    print("rics.py self-test OK")
