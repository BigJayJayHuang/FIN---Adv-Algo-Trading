"""
Run:  python -m pytest final/tests -q

Three layers:
  1. hand-built chains where we know the answer exactly
  2. planted scenarios from sim.py (normal / informed / hedge noise / IV-only), many seeds
  3. the Sanji rules, the order builder, exits, and the whole nightly job end to end
"""
import json
import os
import sys
from datetime import date, datetime

import numpy as np
import pandas as pd
import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import book                                                    # noqa: E402
from sanji import validate, resolve_expiry, resolve_strike      # noqa: E402
from signals import Config, bs_price, bs_greeks, implied_vol, scan, decide   # noqa: E402
from sim import Market                                          # noqa: E402

ASOF = date(2026, 11, 16)


# ----------------------------------------------------------------------------
# 1. hand-built chains
# ----------------------------------------------------------------------------
def flat_chain(spot=100.0, iv=0.5, expiries=(date(2026, 11, 20), date(2026, 11, 27)), vol_frac=0.2):
    rows = []
    for e in expiries:
        T = (e - ASOF).days / 365
        for k in np.arange(80, 121, 2.5):
            for typ in "CP":
                mid = float(bs_price(spot, k, T, iv, typ == "C"))
                if mid < 0.05:
                    continue
                oi = 2000
                rows.append([e, typ, float(k), mid - 0.05, mid + 0.05, mid, int(oi * vol_frac), oi, iv])
    return pd.DataFrame(rows, columns=["expiry", "type", "strike", "bid", "ask", "last", "volume", "oi", "iv"])


def sweep(df, typ, strikes, at="ask", mult=8):
    df = df.copy()
    for k in strikes:
        i = df[(df["type"] == typ) & (df["strike"] == k) & (df["expiry"] == date(2026, 11, 27))].index
        df.loc[i, "volume"] = df.loc[i, "oi"] * mult
        df.loc[i, "last"] = df.loc[i, "ask"] if at == "ask" else df.loc[i, "bid"]
    return df


def rising_closes(n=40, start=90.0, end=100.0):
    return pd.Series(np.linspace(start, end, n) * (1 + 0.01 * np.sin(np.arange(n))),
                     index=pd.bdate_range(end=ASOF, periods=n).date)


def iv_hist(level=0.42, n=20):
    return [{"date": d.isoformat(), "atm_iv": level} for d in pd.bdate_range(end=date(2026, 11, 13), periods=n).date]


def test_greeks_sane():
    c = float(bs_price(100, 100, 30 / 365, 0.4, True))
    p = float(bs_price(100, 100, 30 / 365, 0.4, False))
    assert c - p == pytest.approx(100 - 100 * np.exp(-0.04 * 30 / 365), abs=1e-6)   # put-call parity
    d, g, v, th = bs_greeks(100, np.array([90, 100, 110]), 30 / 365, 0.4, np.array([True, True, True]))
    assert 0.5 < d[1] < 0.6 and d[0] > d[1] > d[2] and (g > 0).all() and (th < 0).all()
    assert implied_vol(c, 100, 100, 30 / 365, True) == pytest.approx(0.4, abs=1e-4)


def test_quiet_chain_has_no_irregular_flow():
    f = scan(flat_chain(), 100.0, ASOF, rising_closes(), iv_hist())
    assert f["ok"] and f["n_unusual"] == 0 and f["unusual_share"] == 0
    assert decide(f)["mode"] != "STRIKER"


def test_call_sweep_at_the_ask_is_bullish_flow():
    df = sweep(flat_chain(iv=0.5), "C", [102.5, 105, 107.5, 110])
    f = scan(df, 100.0, ASOF, rising_closes(), iv_hist(0.42))
    assert f["n_unusual"] == 4
    assert f["flow_score"] > 0.9
    assert f["iv_change_1d"] > 0.08                      # 0.42 -> 0.50
    d = decide(f)
    assert d["mode"] == "STRIKER" and d["direction"] == 1
    assert {c["name"] for c in d["striker_checks"] if c["passed"]} >= {"Irregular flow", "Flow is one-sided", "IV popped"}


