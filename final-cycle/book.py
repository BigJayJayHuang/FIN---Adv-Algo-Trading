"""
Turning decisions into Sanji orders, and managing what we already hold.

Every order names an exact contract (numeric strike, calendar expiry) picked
from tonight's chain, and most carry a numeric limit from pricing.py. Sanji
can resolve all of that today: bare numbers need no chain snapshot.

Legging rule - a short option never goes out on its own:
  * Sleeper credit spread: the protective wing is a MARKET buy (always fills),
    the short leg is a patient LIMIT sell. Worst case the short doesn't fill
    and we hold a cheap wing, which the next night either re-tries the short
    or sells.
  * Striker: the long option goes out alone at a limit. If IV is rich, the
    short leg that turns it into a debit spread is sold the NEXT night, once
    positions.json shows the long is ours ("leg in"), at a patient limit.
  * Closing a spread patiently covers the short first; the long leg left
    behind is handled the next night. Urgent closes send every leg at market.
"""
from __future__ import annotations

import math
from datetime import date, timedelta

from pricing import PricingConfig, find_row, pick_contract, price_limit
from sanji import validate

# exits
EXIT_DTE = 1              # close anything that expires the next day or sooner
STRIKER_TAKE = 1.00       # +100% on a long option
STRIKER_STOP = 0.50       # -50%
SLEEPER_TAKE = 0.50       # keep 50% of the credit, then cover
MAX_STRIKER_CONTRACTS = 10
MIN_WING_VALUE = 0.10     # an orphan wing worth less than this is left to expire


def sigma_daily(feat: dict) -> float:
    for v in (feat.get("atm_iv"), feat.get("rv20")):
        if v:
            return float(v) / math.sqrt(252)
    return 0.03


def _pct(token: str) -> float:
    return float(str(token).split(":")[1])


def make_order(oid, for_day, action, qty, conid, contract, expiry, strike, limit=None):
    o = {"id": oid, "for": for_day.isoformat(), "order_type": "lmt" if limit is not None else "mkt",
         "action": action, "qty": int(qty), "underlying": int(conid), "contract": contract,
         "expiry": expiry.isoformat() if isinstance(expiry, date) else expiry,
         "strike": float(strike), "ref": "open"}
    if limit is not None:
        o["lmt_prc"] = float(limit)
    return o


class Pricer:
    """Applies the pricing mode: 'smart' (simulated-fill limits) or 'market' (everything at market)."""

    def __init__(self, mode="smart", pcfg: PricingConfig | None = None):
        self.mode, self.pcfg, self.info = mode, pcfg or PricingConfig(), {}

    def limit(self, oid, side, row, spot, for_day, feat, target, thesis_drift=0.0, chase=False):
        if self.mode != "smart" or row is None:
            return None
        p = price_limit(side, row, spot, for_day, sigma_daily(feat), feat.get("ret1"), target, self.pcfg,
                        seed=abs(hash(oid)) % (2 ** 31), thesis_drift=thesis_drift, chase=chase)
        self.info[oid] = p
        return p["limit"]


