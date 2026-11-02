"""
Option RIC helpers (OPRA, calls AND puts - the wheel needs both legs).

    {ROOT}{MONTH}{DAY}{YY}{STRIKE}.U{^MONTHYY if expired}

    MONTH  = letter. Calls A..L for Jan..Dec. Puts M..X for Jan..Dec (same order, offset by 12).
    DAY    = day of month, NOT zero-padded (some feeds want it padded for days 1-9; fetch_data3.py
             tries both spellings)
    YY     = two-digit year
    STRIKE = strike x 100, 5 digits, zero-padded
    .U     = OPRA
    expired options add ^{MONTH}{YY} - the CALL letter for that month, always, even on a put.
             The base symbol's own letter (A-L vs M-X) is what encodes put-vs-call; the ^ suffix
             only tags which monthly expiration cycle the contract belongs to, and LSEG reuses
             the call-letter scheme for that tag on both sides. A put's suffix is NOT its own
             M-X letter repeated - that was HW3's original bug (every real put failed to resolve
             until this was found and fixed; see git history / diag_* scripts in this folder for
             how it was tracked down).

Examples (from HW1.2/HW2, and corrected for HW3's puts):
    AAPLF52619000.U^F26    AAPL Jun  5 2026, 190 call (expired) - base F, suffix F (same, call)
    AAPLG172620000.U^G26   AAPL Jul 17 2026, 200 call (expired)
    AAPLR52618000.U^F26    AAPL Jun  5 2026, 180 put  (expired) - base R (June put), suffix F
                            (June CALL letter, not R)
"""
import datetime as dt
import re

CALL_LETTERS = "ABCDEFGHIJKL"
PUT_LETTERS = "MNOPQRSTUVWX"


def option_letter(month: int, side: str) -> str:
    letters = CALL_LETTERS if side == "C" else PUT_LETTERS
    return letters[month - 1]


def build_option_ric(root: str, expiry: dt.date, strike: float, side: str,
                      expired: bool = True, pad_day: bool = False) -> str:
    """side = 'C' or 'P'. pad_day=True writes the day as two digits ("07") for feeds that need it.
    The ^-suffix always uses the CALL letter for the expiry month, even when side='P' - see the
    module docstring. Confirmed against a classmate's corrected pull and LSEG's own community
    explanation of the expired-put suffix."""
    assert side in ("C", "P")
    code = option_letter(expiry.month, side)
    suffix_code = option_letter(expiry.month, "C")  # always the call letter, regardless of side
    yy = expiry.strftime("%y")
    day = f"{expiry.day:02d}" if pad_day else f"{expiry.day}"
    base = f"{root}{code}{day}{yy}{int(round(strike * 100)):05d}.U"
    return base + (f"^{suffix_code}{yy}" if expired else "")


def build_call_ric(root: str, expiry: dt.date, strike: float, expired: bool = True, pad_day: bool = False) -> str:
    return build_option_ric(root, expiry, strike, "C", expired, pad_day)


def build_put_ric(root: str, expiry: dt.date, strike: float, expired: bool = True, pad_day: bool = False) -> str:
    return build_option_ric(root, expiry, strike, "P", expired, pad_day)


_RIC_RE = re.compile(
    r"^(?P<root>[A-Z]+?)(?P<code>[A-X])(?P<day>\d{1,2})(?P<yy>\d{2})(?P<strike>\d{5})\.U(?:\^(?P<ecode>[A-X])(?P<eyy>\d{2}))?$"
)


def parse_option_ric(ric: str):
    """-> dict(root, expiry, strike, side, expired) or None if it does not parse."""
    m = _RIC_RE.match(ric)
    if not m:
        return None
    code = m.group("code")
    if code in CALL_LETTERS:
        side, month = "C", CALL_LETTERS.index(code) + 1
    else:
        side, month = "P", PUT_LETTERS.index(code) + 1
    try:
        expiry = dt.date(2000 + int(m.group("yy")), month, int(m.group("day")))
    except ValueError:
        return None
    return {
        "root": m.group("root"),
        "expiry": expiry,
        "strike": int(m.group("strike")) / 100.0,
        "side": side,
        "expired": m.group("ecode") is not None,
    }


def candidate_strikes(lo: float, hi: float):
    """Strikes worth trying between lo and hi: whole-dollar grid union the $2.50 grid
    (2.5, 7.5, 12.5 ... i.e. x.5 where x mod 5 == 2). A RIC that does not exist just
    returns nothing, so it is safe to over-ask; it is not safe to under-ask (we would
    miss the true nearest listed strike)."""
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
    assert build_call_ric("AAPL", dt.date(2026, 6, 5), 190) == "AAPLF52619000.U^F26"
    # Put: base letter is June's PUT letter (R), but the ^ suffix is June's CALL letter (F) - not R.
    assert build_put_ric("AAPL", dt.date(2026, 6, 5), 180) == "AAPLR52618000.U^F26"
    # The classmate-confirmed real example from the README: Aug 14 2026 $15 put.
    assert build_put_ric("UUUU", dt.date(2026, 8, 14), 15.0, pad_day=True) == "UUUUT142601500.U^H26"
    p = parse_option_ric("AAPLR52618000.U^F26")
    assert p["side"] == "P" and p["strike"] == 180.0 and p["expiry"] == dt.date(2026, 6, 5) and p["expired"]
    p = parse_option_ric("AAPLF52619000.U")
    assert p["side"] == "C" and p["strike"] == 190.0 and not p["expired"]
    print("rics.py self-test OK")