def test_put_sweep_at_the_ask_is_bearish_flow():
    df = sweep(flat_chain(iv=0.5), "P", [97.5, 95, 92.5, 90])
    f = scan(df, 100.0, ASOF, rising_closes(110, 100), iv_hist(0.42))
    d = decide(f)
    assert f["flow_score"] < -0.9 and d["mode"] == "STRIKER" and d["direction"] == -1


def test_calls_sold_at_the_bid_is_not_bullish():
    """Same contracts, but hitting the bid = someone writing calls -> bearish, not bullish."""
    df = sweep(flat_chain(iv=0.5), "C", [102.5, 105, 107.5, 110], at="bid")
    f = scan(df, 100.0, ASOF, rising_closes(), iv_hist(0.42))
    assert f["flow_score"] < -0.9
    assert decide(f)["direction"] != 1


def test_straddle_buying_is_irregular_but_not_directional():
    df = flat_chain(iv=0.5)
    for typ in "CP":
        df = sweep(df, typ, [97.5, 100, 102.5])
    f = scan(df, 100.0, ASOF, rising_closes(), iv_hist(0.42))
    assert f["unusual_share"] > 0.2 and f["n_straddle_legs"] == 6 and abs(f["flow_score"]) < 0.05
    d = decide(f)
    assert d["mode"] == "IDLE" and "stand aside" in d["reason"]


def test_iv_spike_without_flow_never_strikes():
    f = scan(flat_chain(iv=0.7), 100.0, ASOF, rising_closes(), iv_hist(0.42))
    assert f["iv_change_1d"] > 0.5
    assert decide(f)["mode"] != "STRIKER"


def test_one_big_print_is_not_enough():
    df = sweep(flat_chain(iv=0.5), "C", [105], mult=20)
    f = scan(df, 100.0, ASOF, rising_closes(), iv_hist(0.42))
    assert f["n_unusual"] == 1 and decide(f)["mode"] != "STRIKER"


def test_quiet_rich_iv_sells_premium():
    closes = pd.Series(100 * np.exp(np.cumsum(np.r_[0, np.full(39, 0.0005)] + 0.008 * np.tile([1, -1], 20))),
                       index=pd.bdate_range(end=ASOF, periods=40).date)
    f = scan(flat_chain(spot=float(closes.iloc[-1]), iv=0.45), float(closes.iloc[-1]), ASOF, closes, iv_hist(0.45))
    assert f["vrp"] > 0.03
    d = decide(f)
    assert d["mode"] == "SLEEPER" and d["structure"] == "bull_put_spread"
    assert d["strikes"]["short"].startswith("pct:-") and int(d["strikes"]["wing"][4:]) < int(d["strikes"]["short"][4:])


def test_empty_or_broken_chain_is_idle():
    f = scan(flat_chain().iloc[0:0], 100.0, ASOF, rising_closes(), [])
    assert decide(f)["mode"] == "IDLE"
    junk = flat_chain()
    junk["iv"] = np.nan                                  # source gave no IVs -> solved from mids
    f2 = scan(junk, 100.0, ASOF, rising_closes(), [])
    assert f2["ok"] and f2["atm_iv"] == pytest.approx(0.5, abs=0.02)


# ----------------------------------------------------------------------------
# 2. planted scenarios, many seeds
# ----------------------------------------------------------------------------
def night(day_type, seed):
    n = 40
    m = Market(seed=seed, n_days=n, day_types=["normal"] * (n - 1) + [day_type])
    ivh = []
    for j in range(n - 1):
        f = scan(m.chain(j), m.days[j].close, m.dates[j], m.closes(j), ivh)
        ivh.append({"date": m.dates[j].isoformat(), "atm_iv": f["atm_iv"]})
    f = scan(m.chain(n - 1), m.days[-1].close, m.dates[-1], m.closes(n - 1), ivh)
    return f, decide(f)


SEEDS = range(12)


@pytest.fixture(scope="module")
def scenario_results():
    return {t: [night(t, s) for s in SEEDS]
            for t in ["normal", "informed_bull", "informed_bear", "hedge_noise", "iv_only"]}


def test_normal_nights_never_strike(scenario_results):
    assert all(d["mode"] != "STRIKER" for _, d in scenario_results["normal"])