# ----------------------------------------------------------------------------
# entries
# ----------------------------------------------------------------------------
def plan_entry(ticker, conid, for_day, decision, feat, pc, spot, pricer: Pricer):
    """Orders + ledger rows for a new idea on one ticker."""
    mode, d, q = decision["mode"], decision["direction"], int(decision.get("qty") or 0)
    if mode not in ("STRIKER", "SLEEPER") or q < 1 or pc is None or len(pc) == 0:
        return [], []
    tag = f"{ticker.lower()}-{mode.lower()}"
    fd = for_day.isoformat()
    orders, rows = [], []

    def ledger(o, role, plan=None):
        rows.append({"id": o["id"], "ticker": ticker, "conid": int(conid), "for": fd, "mode": mode,
                     "structure": decision.get("structure"), "role": role, "contract": o["contract"],
                     "expiry": o["expiry"], "strike": o["strike"], "qty": o["qty"], "plan": plan or {}})

    if mode == "STRIKER":
        contract = "call" if d > 0 else "put"
        row = pick_contract(pc, for_day, contract, _pct(decision["strikes"]["long"]), spot)
        if row is None:
            return [], []
        oid = f"{tag}-long-{fd}"
        pc_ = pricer.pcfg
        dip_target = pc_.target_striker.get(min(decision.get("points", 3), 5), 0.6)
        style = pc_.striker_style if pricer.mode == "smart" else "market"
        core_q, dip_q = q, 0
        if style == "split" and q >= 2:
            core_q, dip_q = math.ceil(q / 2), q - math.ceil(q / 2)
        if style in ("market",):
            lim = None
        elif style == "protected":
            # price it as if we're right (the stock drifts our way), and pay up to the
            # 98th-percentile open: nearly a market order, but a runaway gap is capped
            drift = d * pc_.protected_drift * sigma_daily(feat)
            lim = pricer.limit(oid, "buy", row, spot, for_day, feat, pc_.target_protected, thesis_drift=drift, chase=True)
        else:
            core_target = dip_target if style == "dip" else pc_.target_marketable
            lim = pricer.limit(oid, "buy", row, spot, for_day, feat, core_target)
        o = make_order(oid, for_day, "BUY", core_q, conid, contract, row["expiry"], row["strike"], lim)
        orders.append(o)
        if dip_q:
            did = f"{tag}-dip-{fd}"
            dlim = pricer.limit(did, "buy", row, spot, for_day, feat, dip_target)
            do = make_order(did, for_day, "BUY", dip_q, conid, contract, row["expiry"], row["strike"], dlim)
            orders.append(do)
            ledger(do, "long")
        plan = {}
        if decision.get("structure") == "debit_spread" and "short" in decision.get("strikes", {}):
            plan = {"short_pct": _pct(decision["strikes"]["short"])}
            if pricer.mode != "smart":
                # market mode: both legs at once (the old behaviour, kept for the comparison)
                srow = pick_contract(pc, for_day, contract, plan["short_pct"], spot, expiry=row["expiry"])
                if srow and srow["strike"] != row["strike"]:
                    so = make_order(f"{tag}-short-{fd}", for_day, "SHT", q, conid, contract, srow["expiry"], srow["strike"])
                    orders.append(so)
                    ledger(so, "short")
                    plan = {}
        ledger(o, "long", plan)
        return orders, rows

    # SLEEPER: wing at market first, short leg at a patient limit
    contract = "put" if d > 0 else "call"
    srow = pick_contract(pc, for_day, contract, _pct(decision["strikes"]["short"]), spot)
    if srow is None:
        return [], []
    wrow = pick_contract(pc, for_day, contract, _pct(decision["strikes"]["wing"]), spot, expiry=srow["expiry"])
    further = (lambda k: k < srow["strike"]) if contract == "put" else (lambda k: k > srow["strike"])
    if wrow is None or not further(wrow["strike"]):
        return [], []
    wing = make_order(f"{tag}-wing-{fd}", for_day, "BUY", q, conid, contract, wrow["expiry"], wrow["strike"])
    sid = f"{tag}-short-{fd}"
    lim = (pricer.limit(sid, "sell", srow, spot, for_day, feat, pricer.pcfg.target_sleeper)
           if pricer.pcfg.sleeper_style == "limit" else None)
    short = make_order(sid, for_day, "SHT", q, conid, contract, srow["expiry"], srow["strike"], lim)
    orders += [wing, short]
    ledger(wing, "wing")
    ledger(short, "short")
    return orders, rows


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


def leg_key(x):
    return (int(x["conid"]), x["contract"], str(x["expiry"])[:10], round(float(x["strike"]), 4))


def role_map(ledger: list[dict]) -> dict:
    return {leg_key(r): r for r in ledger if r.get("strike") is not None}


def group_positions(positions: list[dict], roles: dict | None = None) -> list[dict]:
    roles = roles or {}
    groups = {}
    for p in positions:
        groups.setdefault((p["conid"], p["expiry"], p["contract"]), []).append(p)
    out = []
    for (conid, expiry, contract), legs in groups.items():
        shorts = [l for l in legs if l["qty"] < 0]
        longs = [l for l in legs if l["qty"] > 0]
        info = [roles.get(leg_key(l), {}) for l in legs]
        if shorts and longs:
            sk, lk = shorts[0]["strike"], longs[0]["strike"]
            credit = (sk > lk) if contract == "put" else (sk < lk)
            kind = "credit_spread" if credit else "debit_spread"
        elif shorts:
            kind = "naked_short"
        elif any(i.get("role") == "wing" for i in info):
            kind = "orphan_wing"
        else:
            kind = "long"
        lr = next((i for i, l in zip(info, legs) if l["qty"] > 0 and i), {})
        out.append({"ticker": legs[0]["ticker"], "conid": conid, "expiry": expiry, "contract": contract,
                    "kind": kind, "legs": legs, "ledger": lr})
    return out


