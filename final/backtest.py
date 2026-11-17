"""
Backtest the nightly algo on the synthetic market in sim.py.

Every night it calls the exact functions the live job calls
(signals.scan -> signals.decide -> book.manage / book.plan_entry), then fills
the orders the next day with Sanji's rule, on the option's intraday bar:

  market      : the contract's open, plus half the bid/ask (we cross the spread)
  limit buy   : the open if the open is already <= limit, else the limit if the
                day's low trades through it, else unfilled
  limit sell  : mirror image using the high
  all or nothing, and an unfilled order is not retried by Sanji

Variants: pricing "smart" (simulated-fill limits) vs "market" (everything at
market), for the full algo and for each mode alone.

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

import book                                                                 # noqa: E402
from pricing import find_row                                                # noqa: E402
from signals import Config, scan, decide, prepare_chain                     # noqa: E402
from sim import Market                                                      # noqa: E402

TICKER, CONID = "SIM", 1
FEE = 0.65          # per contract
WARMUP = 25


def order_class(o):
    i = o["id"]
    if "-exit-" in i:
        return "exit (limit)" if o["order_type"] == "lmt" else "exit (market)"
    if "-legin-" in i:
        return "leg-in short"
    if "-reshort-" in i:
        return "re-try short"
    if "-striker-dip-" in i:
        return "Striker dip"
    if "-striker-" in i:
        return "Striker entry"
    if "-wing-" in i:
        return "Sleeper wing"
    return "Sleeper short"


def run(seed=0, cfg: Config | None = None, allow_striker=True, allow_sleeper=True, pricing="smart", pcfg=None,
        n_days=260, start_cash=100_000.0, keep_nights=False, cache=None):
    cfg = cfg or Config()
    m = Market(seed=seed, n_days=n_days)
    cache = {} if cache is None else cache
    cash = start_cash
    pos: list[dict] = []
    ledger: list[dict] = []
    groups: dict[str, dict] = {}
    pending: list[dict] = []
    iv_hist, nav, nights_log, problems, fills = [], [], [], [], []
    detect = Counter()

    def nightly(i):
        if i not in cache:
            d = m.days[i]
            ch = m.chain(i)
            f = scan(ch, d.close, d.date, m.closes(i), iv_hist, cfg)
            cache[i] = (f, prepare_chain(ch, d.close, d.date, cfg))
        return cache[i]

    for i, day in enumerate(m.days):
        # ---------------- the session: Sanji fills last night's orders ----------------
        if pending:
            grid = set(float(k) for k in m.strikes(m.days[i - 1].close)) | {l["strike"] for l in pos}
            listed = set(m.expiries(i - 1)) | {date.fromisoformat(l["expiry"]) for l in pos}
            for o in pending:
                e, k, is_call = date.fromisoformat(o["expiry"]), float(o["strike"]), o["contract"] == "call"
                rec = {"class": order_class(o), "type": o["order_type"], "filled": False, "mid": o.get("_mid"),
                       "pred": o.get("_pred")}
                fills.append(rec)
                if e not in listed or k not in grid or e < day.date:
                    problems.append(f"{o['id']}: unresolved")
                    continue
                op, hi, lo, _ = m.option_bar(i, k, e, is_call)
                pay = o["action"] in ("BUY", "BTC")
                if o["order_type"] == "mkt":
                    half = max(0.025, 0.015 * op)
                    px = max(0.01, op + half if pay else op - half)
                else:
                    L = o["lmt_prc"]
                    # filled at the open = we crossed the spread like a market order (capped at the limit);
                    # filled later at the limit = our resting order got hit, no spread paid
                    half = max(0.025, 0.015 * op)
                    if pay:
                        px = min(L, op + half) if op <= L else (L if lo <= L else None)
                    else:
                        px = max(L, op - half) if op >= L else (L if hi >= L else None)
                    if px is None:
                        continue
                rec.update(filled=True, price=px)
                if rec["mid"]:
                    rec["improvement"] = (rec["mid"] - px) / rec["mid"] if pay else (px - rec["mid"]) / rec["mid"]
                q = o["qty"]
                flow = (-1 if pay else 1) * px * q * 100 - FEE * q
                cash += flow
                key = (o["contract"], e.isoformat(), k)
                leg = next((l for l in pos if (l["contract"], l["expiry"], l["strike"]) == key and l["qty"] != 0), None)
                if o["action"] in ("BUY", "SHT"):
                    signed = q if o["action"] == "BUY" else -q
                    gid = o["_group"]
                    if leg is None:
                        pos.append({"conid": CONID, "ticker": TICKER, "contract": o["contract"], "expiry": e.isoformat(),
                                    "strike": k, "qty": signed, "avg_price": px, "mark": px, "group": gid})
                    else:
                        leg["avg_price"] = (leg["avg_price"] * abs(leg["qty"]) + px * q) / (abs(leg["qty"]) + q)
                        leg["qty"] += signed
                    groups[gid]["cash"] += flow
                    groups[gid]["filled_legs"] += 1
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

        # ---------------- the close: expiries settle, mark the book ----------------
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
                iv_hist.append({"date": day.date.isoformat(), "atm_iv": nightly(i)[0].get("atm_iv")})
            continue

        # ---------------- the night: the same code the live job runs ----------------
        feat, pc = nightly(i)
        iv_hist.append({"date": day.date.isoformat(), "atm_iv": feat.get("atm_iv")})
        dec = decide(feat, cfg)
        detect[(day.day_type, dec["mode"], dec["direction"])] += 1
        if keep_nights:
            nights_log.append({"date": day.date.isoformat(), "day_type": day.day_type, "decision": dec["mode"]})
        if (dec["mode"] == "STRIKER" and not allow_striker) or (dec["mode"] == "SLEEPER" and not allow_sleeper):
            dec = dict(dec, mode="IDLE")
        for_day = m.days[i + 1].date
        pricer = book.Pricer(pricing, pcfg)
        nights = {TICKER: {"decision": dec, "feat": feat, "pc": pc, "spot": day.close}}
        held = [dict(l) for l in pos]
        exits, notes, follow_rows = book.manage(held, ledger, nights, for_day, pricer)
        why = "; ".join(n.split(": ", 1)[-1] for n in notes)
        for o in exits:
            o["_why"] = why
            partner = next((l for l in pos if (l["contract"], l["expiry"]) == (o["contract"], o["expiry"])), None)
            o["_group"] = partner["group"] if partner else None

        entries, entry_rows = [], []
        if dec["mode"] != "IDLE" and not pos:
            entries, entry_rows = book.plan_entry(TICKER, CONID, for_day, dec, feat, pc, day.close, pricer)
            gid = f"{dec['mode'].lower()}-{for_day.isoformat()}"
            for o in entries:
                o["_group"] = gid
            if entries:
                groups[gid] = {"id": gid, "mode": dec["mode"], "structure": dec["structure"], "direction": dec["direction"],
                               "qty": dec["qty"], "signal_date": day.date.isoformat(), "entry_date": for_day.isoformat(),
                               "day_type": day.day_type, "worked": day.worked, "cash": 0.0, "filled_legs": 0}
        orders = exits + entries
        clean = [{k: v for k, v in o.items() if not k.startswith("_")} for o in orders]
        problems.extend(book.check_all(clean))
        problems.extend(f"{x}: naked short" for x in book.naked_risk(clean, pos))
        for o in orders:
            info = pricer.info.get(o["id"])
            if info:
                o["_mid"], o["_pred"] = info["tonight_mid"], info["fill_prob"]
            else:
                r = find_row(pc, o["contract"], o["expiry"], o["strike"])
                o["_mid"] = r["mid"] if r else None
        sent = {o["id"] for o in orders}
        ledger.extend(r for r in entry_rows + follow_rows if r["id"] in sent)
        pending = orders

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
        t["filled"] = t.pop("filled_legs") > 0
        if not t["filled"]:
            t["exit_reason"] = "never filled"
    return {
        "seed": seed, "pricing": pricing,
        "dates": [d.date.isoformat() for d in m.days],
        "close": [round(d.close, 2) for d in m.days],
        "day_type": [d.day_type for d in m.days],
        "nav": [round(x, 2) for x in nav],
        "total_return": float(nav[-1] / start_cash - 1),
        "max_drawdown": float(((nav - peak) / peak).min()),
        "sharpe": float(rets.mean() / rets.std() * math.sqrt(252)) if rets.std() > 0 else 0.0,
        "trades": trades, "fills": fills, "detect": detect, "problems": problems, "nights": nights_log,
    }


def mode_stats(trades):
    out = {}
    for mode in ("STRIKER", "SLEEPER"):
        t = [x for x in trades if x["mode"] == mode and x["filled"]]
        p = [x["pnl"] for x in t]
        sent = sum(1 for x in trades if x["mode"] == mode)
        out[mode] = {"n": len(t), "sent": sent, "win_rate": float(np.mean([x > 0 for x in p])) if p else None,
                     "avg_pnl": float(np.mean(p)) if p else None, "total_pnl": float(np.sum(p)) if p else 0.0}
    return out


def fill_stats(fills):
    by = defaultdict(list)
    for f in fills:
        by[f["class"]].append(f)
    out = {}
    for c, fs in sorted(by.items()):
        imp = [f["improvement"] for f in fs if f.get("improvement") is not None]
        out[c] = {"sent": len(fs), "fill_rate": float(np.mean([f["filled"] for f in fs])),
                  "avg_vs_mid": float(np.mean(imp)) if imp else None,
                  "limit_share": float(np.mean([f["type"] == "lmt" for f in fs]))}
    return out


def detection_table(detect: Counter):
    types = ["normal", "informed_bull", "informed_bear", "hedge_noise", "iv_only"]
    rows = []
    for t in types:
        n = sum(v for (dt, _, _), v in detect.items() if dt == t)
        c = lambda mode, d=None: sum(v for (dt, mo, di), v in detect.items()
                                     if dt == t and mo == mode and (d is None or di == d))
        rows.append({"day_type": t, "nights": n, "striker_up": c("STRIKER", 1), "striker_down": c("STRIKER", -1),
                     "sleeper": c("SLEEPER"), "idle": c("IDLE")})
    inf = [r for r in rows if r["day_type"].startswith("informed")]
    caught = sum(r["striker_up"] if r["day_type"] == "informed_bull" else r["striker_down"] for r in inf)
    wrong_way = sum(r["striker_down"] if r["day_type"] == "informed_bull" else r["striker_up"] for r in inf)
    n_inf = sum(r["nights"] for r in inf)
    others = [r for r in rows if not r["day_type"].startswith("informed")]
    false_alarm = sum(r["striker_up"] + r["striker_down"] for r in others)
    n_oth = sum(r["nights"] for r in others)
    strikes = caught + wrong_way + false_alarm
    return {"rows": rows, "catch_rate": caught / n_inf if n_inf else None, "wrong_way": wrong_way,
            "false_alarm_rate": false_alarm / n_oth if n_oth else None,
            "precision": caught / strikes if strikes else None}


from pricing import PricingConfig                                           # noqa: E402

VARIANTS = {
    "full_smart": dict(pricing="smart"),
    "full_market": dict(pricing="market"),
    "sleeper_smart": dict(pricing="smart", allow_striker=False),
    "sleeper_limit": dict(pricing="smart", allow_striker=False, pcfg=PricingConfig(sleeper_style="limit")),
    "striker_smart": dict(pricing="smart", allow_sleeper=False),
    "striker_market": dict(pricing="market", allow_sleeper=False),
    "striker_dip": dict(pricing="smart", allow_sleeper=False, pcfg=PricingConfig(striker_style="dip")),
    "striker_protected": dict(pricing="smart", allow_sleeper=False, pcfg=PricingConfig(striker_style="protected")),
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, default=20)
    ap.add_argument("--out", default=os.path.join(HERE, "data", "backtest.json"))
    a = ap.parse_args()

    agg = {v: [] for v in VARIANTS}
    det = Counter()
    all_trades, all_fills = defaultdict(list), defaultdict(list)
    calib = []
    sample, problems = {}, []
    for s in range(a.seeds):
        cache = {}
        for v, kw in VARIANTS.items():
            r = run(seed=s, keep_nights=(s == 0 and v == "full_smart"), cache=cache, **kw)
            agg[v].append({k: r[k] for k in ("total_return", "max_drawdown", "sharpe")})
            all_trades[v].extend(r["trades"])
            all_fills[v].extend(r["fills"])
            calib.extend((f["pred"], f["filled"]) for f in r["fills"] if f.get("pred") is not None)
            problems.extend(f"[{v}] {p}" for p in r["problems"])
            if v == "full_smart":
                det.update(r["detect"])
            if s == 0:
                sample[v] = r
        print(f"seed {s:2d}: " + "  ".join(f"{v} {agg[v][-1]['total_return']:+.1%}" for v in VARIANTS), flush=True)

    summary = {}
    for v, rs in agg.items():
        tr = [r["total_return"] for r in rs]
        summary[v] = {"mean_return": float(np.mean(tr)), "median_return": float(np.median(tr)),
                      "pct_seeds_positive": float(np.mean([x > 0 for x in tr])),
                      "mean_max_dd": float(np.mean([r["max_drawdown"] for r in rs])),
                      "mean_sharpe": float(np.mean([r["sharpe"] for r in rs])),
                      "modes": mode_stats(all_trades[v]), "fills": fill_stats(all_fills[v])}

    split = {}
    st = [t for t in all_trades["full_smart"] if t["mode"] == "STRIKER"]
    for label, sel in [("informed_worked", lambda t: t["worked"] is True),
                       ("informed_fizzled", lambda t: t["worked"] is False),
                       ("not_informed", lambda t: t["worked"] is None)]:
        ts = [t for t in st if sel(t)]
        p = [t["pnl"] for t in ts if t["filled"]]
        split[label] = {"sent": len(ts), "filled": len(p), "avg_pnl": float(np.mean(p)) if p else None,
                        "win_rate": float(np.mean([x > 0 for x in p])) if p else None}

    c = np.array(calib, dtype=float) if calib else np.zeros((0, 2))
    calibration = []
    for lo, hi in [(0, .5), (.5, .65), (.65, .8), (.8, .9), (.9, 1.01)]:
        mk = (c[:, 0] >= lo) & (c[:, 0] < hi) if len(c) else np.array([], bool)
        if mk.any():
            calibration.append({"bucket": f"{lo:.0%}-{min(hi, 1):.0%}", "n": int(mk.sum()),
                                "predicted": float(c[mk, 0].mean()), "realized": float(c[mk, 1].mean())})

    d = detection_table(det)
    s0 = sample["full_smart"]
    out = {
        "seeds": a.seeds, "days_per_seed": len(s0["dates"]), "warmup": WARMUP,
        "config": Config().to_dict(), "variants": list(VARIANTS),
        "summary": summary, "detection": d, "striker_by_outcome": split, "fill_calibration": calibration,
        "order_problems": problems[:50], "n_order_problems": len(problems),
        "seed0": {"dates": s0["dates"], "close": s0["close"], "day_type": s0["day_type"],
                  "nav": {v: sample[v]["nav"] for v in ("full_smart", "full_market", "sleeper_smart", "striker_smart")},
                  "trades": s0["trades"]},
    }
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    with open(a.out, "w") as fh:
        json.dump(out, fh, indent=1, default=str)
    for v, s in summary.items():
        print(f"{v:15s} mean {s['mean_return']:+.1%} median {s['median_return']:+.1%} pos {s['pct_seeds_positive']:.0%} "
              f"dd {s['mean_max_dd']:.1%} sharpe {s['mean_sharpe']:.2f} | "
              + " ".join(f"{m}: {x['n']}/{x['sent']} win {x['win_rate'] or 0:.0%} avg {x['avg_pnl'] or 0:+.0f}" for m, x in s["modes"].items()))
        for c, f in s["fills"].items():
            vm = "n/a" if f["avg_vs_mid"] is None else f"{f['avg_vs_mid'] * 100:+.1f}%"
            print(f"      {c:15s} sent {f['sent']:5d} filled {f['fill_rate']:.0%} vs mid {vm}")
    print({k: v for k, v in d.items() if k != "rows"}, "problems", len(problems), problems[:5])
    print(split)


if __name__ == "__main__":
    main()