@pytest.mark.parametrize("t,want", [("informed_bull", 1), ("informed_bear", -1)])
def test_informed_flow_is_caught_in_the_right_direction(scenario_results, t, want):
    res = scenario_results[t]
    assert all(f["n_unusual"] >= 3 for f, _ in res)                         # the scanner sees it every time
    assert all(np.sign(f["flow_score"]) == want for f, _ in res)            # and reads the direction right
    strikes = [d for _, d in res if d["mode"] == "STRIKER"]
    assert len(strikes) >= 0.75 * len(res)                                  # confirmation filters drop a few
    assert all(d["direction"] == want for d in strikes)                     # never the wrong way


def test_hedge_noise_is_flagged_but_not_traded(scenario_results):
    res = scenario_results["hedge_noise"]
    assert all(f["unusual_share"] >= 0.2 for f, _ in res)
    assert sum(d["mode"] == "STRIKER" for _, d in res) <= 1


def test_iv_only_nights_never_strike(scenario_results):
    res = scenario_results["iv_only"]
    assert all(f["iv_change_1d"] > 0.05 for f, _ in res)
    assert all(d["mode"] != "STRIKER" for _, d in res)


# ----------------------------------------------------------------------------
# 3. Sanji rules, orders, exits, end to end
# ----------------------------------------------------------------------------
SPEC_EXAMPLE = {"id": "tsla-csp-2026-11-05", "for": "2026-11-05", "order_type": "lmt", "action": "SHT", "qty": 1,
                "underlying": 76792991, "contract": "put", "expiry": "5DTE+", "strike": "otm:1", "ref": "open",
                "lmt_prc": "mid"}


def test_spec_example_is_legal():
    assert validate(SPEC_EXAMPLE) == []


@pytest.mark.parametrize("patch", [
    {"qty": 0}, {"qty": 1.5}, {"for": "next"}, {"action": "SELL"}, {"underlying": "TSLA"},
    {"strike": "vega:20"}, {"expiry": "next week"}, {"order_type": "mkt"}, {"color": "red"}, {"ref": "close"},
])
def test_illegal_orders_are_rejected(patch):
    assert validate({**SPEC_EXAMPLE, **patch}) != []


def test_resolver_matches_spec_wording():
    ks = [95, 100, 105, 110]
    assert resolve_strike("atm", "put", 102.5, ks) == 100            # tie -> lower
    assert resolve_strike("otm:1", "put", 101, ks) == 95              # puts: OTM is down
    assert resolve_strike("otm:1", "call", 101, ks) == 105            # calls: up
    assert resolve_strike("itm:1", "call", 101, ks) == 95
    assert resolve_strike("pct:5", "call", 100, ks) == 105
    assert resolve_strike("pct:-7.5", "put", 100, ks) == 90 or resolve_strike("pct:-7.5", "put", 100, ks) == 95
    exps = [date(2026, 11, 20), date(2026, 11, 27), date(2026, 12, 18)]
    assert resolve_expiry("5DTE+", date(2026, 11, 17), exps) == date(2026, 11, 27)
    assert resolve_expiry("5DTE-", date(2026, 11, 17), exps) == date(2026, 11, 20)
    assert resolve_expiry("W+", date(2026, 11, 17), exps) == date(2026, 11, 20)
    assert resolve_expiry("M+", date(2026, 11, 17), exps) == date(2026, 11, 20)   # Nov 20 is the 3rd Friday
    assert resolve_expiry("M+", date(2026, 11, 23), exps) == date(2026, 12, 18)
    assert resolve_expiry("5DTE", date(2026, 11, 17), exps) is None


def test_every_structure_builds_legal_orders():
    for (mode, structure, direction), legs in book.STRUCTURES.items():
        strikes = {"long": f"pct:{2 * direction}", "short": f"pct:{8 * direction}", "wing": f"pct:{-12 * direction}"}
        dec = {"mode": mode, "structure": structure, "direction": direction, "qty": 3, "strikes": strikes}
        os_ = book.entry_orders("TSLA", 76792991, date(2026, 11, 17), dec)
        assert len(os_) == len(legs) and book.check_all(os_) == []