def _value(legs, field):
    tot = 0.0
    for l in legs:
        v = l.get(field)
        if v is None:
            return None
        tot += l["qty"] * v
    return tot


def manage(positions, ledger, nights: dict, for_day: date, pricer: Pricer):
    """Exits and follow-ups for what we hold.
    nights[ticker] = {"decision", "feat", "pc", "spot"} for tonight."""
    orders, notes, new_ledger = [], [], []
    roles = role_map(ledger)
    fd = for_day.isoformat()
    for g in group_positions(positions, roles):
        tkr, legs = g["ticker"], g["legs"]
        n = nights.get(tkr, {})
        dec, feat, pc, spot = n.get("decision", {}), n.get("feat", {}), n.get("pc"), n.get("spot")
        for l in legs:   # fill missing marks from tonight's chain
            if l.get("mark") is None:
                r = find_row(pc, l["contract"], l["expiry"], l["strike"])
                l["mark"] = r["mid"] if r else None
        dte = (date.fromisoformat(g["expiry"]) - for_day).days
        bull = (g["contract"] == "call") if g["kind"] in ("long", "debit_spread") else (g["contract"] == "put")
        my_dir = 1 if bull else -1
        name = f"{tkr} {g['expiry']} {g['contract']} {g['kind'].replace('_', ' ')}"

        sleeper_group = g["kind"] in ("credit_spread", "orphan_wing")
        def close(legs_, why, urgent):
            if sleeper_group and pricer.pcfg.sleeper_style != "limit":
                urgent = True
            for l in legs_:
                side = "sell" if l["qty"] > 0 else "buy"
                oid = f"{tkr.lower()}-exit-{l['contract']}-{l['expiry']}-{l['strike']:g}-{fd}"
                lim = None
                if not urgent:
                    r = find_row(pc, l["contract"], l["expiry"], l["strike"])
                    lim = pricer.limit(oid, side, r, spot, for_day, feat, pricer.pcfg.target_patient_exit) if r and spot else None
                orders.append(make_order(oid, for_day, "SEL" if l["qty"] > 0 else "BTC", abs(int(l["qty"])),
                                         l["conid"], l["contract"], l["expiry"], l["strike"], lim))
            notes.append(f"Close {name}: {why}" + ("" if urgent else " (limit)"))

        shorts = [l for l in legs if l["qty"] < 0]
        # ---- orphan wing: re-try the short, sell it, or let it expire ----
        if g["kind"] == "orphan_wing":
            wing = legs[0]
            same_side = (dec.get("mode") == "SLEEPER" and
                         dec.get("structure") == ("bull_put_spread" if g["contract"] == "put" else "bear_call_spread"))
            if same_side and dte > EXIT_DTE + 1 and pc is not None and spot:
                row = pick_contract(pc, for_day, g["contract"], _pct(dec["strikes"]["short"]), spot,
                                    expiry=date.fromisoformat(g["expiry"]))
                between = row and ((g["contract"] == "put" and wing["strike"] < row["strike"] < spot) or
                                   (g["contract"] == "call" and spot < row["strike"] < wing["strike"]))
                if between:
                    oid = f"{tkr.lower()}-sleeper-reshort-{fd}"
                    lim = (pricer.limit(oid, "sell", row, spot, for_day, feat, pricer.pcfg.target_sleeper)
                           if pricer.pcfg.sleeper_style == "limit" else None)
                    o = make_order(oid, for_day, "SHT", wing["qty"], g["conid"], g["contract"], row["expiry"], row["strike"], lim)
                    orders.append(o)
                    new_ledger.append({"id": oid, "ticker": tkr, "conid": int(g["conid"]), "for": fd, "mode": "SLEEPER",
                                       "structure": dec.get("structure"), "role": "short", "contract": o["contract"],
                                       "expiry": o["expiry"], "strike": o["strike"], "qty": o["qty"], "plan": {}})
                    notes.append(f"{name}: short leg didn't fill, re-trying it against the wing we hold")
                    continue
            if (wing.get("mark") or 0) >= MIN_WING_VALUE and dte > EXIT_DTE:
                close(legs, "wing without its short leg", urgent=False)
            continue

        # ---- urgent exits: everything at market ----
        why = None
        if g["kind"] == "naked_short":
            why = "short leg without its protection"
        elif dte <= EXIT_DTE:
            why = f"{dte} day(s) to expiry"
        elif dec.get("mode") == "STRIKER" and dec.get("direction") == -my_dir:
            why = "a Striker signal fired against this position"
        elif g["kind"] == "credit_spread" and spot and shorts:
            k = shorts[0]["strike"]
            if (g["contract"] == "put" and spot <= k) or (g["contract"] == "call" and spot >= k):
                why = f"spot {spot:.2f} breached the short {k:g} strike"
        entry, now = _value(legs, "avg_price"), _value(legs, "mark")
        if why is None and g["kind"] in ("long", "debit_spread") and entry and now is not None and entry > 0:
            if now / entry - 1 <= -STRIKER_STOP:
                why = f"stop loss ({now / entry - 1:+.0%})"
        if why:
            close(legs, why, urgent=True)
            continue

        # ---- patient exits: limit orders, short leg first ----
        if g["kind"] in ("long", "debit_spread") and entry and now is not None and entry > 0 and now / entry - 1 >= STRIKER_TAKE:
            close(shorts or legs, f"take profit ({now / entry - 1:+.0%})", urgent=False)
            continue
        if g["kind"] == "credit_spread" and entry is not None and now is not None and -entry > 0 and -now <= (1 - SLEEPER_TAKE) * -entry:
            close(shorts, f"kept {1 - (-now) / (-entry):.0%} of the credit", urgent=False)
            continue

        # ---- leg in: sell the short leg against a long we now own ----
        plan = g["ledger"].get("plan") or {}
        if g["kind"] == "long" and plan.get("short_pct") is not None and pc is not None and spot and dte > EXIT_DTE + 1:
            lng = legs[0]
            row = pick_contract(pc, for_day, g["contract"], plan["short_pct"], spot, expiry=date.fromisoformat(g["expiry"]))
            ok = row and ((g["contract"] == "call" and row["strike"] > lng["strike"]) or
                          (g["contract"] == "put" and row["strike"] < lng["strike"]))
            if ok:
                oid = f"{tkr.lower()}-striker-legin-{fd}"
                lim = pricer.limit(oid, "sell", row, spot, for_day, feat, pricer.pcfg.target_leg_in)
                o = make_order(oid, for_day, "SHT", lng["qty"], g["conid"], g["contract"], row["expiry"], row["strike"], lim)
                orders.append(o)
                new_ledger.append({"id": oid, "ticker": tkr, "conid": int(g["conid"]), "for": fd, "mode": "STRIKER",
                                   "structure": "debit_spread", "role": "short", "contract": o["contract"],
                                   "expiry": o["expiry"], "strike": o["strike"], "qty": o["qty"], "plan": {}})
                notes.append(f"{name}: long is filled, selling the {row['strike']:g} against it to make the debit spread")
    return orders, notes, new_ledger


