"""
Backtest the nightly algo on the synthetic market in sim.py.

Every night it calls the exact functions the live job calls
(signals.scan -> signals.decide -> book.exit_orders / book.entry_orders),
then fills those orders the next morning the way Sanji says he will:
tokens resolve against the listed strikes/expiries at the open, market orders
fill at the contract's open (we pay half the spread), all or nothing.

    python final/backtest.py              # 20 seeds, writes final/data/backtest.json
    python final/backtest.py --seeds 3    # quick run
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from collections import Counter, defaultdict
from datetime import date

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from book import entry_orders, exit_orders, group_positions, check_all   # noqa: E402
from sanji import resolve_expiry, resolve_strike                          # noqa: E402
from signals import Config, scan, decide                                  # noqa: E402
from sim import Market                                                    # noqa: E402

TICKER, CONID = "SIM", 1
FEE = 0.65          # per contract
WARMUP = 25


def run(seed=0, cfg: Config | None = None, allow_striker=True, allow_sleeper=True,
        n_days=260, start_cash=100_000.0, keep_nights=False, cache=None):
    cfg = cfg or Config()
    m = Market(seed=seed, n_days=n_days)
    cache = {} if cache is None else cache

    def nightly(i):
        if i not in cache:
            d = m.days[i]
            cache[i] = scan(m.chain(i), d.close, d.date, m.closes(i), iv_hist, cfg)
        return cache[i]
    cash = start_cash
    pos: list[dict] = []            # legs: conid, ticker, contract, expiry, strike, qty, avg_price, mark, group
    groups: dict[str, dict] = {}    # trade id -> record
    pending: list[dict] = []
    iv_hist, nav, nights, problems = [], [], [], []
    detect = Counter()

    def leg_key(l):
        return (l["contract"], l["expiry"], l["strike"])

    for i, day in enumerate(m.days):
        # ---------------- morning: fill last night's orders at the open ----------------
        if pending:
            listed = m.expiries(i - 1)
            # listed strikes = tonight's grid plus anything already listed that we hold
            strikes = sorted(set(m.strikes(m.days[i - 1].close)) | {l["strike"] for l in pos})
            for o in pending:
                e = resolve_expiry(o["expiry"], day.date, listed)
                k = resolve_strike(o["strike"] if isinstance(o["strike"], str) else float(o["strike"]),
                                   o["contract"], day.open, strikes)
                if e is None or k is None:
                    problems.append(f"{o['id']}: unresolved")
                    continue
                is_call = o["contract"] == "call"
                px = m.option_value(i, day.open, k, e, is_call, at_open=True)
                half = max(0.025, 0.015 * px)
                pay = o["action"] in ("BUY", "BTC")
                fill = max(0.01, px + half if pay else px - half)
                q = o["qty"]
                flow = (-1 if pay else 1) * fill * q * 100 - FEE * q
                cash += flow
                gid = o.get("_group")
                key = (o["contract"], e.isoformat(), float(k))
                leg = next((l for l in pos if leg_key(l) == key and l["qty"] != 0
                            and (o["action"] in ("SEL", "BTC") or l["group"] == gid)), None)
                if o["action"] in ("BUY", "SHT"):
                    signed = q if o["action"] == "BUY" else -q
                    if leg is None:
                        pos.append({"conid": CONID, "ticker": TICKER, "contract": o["contract"],
                                    "expiry": e.isoformat(), "strike": float(k), "qty": signed,
                                    "avg_price": fill, "mark": px, "group": gid})
                    else:
                        leg["avg_price"] = (leg["avg_price"] * abs(leg["qty"]) + fill * q) / (abs(leg["qty"]) + q)
                        leg["qty"] += signed
                    groups[gid]["cash"] += flow
                else:
                    if leg is None:
                        problems.append(f"{o['id']}: closing a leg we don't hold")
                        continue
                    leg["qty"] += q if o["action"] == "BTC" else -q
                    groups[leg["group"]]["cash"] += flow
                    if leg["qty"] == 0:
                        groups[leg["group"]]["exit_date"] = day.date.isoformat()
                        groups[leg["group"]].setdefault("exit_reason", o.get("_why", "exit"))
            pending = []
            pos = [l for l in pos if l["qty"] != 0]

        # ---------------- close: settle expiries, mark the book ----------------
        for l in pos:
            if l["expiry"] <= day.date.isoformat():
                intrinsic = max(0.0, (day.close - l["strike"]) if l["contract"] == "call" else (l["strike"] - day.close))
                flow = l["qty"] * intrinsic * 100
                cash += flow
                groups[l["group"]]["cash"] += flow
                groups[l["group"]]["exit_date"] = day.date.isoformat()
                groups[l["group"]].setdefault("exit_reason", "expired")
                l["qty"] = 0
        pos = [l for l in pos if l["qty"] != 0]
        for l in pos:
            l["mark"] = m.option_value(i, day.close, l["strike"], date.fromisoformat(l["expiry"]), l["contract"] == "call")
        nav.append(cash + sum(l["qty"] * l["mark"] * 100 for l in pos))

        if i < WARMUP or i >= len(m.days) - 1:
            if i < WARMUP:
                f = nightly(i)
                iv_hist.append({"date": day.date.isoformat(), "atm_iv": f.get("atm_iv")})
            continue

        # ---------------- evening: the nightly job ----------------
        feat = nightly(i)
        iv_hist.append({"date": day.date.isoformat(), "atm_iv": feat.get("atm_iv")})
        dec = decide(feat, cfg)
        detect[(day.day_type, dec["mode"], dec["direction"])] += 1
        if keep_nights:
            nights.append({"date": day.date.isoformat(), "day_type": day.day_type, "worked": day.worked,
                           "feat": {k: v for k, v in feat.items() if k != "top_unusual"},
                           "top_unusual": feat.get("top_unusual", [])[:5], "decision": dec})

        if (dec["mode"] == "STRIKER" and not allow_striker) or (dec["mode"] == "SLEEPER" and not allow_sleeper):
            dec = dict(dec, mode="IDLE")
        for_day = m.days[i + 1].date

        exits, notes = exit_orders(pos, {TICKER: dec}, {TICKER: day.close}, for_day)
        why = "; ".join(n.split(": ", 1)[-1] for n in notes)
        for o in exits:
            o["_why"] = why
        closing = {(o["contract"], o["expiry"], float(o["strike"])) for o in exits}
        still_open = [l for l in pos if leg_key(l) not in closing]
        entries = []
        if dec["mode"] != "IDLE" and not still_open:
            entries = entry_orders(TICKER, CONID, for_day, dec)
            gid = f"{dec['mode'].lower()}-{for_day.isoformat()}"
            for o in entries:
                o["_group"] = gid
            if entries:
                groups[gid] = {"id": gid, "mode": dec["mode"], "structure": dec["structure"],
                               "direction": dec["direction"], "qty": dec["qty"], "signal_date": day.date.isoformat(),
                               "entry_date": for_day.isoformat(), "day_type": day.day_type,
                               "worked": day.worked, "cash": 0.0}
        errs = check_all([{k: v for k, v in o.items() if not k.startswith("_")} for o in exits + entries])
        problems.extend(errs)
        pending = exits + entries

    # anything still open is valued at the last mark (already in nav)
    for l in pos:
        g = groups[l["group"]]
        g["cash"] += l["qty"] * l["mark"] * 100
        g.setdefault("exit_reason", "open at end (marked)")

    nav = np.array(nav)
    rets = np.diff(nav) / nav[:-1]
    peak = np.maximum.accumulate(nav)
    trades = list(groups.values())
    for t in trades:
        t["pnl"] = round(t.pop("cash"), 2)
    return {
        "seed": seed,
        "dates": [d.date.isoformat() for d in m.days],
        "close": [round(d.close, 2) for d in m.days],
        "day_type": [d.day_type for d in m.days],
        "nav": [round(x, 2) for x in nav],
        "total_return": float(nav[-1] / start_cash - 1),
        "max_drawdown": float(((nav - peak) / peak).min()),
        "sharpe": float(rets.mean() / rets.std() * math.sqrt(252)) if rets.std() > 0 else 0.0,
        "trades": trades,
        "detect": detect,
        "problems": problems,
        "nights": nights,
    }


def mode_stats(trades):
    out = {}
    for mode in ("STRIKER", "SLEEPER"):
        t = [x for x in trades if x["mode"] == mode]
        p = [x["pnl"] for x in t]
        out[mode] = {"n": len(t), "win_rate": float(np.mean([x > 0 for x in p])) if p else None,
                     "avg_pnl": float(np.mean(p)) if p else None, "total_pnl": float(np.sum(p)) if p else 0.0}
    return out


def detection_table(detect: Counter):
    types = ["normal", "informed_bull", "informed_bear", "hedge_noise", "iv_only"]
    rows = []
    for t in types:
        n = sum(v for (dt, _, _), v in detect.items() if dt == t)
        c = lambda mode, d=None: sum(v for (dt, mo, di), v in detect.items()
                                     if dt == t and mo == mode and (d is None or di == d))
        rows.append({"day_type": t, "nights": n,
                     "striker_up": c("STRIKER", 1), "striker_down": c("STRIKER", -1),
                     "sleeper": c("SLEEPER"), "idle": c("IDLE")})
    inf = [r for r in rows if r["day_type"].startswith("informed")]
    caught = sum(r["striker_up"] if r["day_type"] == "informed_bull" else r["striker_down"] for r in inf)
    wrong_way = sum(r["striker_down"] if r["day_type"] == "informed_bull" else r["striker_up"] for r in inf)
    n_inf = sum(r["nights"] for r in inf)
    others = [r for r in rows if not r["day_type"].startswith("informed")]
    false_alarm = sum(r["striker_up"] + r["striker_down"] for r in others)
    n_oth = sum(r["nights"] for r in others)
    strikes = caught + wrong_way + false_alarm
    return {"rows": rows,
            "catch_rate": caught / n_inf if n_inf else None,
            "wrong_way": wrong_way,
            "false_alarm_rate": false_alarm / n_oth if n_oth else None,
            "precision": caught / strikes if strikes else None}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, default=20)
    ap.add_argument("--out", default=os.path.join(HERE, "data", "backtest.json"))
    a = ap.parse_args()

    variants = {"full": dict(), "sleeper_only": dict(allow_striker=False), "striker_only": dict(allow_sleeper=False)}
    agg = {v: [] for v in variants}
    det = Counter()
    all_trades = defaultdict(list)
    sample = {}
    problems = []
    for s in range(a.seeds):
        cache = {}
        for v, kw in variants.items():
            r = run(seed=s, keep_nights=(s == 0 and v == "full"), cache=cache, **kw)
            agg[v].append(r)
            all_trades[v].extend(r["trades"])
            problems.extend(r["problems"])
            if v == "full":
                det.update(r["detect"])
            if s == 0:
                sample[v] = r
        f = agg["full"][-1]
        print(f"seed {s:2d}: full {f['total_return']:+.1%}  sleeper-only {agg['sleeper_only'][-1]['total_return']:+.1%}"
              f"  striker-only {agg['striker_only'][-1]['total_return']:+.1%}", flush=True)

    summary = {}
    for v, rs in agg.items():
        tr = [r["total_return"] for r in rs]
        summary[v] = {"mean_return": float(np.mean(tr)), "median_return": float(np.median(tr)),
                      "pct_seeds_positive": float(np.mean([x > 0 for x in tr])),
                      "mean_max_dd": float(np.mean([r["max_drawdown"] for r in rs])),
                      "mean_sharpe": float(np.mean([r["sharpe"] for r in rs])),
                      "modes": mode_stats(all_trades[v])}

    # Striker trades split by whether the planted move actually followed through
    st = [t for t in all_trades["full"] if t["mode"] == "STRIKER"]
    split = {}
    for label, sel in [("informed_worked", lambda t: t["worked"] is True),
                       ("informed_fizzled", lambda t: t["worked"] is False),
                       ("not_informed", lambda t: t["worked"] is None)]:
        p = [t["pnl"] for t in st if sel(t)]
        split[label] = {"n": len(p), "avg_pnl": float(np.mean(p)) if p else None,
                        "win_rate": float(np.mean([x > 0 for x in p])) if p else None}

    d = detection_table(det)
    s0 = sample["full"]
    examples = {}
    for n in s0["nights"]:
        examples.setdefault(n["day_type"], n)
    out = {
        "seeds": a.seeds, "days_per_seed": len(s0["dates"]), "warmup": WARMUP,
        "config": Config().to_dict(),
        "summary": summary, "detection": d, "striker_by_outcome": split,
        "order_problems": problems[:50], "n_order_problems": len(problems),
        "seed0": {"dates": s0["dates"], "close": s0["close"], "day_type": s0["day_type"],
                  "nav": {v: sample[v]["nav"] for v in variants},
                  "trades": s0["trades"]},
        "example_nights": examples,
    }
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    with open(a.out, "w") as fh:
        json.dump(out, fh, indent=1, default=str)
    print(json.dumps({"summary": summary, "detection": {k: v for k, v in d.items() if k != "rows"},
                      "striker_by_outcome": split, "n_order_problems": len(problems)}, indent=1))
    for r in d["rows"]:
        print(r)


if __name__ == "__main__":
    main()