def test_exit_rules():
    fd = date(2026, 11, 17)
    long_call = [{"ticker": "TSLA", "conid": 1, "contract": "call", "expiry": "2026-11-27", "strike": 410.0,
                  "qty": 2, "avg_price": 5.0, "mark": 11.0}]
    o, n = book.exit_orders(long_call, {}, {"TSLA": 400}, fd)
    assert o and o[0]["action"] == "SEL" and "take profit" in n[0] and book.check_all(o) == []
    o, _ = book.exit_orders([dict(long_call[0], mark=4.0)], {}, {"TSLA": 400}, fd)
    assert o == []                                                     # nothing fires
    o, n = book.exit_orders([dict(long_call[0], mark=4.0)], {"TSLA": {"mode": "STRIKER", "direction": -1}}, {}, fd)
    assert o and "against" in n[0]
    o, n = book.exit_orders([dict(long_call[0], expiry="2026-11-18", mark=4.0)], {}, {}, fd)
    assert o and "expiry" in n[0]

    put_spread = [
        {"ticker": "TSLA", "conid": 1, "contract": "put", "expiry": "2026-11-27", "strike": 380.0, "qty": -1,
         "avg_price": 4.0, "mark": 1.5},
        {"ticker": "TSLA", "conid": 1, "contract": "put", "expiry": "2026-11-27", "strike": 360.0, "qty": 1,
         "avg_price": 1.0, "mark": 0.3}]
    o, n = book.exit_orders(put_spread, {}, {"TSLA": 400}, fd)        # credit 3.00, now 1.20 -> kept 60%
    assert {x["action"] for x in o} == {"BTC", "SEL"} and "credit" in n[0]
    hold = [dict(put_spread[0], mark=3.5), dict(put_spread[1], mark=1.0)]
    o, n = book.exit_orders(hold, {}, {"TSLA": 379}, fd)               # spot under the short strike
    assert o and "breached" in n[0]


def test_parse_positions_variants():
    m = {76792991: "TSLA"}
    a = book.parse_positions([{"underlying": 76792991, "contract": "put", "expiry": "2026-11-27", "strike": 380,
                               "qty": -1, "avg_price": 4}], m)
    b = book.parse_positions({"positions": [{"conid": "76792991", "right": "P", "expiration": "2026-11-27",
                                             "strike": "380", "position": 1, "side": "short"}]}, m)
    assert a[0]["qty"] == -1 and b[0]["qty"] == -1 and a[0]["ticker"] == "TSLA"
    assert book.parse_positions(None, m) == [] and book.parse_positions({"cash": 1e5}, m) == []


def test_session_calendar():
    import algo
    from zoneinfo import ZoneInfo
    et = ZoneInfo("America/New_York")
    assert algo.next_session(datetime(2026, 11, 16, 22, 0, tzinfo=et)) == date(2026, 11, 17)
    assert algo.next_session(datetime(2026, 11, 25, 22, 0, tzinfo=et)) == date(2026, 11, 27)   # Thanksgiving
    assert algo.next_session(datetime(2026, 11, 20, 22, 0, tzinfo=et)) == date(2026, 11, 23)   # Fri -> Mon
    assert algo.next_session(datetime(2026, 11, 17, 8, 0, tzinfo=et)) == date(2026, 11, 17)


def test_nightly_job_end_to_end(tmp_path):
    import algo
    from zoneinfo import ZoneInfo
    root = tmp_path
    (root / "price_history.json").write_text(json.dumps({
        "272093": {"tracked": True, "name": "MSFT", "price_history": {"2026-11-13": {"close": 500}}},
        "76792991": {"tracked": True, "name": "TSLA", "price_history": {"2026-11-13": {"close": 400}}},
        "4815747": {"tracked": True, "name": "NVDA", "price_history": {}},
    }))
    latest = algo.run(str(root), datetime(2026, 11, 16, 22, 0, tzinfo=ZoneInfo("America/New_York")), demo=True)
    orders = json.loads((root / "open_orders.json").read_text())
    assert isinstance(orders, list) and book.check_all(orders) == []
    assert all(o["for"] == "2026-11-17" for o in orders)
    modes = {t["symbol"]: t["decision"]["mode"] for t in latest["tickers"]}
    assert modes["TSLA"] == "STRIKER"                                  # planted bullish sweep
    assert modes["NVDA"] != "STRIKER"                                  # planted straddle noise
    assert any(o["underlying"] == 76792991 and o["action"] == "BUY" and o["contract"] == "call" for o in orders)
    assert (root / "final" / "data" / "latest.json").exists()
