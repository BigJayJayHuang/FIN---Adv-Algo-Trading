"""
Turning decisions into Sanji orders, and managing what we already hold.

Entries use relative tokens (otm:n, 5DTE+) because at 10pm we cannot see
tomorrow's open. Exits use the exact expiry date and strike, which we only
know once Sanji reports the fill in mkt/positions.json.

Every order is a market order on purpose:
  * fills are all-or-nothing per order, so a limit on one leg of a spread
    could leave us with a naked short leg;
  * `mid`/`bid`/`ask` limits and `delta:` strikes need an option-chain
    snapshot that Sanji says he does not have yet. otm:n and 5DTE+ resolve
    from the strike and expiry lists alone.
"""
from __future__ import annotations

from datetime import date, timedelta

from sanji import validate

EXPIRY_TOKEN = "5DTE+"

# (action, contract, leg name). Strike tokens come from the decision (pct: tokens
# sized off the expected move), so the same rule works for a $20 or a $400 stock.
STRUCTURES = {
    ("STRIKER", "long_option", 1): [("BUY", "call", "long")],
    ("STRIKER", "long_option", -1): [("BUY", "put", "long")],
    ("STRIKER", "debit_spread", 1): [("BUY", "call", "long"), ("SHT", "call", "short")],
    ("STRIKER", "debit_spread", -1): [("BUY", "put", "long"), ("SHT", "put", "short")],
    ("SLEEPER", "bull_put_spread", 1): [("SHT", "put", "short"), ("BUY", "put", "wing")],
    ("SLEEPER", "bear_call_spread", -1): [("SHT", "call", "short"), ("BUY", "call", "wing")],
}

# exits
EXIT_DTE = 1              # close anything that expires the next day or sooner
STRIKER_TAKE = 1.00       # +100% on a long option
STRIKER_STOP = 0.50       # -50%
SLEEPER_TAKE = 0.50       # keep 50% of the credit, then close
MAX_STRIKER_CONTRACTS = 10


def entry_orders(ticker: str, conid: int, for_day: date, decision: dict) -> list[dict]:
    key = (decision["mode"], decision.get("structure"), decision["direction"])
    legs = STRUCTURES.get(key)
    if not legs or decision["qty"] < 1:
        return []
    out = []
    for action, contract, leg in legs:
        strike = decision["strikes"][leg]
        out.append({
            "id": f"{ticker.lower()}-{decision['mode'].lower()}-{leg}-{for_day.isoformat()}",
            "for": for_day.isoformat(),
            "order_type": "mkt",
            "action": action,
            "qty": int(decision["qty"]),
            "underlying": int(conid),
            "contract": contract,
            "expiry": EXPIRY_TOKEN,
            "strike": strike,
            "ref": "open",
        })
    return out


# ----------------------------------------------------------------------------
# positions
# ----------------------------------------------------------------------------
def _first(d, *keys):
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return None


def parse_positions(raw, conid_to_ticker: dict[int, str]) -> list[dict]:
    """Read mkt/positions.json. Its exact shape is not published yet, so accept
    the obvious variants and skip anything we can't read."""
    if raw is None:
        return []
    items = raw
    if isinstance(raw, dict):
        items = raw.get("positions") or raw.get("book") or list(raw.values())
    out = []
    for p in items if isinstance(items, list) else []:
        if not isinstance(p, dict):
            continue
        try:
            conid = int(_first(p, "underlying", "conid", "underlying_conid"))
        except (TypeError, ValueError):
            continue
        contract = str(_first(p, "contract", "right", "type") or "").lower()
        contract = {"c": "call", "p": "put", "stk": "underlying", "stock": "underlying"}.get(contract, contract)
        if contract not in ("call", "put"):
            continue
        qty = _first(p, "qty", "position", "quantity")
        try:
            qty = float(qty)
        except (TypeError, ValueError):
            continue
        side = str(_first(p, "side") or "").lower()
        if side in ("short", "sht", "s") and qty > 0:
            qty = -qty
        if qty == 0:
            continue
        def num(*k):
            v = _first(p, *k)
            try:
                return float(v)
            except (TypeError, ValueError):
                return None
        expiry = str(_first(p, "expiry", "expiration", "exp") or "")[:10]
        strike = num("strike")
        if not expiry or strike is None:
            continue
        out.append({
            "ticker": conid_to_ticker.get(conid, str(conid)), "conid": conid, "contract": contract,
            "expiry": expiry, "strike": strike, "qty": int(qty),
            "avg_price": num("avg_price", "avg_cost", "cost", "price", "fill_price"),
            "mark": num("mark", "close", "last", "mark_price"),
        })
    return out