# ----------------------------------------------------------------------------
# ledger: what we sent. Used to tag positions, and to avoid stacking ideas
# while Sanji isn't publishing positions yet.
# ----------------------------------------------------------------------------
def fill_status(raw_fills) -> dict:
    """id -> 'filled' / 'unfilled' / 'unresolved' / 'rejected' from mkt/fills.json (shape not published yet)."""
    out = {}
    items = raw_fills.get("fills", []) if isinstance(raw_fills, dict) else (raw_fills or [])
    for f in items if isinstance(items, list) else []:
        if isinstance(f, dict) and f.get("id"):
            out[f["id"]] = str(f.get("status") or f.get("outcome") or f.get("result") or "filled").lower()
    return out


def ledger_open_tickers(ledger: list[dict], for_day: date, fills: dict | None = None) -> set[str]:
    fills = fills or {}
    return {e["ticker"] for e in ledger
            if str(e.get("expiry", e.get("est_expiry", ""))) >= for_day.isoformat()
            and e.get("for", "") < for_day.isoformat()
            and fills.get(e["id"], "filled") == "filled"}


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


def naked_risk(orders: list[dict], positions: list[dict]) -> list[str]:
    """Any short order whose protection isn't either a market order tonight or already held."""
    bad = []
    held_long = {(p["conid"], p["contract"], p["expiry"]) for p in positions if p["qty"] > 0}
    for o in orders:
        if o["action"] != "SHT":
            continue
        k = (o["underlying"], o["contract"], o["expiry"])
        partner_mkt = any(x["action"] == "BUY" and x["order_type"] == "mkt" and
                          (x["underlying"], x["contract"], x["expiry"]) == k for x in orders)
        if not (partner_mkt or k in held_long):
            bad.append(o["id"])
    return bad
