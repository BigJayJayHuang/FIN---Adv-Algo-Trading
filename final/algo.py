#!/usr/bin/env python3
"""
Nightly job: inputs in, open orders out.

    python final/algo.py            # live: real data, writes open_orders.json at the repo root
    python final/algo.py --demo     # fake data from sim.py, writes into a temp folder only

Steps
  1. work out the next session (the `for` date) and the last completed one (`asof`)
  2. for each ticker in final/config.json: pull the option chain + daily closes,
     run signals.scan() and signals.decide()
  3. read mkt/positions.json (Sanji's book): exits, plus follow-ups (leg the
     short into a filled long, re-try a short leg that didn't fill)
  4. write entry orders for tickers the course server prices (they must be
     in price_history.json with bars), never stacking a second idea on a ticker.
     Each order names an exact contract from tonight's chain and, where it is
     safe, a limit price from pricing.py
  5. validate every order against the Sanji spec, then write
       open_orders.json                 the orders (repo root)
       final/data/latest.json           tonight's signals, decisions, orders (for the site)
       final/data/iv_history.json       ATM IV by night (IV rank / IV pop need this)
       final/data/ledger.json           what we sent (stand-in until positions.json exists)
       final/data/runs.json             one line per night
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import traceback
from datetime import date, datetime, timedelta, time
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import pandas as pd                                                    # noqa: E402

import book                                                            # noqa: E402
import data_sources as ds                                              # noqa: E402
from sanji import needs_chain_snapshot                                 # noqa: E402
from signals import Config, scan, decide, prepare_chain                # noqa: E402

ET = ZoneInfo("America/New_York")
NYSE_HOLIDAYS = {date.fromisoformat(d) for d in [
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25", "2026-06-19", "2026-07-03",
    "2026-09-07", "2026-11-26", "2026-12-25",
    "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31", "2027-06-18", "2027-07-05",
    "2027-09-06", "2027-11-25", "2027-12-24",
]}


def is_session(d: date) -> bool:
    return d.weekday() < 5 and d not in NYSE_HOLIDAYS


def next_session(now: datetime) -> date:
    """The session the orders are for. Before the 9:30 open on a trading day that is today."""
    d = now.date()
    if is_session(d) and now.time() < time(9, 30):
        return d
    d += timedelta(days=1)
    while not is_session(d):
        d += timedelta(days=1)
    return d


def prev_session(d: date) -> date:
    d -= timedelta(days=1)
    while not is_session(d):
        d -= timedelta(days=1)
    return d


def read_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def write_json(path, obj, indent=2):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=indent, default=str)
        f.write("\n")
    os.replace(tmp, path)


# ----------------------------------------------------------------------------
def demo_inputs(universe, asof: date):
    """Fake chains from sim.py with one planted scenario per ticker."""
    from sim import Market
    scen = {0: "informed_bull", 1: "hedge_noise", 2: "normal", 3: "informed_bear"}
    out = {}
    for i, u in enumerate(universe):
        t = scen.get(i, "normal")
        n = 40
        m = Market(seed=100 + i, n_days=n, s0=[400, 180, 500, 120][i % 4], day_types=["normal"] * (n - 1) + [t])
        shift = asof - m.dates[-1]
        chain = m.chain(n - 1)
        chain["expiry"] = [e + shift for e in chain["expiry"]]
        closes = m.closes(n - 1)
        closes.index = [d + shift for d in closes.index]
        ivh = []
        for j in range(n - 1):
            f = scan(m.chain(j), m.days[j].close, m.dates[j], m.closes(j), ivh)
            ivh.append({"date": (m.dates[j] + shift).isoformat(), "atm_iv": f.get("atm_iv")})
        out[u["symbol"]] = {"chain": chain, "spot": m.days[-1].close, "source": f"demo ({t})",
                            "closes": closes, "iv_history": ivh, "extra": {}}
    return out


def run(root: str, now: datetime, demo: bool = False, cfg: Config | None = None, pricing: str = "smart") -> dict:
    cfg = cfg or Config()
    data_dir = os.path.join(root, "final", "data")
    conf = read_json(os.path.join(HERE, "config.json"), {})
    universe = conf.get("universe", [])
    price_history = read_json(os.path.join(root, "price_history.json"), {})
    iv_hist_all = read_json(os.path.join(data_dir, "iv_history.json"), {})
    ledger = read_json(os.path.join(data_dir, "ledger.json"), [])

    for_day = next_session(now)
    asof = prev_session(for_day)
    warnings = []
    demo_data = demo_inputs(universe, asof) if demo else {}

    tickers, nights = [], {}
    for u in universe:
        sym, conid = u["symbol"], int(u["conid"])
        bars = (price_history.get(str(conid)) or {}).get("price_history") or {}
        tracked = bool((price_history.get(str(conid)) or {}).get("tracked"))
        row = {"symbol": sym, "conid": conid, "tracked": tracked, "server_bars": len(bars),
               "tradeable": tracked and len(bars) > 0, "error": None}
        try:
            if demo:
                d = demo_data[sym]
                closes, close_src, ivh = d["closes"], ["demo"], d["iv_history"]
                row["tradeable"] = True
            else:
                d = ds.get_chain(sym)
                closes, close_src = ds.get_closes(sym, u.get("lseg_ric"), price_history, conid)
                ivh = iv_hist_all.get(sym, [])
            spot = float(d["spot"])
            if len(closes) == 0 or closes.index[-1] < asof:
                closes = pd.concat([closes, pd.Series([spot], index=[asof])])
            earn = None if demo else ds.next_earnings(sym)
            feat = scan(d["chain"], spot, asof, closes, ivh, cfg, next_earnings=earn)
            dec = decide(feat, cfg)
            row.update(data_source=d["source"], close_sources=close_src, extra=d.get("extra", {}),
                       feat=feat, decision=dec)
            nights[sym] = {"decision": dec, "feat": feat, "spot": spot,
                           "pc": prepare_chain(d["chain"], spot, asof, cfg)}
            if feat.get("atm_iv") and not demo:
                h = [x for x in iv_hist_all.get(sym, []) if x["date"] != asof.isoformat()]
                h.append({"date": asof.isoformat(), "atm_iv": feat["atm_iv"],
                          "iv30": (d.get("extra") or {}).get("iv30")})
                iv_hist_all[sym] = sorted(h, key=lambda x: x["date"])[-400:]
        except Exception as exc:          # noqa: BLE001 - one bad ticker must not kill the night
            row["error"] = f"{type(exc).__name__}: {exc}"
            warnings.append(f"{sym}: {row['error']}")
            traceback.print_exc()
        tickers.append(row)
    if not demo:
        ds.lseg_close_session()

    by_conid = {int(u["conid"]): u["symbol"] for u in universe}
    raw_pos = read_json(os.path.join(root, "mkt", "positions.json"), None)
    positions = book.parse_positions(raw_pos, by_conid)
    have_book = raw_pos is not None
    fills = book.fill_status(read_json(os.path.join(root, "mkt", "fills.json"), []))
    pricer = book.Pricer(pricing)
    exits, exit_notes, follow_ledger = book.manage(positions, ledger, nights, for_day, pricer)

    # one idea per ticker: anything held (or still working) blocks a new entry
    open_after = {p["ticker"] for p in positions}
    if not have_book:
        open_after |= book.ledger_open_tickers(ledger, for_day, fills)

    entries_by, new_rows = {}, {}
    for row in tickers:
        dec = row.get("decision")
        if not dec or dec["mode"] == "IDLE":
            continue
        if not row["tradeable"]:
            row["skip"] = "not priced by the course server yet (no bars in price_history.json)"
            continue
        if row["symbol"] in open_after:
            row["skip"] = "already holding an idea on this ticker"
            continue
        n = nights[row["symbol"]]
        os_, lrows = book.plan_entry(row["symbol"], row["conid"], for_day, dec, n["feat"], n["pc"], n["spot"], pricer)
        if os_:
            entries_by[row["symbol"]] = os_
            new_rows[row["symbol"]] = lrows
        else:
            row["skip"] = "no listed contract near the target strike"
    entries_by = book.cap_striker(entries_by)
    entries = [o for v in entries_by.values() for o in v]
    entry_ledger = [r for t in entries_by for r in new_rows[t]]

    orders = exits + entries
    errs = book.check_all(orders)
    if errs:
        warnings.extend(errs)
        bad = {e.split(":")[0] for e in errs}
        orders = [o for o in orders if o["id"] not in bad]
    for oid in book.naked_risk(orders, positions):
        warnings.append(f"{oid}: short leg without protection - dropped")
        orders = [o for o in orders if o["id"] != oid]
    for o in orders:
        if needs_chain_snapshot(o):
            warnings.append(f"{o['id']} needs a chain snapshot Sanji may not have")
    for row in tickers:
        row["orders"] = [o["id"] for o in orders if o["underlying"] == row["conid"]]

    now_iso = now.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")
    latest = {"generated_at": now_iso, "for": for_day.isoformat(), "asof": asof.isoformat(), "demo": demo,
              "lseg_enabled": ds.lseg_available(), "positions_file": have_book,
              "positions": positions, "exit_notes": exit_notes, "orders": orders,
              "pricing_mode": pricing, "pricing": {k: v for k, v in pricer.info.items() if any(o["id"] == k for o in orders)},
              "tickers": tickers, "warnings": warnings, "config": cfg.to_dict()}

    write_json(os.path.join(root, "open_orders.json"), orders)
    write_json(os.path.join(data_dir, "latest.json"), latest)
    if not demo:
        write_json(os.path.join(data_dir, "iv_history.json"), iv_hist_all, indent=1)
        sent = {o["id"] for o in orders}
        for r in entry_ledger + follow_ledger:
            if r["id"] in sent and not any(e["id"] == r["id"] for e in ledger):
                ledger.append(r)
        write_json(os.path.join(data_dir, "ledger.json"), ledger[-500:], indent=1)
        runs = [r for r in read_json(os.path.join(data_dir, "runs.json"), []) if r.get("for") != for_day.isoformat()]
        runs.append({"generated_at": now_iso, "for": for_day.isoformat(), "n_orders": len(orders),
                     "tickers": {t["symbol"]: {"mode": (t.get("decision") or {}).get("mode", "ERROR"),
                                               "dir": (t.get("decision") or {}).get("direction"),
                                               "points": (t.get("decision") or {}).get("points"),
                                               "flow": (t.get("feat") or {}).get("flow_score"),
                                               "irregular": (t.get("feat") or {}).get("unusual_share"),
                                               "atm_iv": (t.get("feat") or {}).get("atm_iv")} for t in tickers}})
        write_json(os.path.join(data_dir, "runs.json"), runs[-400:], indent=1)
    return latest


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=os.path.dirname(HERE), help="repo root")
    ap.add_argument("--demo", action="store_true", help="fake data; writes to a temp folder unless --root is given")
    ap.add_argument("--now", help="pretend it is this time (ISO, ET)")
    a = ap.parse_args(argv)
    now = datetime.fromisoformat(a.now).replace(tzinfo=ET) if a.now else datetime.now(ET)
    root = a.root
    if a.demo and "--root" not in (argv or sys.argv):
        root = tempfile.mkdtemp(prefix="sanji-demo-")
        for f in ("price_history.json",):
            src = os.path.join(os.path.dirname(HERE), f)
            if os.path.exists(src):
                with open(src) as s, open(os.path.join(root, f), "w") as d:
                    d.write(s.read())
    latest = run(root, now, demo=a.demo)
    print(f"for {latest['for']} (asof {latest['asof']}) -> {len(latest['orders'])} orders in {root}/open_orders.json")
    for t in latest["tickers"]:
        dec = t.get("decision") or {}
        print(f"  {t['symbol']:5s} {dec.get('mode', 'ERROR'):8s} dir {dec.get('direction')} pts {dec.get('points')}"
              f"  {t.get('skip') or ''} {t.get('error') or ''}  {dec.get('reason', '')}")
    for o in latest["orders"]:
        print("  ", json.dumps(o))
    for w in latest["warnings"]:
        print("  WARNING", w)
    # a night with no data for any ticker is a failure the Action should show
    if all(t.get("error") for t in latest["tickers"]):
        sys.exit(1)


if __name__ == "__main__":
    main()