def group_positions(positions: list[dict]) -> list[dict]:
    """One group per (ticker, expiry, put/call). Any short leg -> Sleeper style spread."""
    groups = {}
    for p in positions:
        groups.setdefault((p["conid"], p["expiry"], p["contract"]), []).append(p)
    out = []
    for (conid, expiry, contract), legs in groups.items():
        has_short = any(l["qty"] < 0 for l in legs)
        has_long = any(l["qty"] > 0 for l in legs)
        if has_short and has_long:
            short_k = [l["strike"] for l in legs if l["qty"] < 0][0]
            long_k = [l["strike"] for l in legs if l["qty"] > 0][0]
            # credit spread if the short leg is the one nearer the money
            credit = (short_k > long_k) if contract == "put" else (short_k < long_k)
            kind = "credit_spread" if credit else "debit_spread"
        else:
            kind = "short" if has_short else "long"
        out.append({"ticker": legs[0]["ticker"], "conid": conid, "expiry": expiry,
                    "contract": contract, "kind": kind, "legs": legs})
    return out


def _value(legs, field):
    """Signed $/share value of the group: long legs +, short legs -."""
    tot = 0.0
    for l in legs:
        v = l.get(field)
        if v is None:
            return None
        tot += l["qty"] * v
    return tot


def exit_orders(positions: list[dict], decisions: dict[str, dict], spots: dict[str, float],
                for_day: date) -> tuple[list[dict], list[str]]:
    """Closing orders for tomorrow plus a one-line note per decision."""
    orders, notes = [], []
    for g in group_positions(positions):
        tkr, legs = g["ticker"], g["legs"]
        dec = decisions.get(tkr, {})
        dte = (date.fromisoformat(g["expiry"]) - for_day).days
        bull = (g["contract"] == "call") if g["kind"] in ("long", "debit_spread") else (g["contract"] == "put")
        my_dir = 1 if bull else -1
        why = None

        if dte <= EXIT_DTE:
            why = f"{dte} day(s) to expiry"
        elif dec.get("mode") == "STRIKER" and dec.get("direction") == -my_dir:
            why = "a Striker signal fired against this position"
        else:
            entry, now = _value(legs, "avg_price"), _value(legs, "mark")
            if g["kind"] in ("long", "debit_spread") and entry and now is not None and entry > 0:
                r = now / entry - 1
                if r >= STRIKER_TAKE:
                    why = f"take profit ({r:+.0%})"
                elif r <= -STRIKER_STOP:
                    why = f"stop loss ({r:+.0%})"
            elif g["kind"] in ("credit_spread", "short"):
                credit = -entry if entry is not None else None
                cost = -now if now is not None else None
                if credit and credit > 0 and cost is not None and cost <= (1 - SLEEPER_TAKE) * credit:
                    why = f"kept {1 - cost / credit:.0%} of the credit"
                spot = spots.get(tkr)
                short_k = [l["strike"] for l in legs if l["qty"] < 0]
                if why is None and spot and short_k:
                    k = short_k[0]
                    if (g["contract"] == "put" and spot <= k) or (g["contract"] == "call" and spot >= k):
                        why = f"spot {spot:.2f} breached the short {k:g} strike"
        if why is None:
            continue
        notes.append(f"Close {tkr} {g['expiry']} {g['contract']} {g['kind']}: {why}")
        for l in legs:
            orders.append({
                "id": f"{tkr.lower()}-exit-{l['contract']}-{l['expiry']}-{l['strike']:g}-{for_day.isoformat()}",
                "for": for_day.isoformat(), "order_type": "mkt",
                "action": "SEL" if l["qty"] > 0 else "BTC", "qty": abs(int(l["qty"])),
                "underlying": int(l["conid"]), "contract": l["contract"],
                "expiry": l["expiry"], "strike": l["strike"], "ref": "open",
            })
    return orders, notes


# ----------------------------------------------------------------------------
# ledger: what we sent, used only while Sanji isn't publishing positions yet
# ----------------------------------------------------------------------------
def est_expiry(for_day: date) -> date:
    """Roughly where 5DTE+ lands: first Friday at least 5 days out."""
    d = for_day + timedelta(days=5)
    return d + timedelta(days=(4 - d.weekday()) % 7)


def ledger_open_tickers(ledger: list[dict], for_day: date) -> set[str]:
    return {e["ticker"] for e in ledger if e.get("est_expiry", "") >= for_day.isoformat()
            and e.get("for", "") < for_day.isoformat()}


def cap_striker(orders_by_ticker: dict[str, list[dict]], cap=MAX_STRIKER_CONTRACTS):
    """Keep total Striker contracts under the cap, biggest signals first."""
    used, kept = 0, {}
    for tkr, os_ in sorted(orders_by_ticker.items(), key=lambda kv: -max((o["qty"] for o in kv[1]), default=0)):
        q = max((o["qty"] for o in os_ if "-striker-" in o["id"]), default=0)
        if q and used + q > cap:
            continue
        used += q
        kept[tkr] = os_
    return kept


def check_all(orders: list[dict]) -> list[str]:
    errs, seen = [], set()
    for o in orders:
        for e in validate(o):
            errs.append(f"{o.get('id')}: {e}")
        if o.get("id") in seen:
            errs.append(f"duplicate id {o.get('id')}")
        seen.add(o.get("id"))
    return errs
