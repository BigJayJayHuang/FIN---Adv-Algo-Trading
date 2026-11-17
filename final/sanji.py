"""
A local copy of the Sanji order rules, so we can check our own orders before
we write them and so the backtest fills orders the way Sanji will.

Spec: https://jakevestal.github.io/server-logic-533/order.html
This is our reading of that page, not Sanji's code. If the two disagree, the
page wins.
"""
from __future__ import annotations

import re
from datetime import date, timedelta

REQUIRED = {"id", "for", "order_type", "action", "qty", "underlying", "contract"}
ALLOWED = REQUIRED | {"expiry", "strike", "ref", "lmt_prc"}
ACTIONS = {"BUY", "SEL", "SHT", "BTC"}

RE_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
RE_EXPIRY = re.compile(r"^(\d{4}-\d{2}-\d{2}|W\+|M\+|\d+DTE[+-]?)$")
RE_STRIKE = re.compile(r"^(atm|(otm|itm):\d+|pct:-?\d+(\.\d+)?|delta:\d+(\.\d+)?|premium:\d+(\.\d+)?)$")
RE_LMT = re.compile(r"^(mid|bid|ask)([+-]\d+(\.\d+)?)?$")
NEEDS_CHAIN = re.compile(r"^(delta|premium):")


def validate(o: dict) -> list[str]:
    """Return the reasons Sanji would reject this order (empty list = legal)."""
    errs = []
    if not isinstance(o, dict):
        return ["not an object"]
    missing = REQUIRED - o.keys()
    if missing:
        errs.append(f"missing {sorted(missing)}")
    extra = o.keys() - ALLOWED
    if extra:
        errs.append(f"unknown field {sorted(extra)}")
    if not isinstance(o.get("id"), str) or not o.get("id"):
        errs.append("id must be a non-empty string")
    if not (isinstance(o.get("for"), str) and RE_DATE.match(o["for"])):
        errs.append("for must be YYYY-MM-DD")
    if o.get("order_type") not in ("mkt", "lmt"):
        errs.append("order_type must be mkt or lmt")
    if o.get("action") not in ACTIONS:
        errs.append("bad action")
    q = o.get("qty")
    if not (isinstance(q, int) and not isinstance(q, bool) and q >= 1):
        errs.append("qty must be an integer >= 1")
    if not (isinstance(o.get("underlying"), int) and not isinstance(o.get("underlying"), bool)):
        errs.append("underlying must be an integer conid")
    c = o.get("contract")
    if c not in ("put", "call", "underlying"):
        errs.append("contract must be put, call or underlying")
    if c in ("put", "call"):
        e, k = o.get("expiry"), o.get("strike")
        if not (isinstance(e, str) and RE_EXPIRY.match(e)):
            errs.append(f"bad expiry {e!r}")
        if isinstance(k, bool) or not (isinstance(k, (int, float)) or (isinstance(k, str) and RE_STRIKE.match(k))):
            errs.append(f"bad strike {k!r}")
    elif c == "underlying" and ("expiry" in o or "strike" in o):
        errs.append("expiry/strike not allowed on underlying")
    if "ref" in o and o["ref"] not in ("open", "prev_close"):
        errs.append("ref must be open or prev_close")
    if o.get("order_type") == "lmt":
        lp = o.get("lmt_prc")
        if lp is None:
            errs.append("lmt needs lmt_prc")
        elif isinstance(lp, bool) or not (isinstance(lp, (int, float)) or (isinstance(lp, str) and RE_LMT.match(lp))):
            errs.append(f"bad lmt_prc {lp!r}")
    elif "lmt_prc" in o:
        errs.append("lmt_prc forbidden on mkt")
    return errs


def needs_chain_snapshot(o: dict) -> bool:
    """delta:/premium: strikes and mid/bid/ask limits need a chain Sanji may not have yet."""
    k, lp = o.get("strike"), o.get("lmt_prc")
    return (isinstance(k, str) and bool(NEEDS_CHAIN.match(k))) or (isinstance(lp, str) and bool(RE_LMT.match(lp)))


# ----------------------------------------------------------------------------
# resolving tokens to a real contract (used by the backtest)
# ----------------------------------------------------------------------------
def _third_friday(y, m):
    d = date(y, m, 15)
    return d + timedelta(days=(4 - d.weekday()) % 7)


def resolve_expiry(token: str, for_day: date, listed: list[date]) -> date | None:
    listed = sorted(e for e in listed if e >= for_day)
    if not listed:
        return None
    if RE_DATE.match(token):
        d = date.fromisoformat(token)
        return d if d in listed else None
    if token == "W+":
        nxt = [e for e in listed if e > for_day]
        return nxt[0] if nxt else None
    if token == "M+":
        monthly = [e for e in listed if e > for_day and e == _third_friday(e.year, e.month)]
        return monthly[0] if monthly else None
    m = re.match(r"^(\d+)DTE([+-]?)$", token)
    if m:
        n, mode = int(m.group(1)), m.group(2)
        days = {e: (e - for_day).days for e in listed}
        if mode == "+":
            ok = [e for e in listed if days[e] >= n]
            return ok[0] if ok else None
        if mode == "-":
            ok = [e for e in listed if 0 <= days[e] <= n]
            return ok[-1] if ok else None
        target = for_day + timedelta(days=n)
        return target if target in listed else None
    return None


def resolve_strike(token, contract: str, ref: float, strikes: list[float]) -> float | None:
    ks = sorted(strikes)
    if not ks:
        return None
    if isinstance(token, (int, float)):
        return float(token) if float(token) in ks else None
    # ATM: closest to ref, ties to the lower strike
    atm_i = min(range(len(ks)), key=lambda i: (abs(ks[i] - ref), ks[i]))
    if token == "atm":
        return ks[atm_i]
    m = re.match(r"^(otm|itm):(\d+)$", token)
    if m:
        n = int(m.group(2))
        up = (contract == "call") == (m.group(1) == "otm")   # calls: otm is up; puts: otm is down
        i = atm_i + n if up else atm_i - n
        return ks[i] if 0 <= i < len(ks) else None
    m = re.match(r"^pct:(-?\d+(\.\d+)?)$", token)
    if m:
        tgt = ref * (1 + float(m.group(1)) / 100)
        further_otm = (lambda k: k) if contract == "call" else (lambda k: -k)
        return min(ks, key=lambda k: (abs(k - tgt), -further_otm(k)))
    return None   # delta / premium need a chain snapshot
