import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tracker as T  # noqa: E402


def test_demo_run(tmp_path):
    t = T.run(demo=True, out_dir=str(tmp_path), minute_days=0)
    for f in ("tracker.json", "tracker_data.js", "events.json", "pm_log.json"):
        assert (tmp_path / f).exists()
    # planted oil burst: three linked markets, followed by a real move
    oil = [e for e in t["events_recent"] if e["sym"] == "CL=F" and e["followed"]]
    assert oil and set(oil[0]["cluster"]) >= {"CL=F", "BZ=F", "USO"}
    # the burst at the 4pm close is public, so it must be tagged scheduled
    ev = json.load(open(tmp_path / "events.json"))
    assert any(e["sym"] == "NVDA" and e["scheduled"] for e in ev)
    # the Iran odds jump is flagged and mapped to oil
    pm = [p for p in t["pm"] if p["flagged"]]
    assert pm and "CL=F" in pm[0]["assets"]
    # oil names light up today
    top = {r["sym"] for r in t["today"] if r["score"] >= 2}
    assert "CL=F" in top
    assert len(t["history"]["dates"]) == len(t["history"]["flagged"]) > 300
    # Huang-style options flow: far OTM calls, new positions -> footprint on NVDA
    nv = next(o for o in t["options"] if o["sym"] == "NVDA")
    assert nv["footprint"] and nv["lean"] > 0.4
    # out-of-place wallets: the dormant one and the sports bettor, not the regular
    sus = {w["name"] for w in t["wallets"] if w["suspicious"]}
    assert sus == {"bbbbbbbbzzz", "BrotherObama"}
    assert next(r for r in t["today"] if r["sym"] == "NVDA")["score"] >= 2
    # greeks layer: NVDA's front month carries event premium with no earnings, calls richer than puts
    assert nv["vol_footprint"] and nv["term"] > 0.05 and nv["iv_spread"] > 0 and nv["net_delta_usd"] > 0


def test_rerun_keeps_archive(tmp_path):
    T.run(demo=True, out_dir=str(tmp_path), minute_days=0)
    n1 = len(json.load(open(tmp_path / "events.json")))
    T.run(demo=True, out_dir=str(tmp_path), minute_days=0)
    n2 = len(json.load(open(tmp_path / "events.json")))
    assert n1 == n2  # same bursts are merged, not duplicated


def test_pm_keywords():
    assert "CL=F" in T.pm_assets("Will the Strait of Hormuz close?")
    assert "TLT" in T.pm_assets("Fed rate cut in December?")
    assert T.pm_assets("Who wins the Super Bowl?") == []


def test_topic():
    import flow
    assert flow.topic("Lakers vs. Celtics") == "Sports"
    assert flow.topic("Will Jensen Huang rejoin NVIDIA by Nov 30?") == "Companies / CEOs"


def test_greeks_and_vol_signals():
    from datetime import date
    import numpy as np
    import flow
    # Black-Scholes sanity: ATM call delta ~0.5+, put delta = call delta - 1
    p, d, g, v = flow.bs(100.0, np.array([100.0, 100.0]), np.array([0.25, 0.25]), np.array([0.3, 0.3]), np.array([True, False]))
    assert 0.5 < d[0] < 0.6 and abs(d[1] - (d[0] - 1)) < 1e-9 and g[0] > 0 and v[0] > 0
    # implied vol solver recovers the input
    iv = flow.solve_iv(p, 100.0, np.array([100.0, 100.0]), np.array([0.25, 0.25]), np.array([True, False]))
    assert np.allclose(iv, 0.3, atol=1e-3)
    # night-over-night: IV pops 15% on a quiet day and calls get bid -> vol footprint, bullish lean
    hist = [{"atm_iv": 0.40, "iv_spread": 0.00, "smirk": 0.04}]
    f = {"atm_iv": 0.46, "back_iv": 0.44, "term": 0.02, "iv_spread": 0.04, "smirk": 0.04, "front_exp": "2026-12-04"}
    out = flow.vol_footprint(f, hist, rz=0.3, earnings=None, asof=date(2026, 11, 24))
    assert out["vol_footprint"] and out["vol_lean"] == 1
    # same IV pop but the stock itself ripped -> not "quiet", and no skew move -> nothing
    f2 = {"atm_iv": 0.46, "back_iv": 0.44, "term": 0.02, "iv_spread": 0.0, "smirk": 0.04, "front_exp": "2026-12-04"}
    assert not flow.vol_footprint(f2, hist, rz=3.0, earnings=None, asof=date(2026, 11, 24))["vol_footprint"]
    # inverted term structure explained by earnings inside the front expiry -> not a footprint
    f3 = {"atm_iv": 0.70, "back_iv": 0.45, "term": 0.25, "iv_spread": 0.0, "smirk": 0.04, "front_exp": "2026-12-04"}
    assert not flow.vol_footprint(f3, hist[:0], rz=0.0, earnings=["2026-12-01"], asof=date(2026, 11, 24))["vol_footprint"]
