"""
Run:  python -m pytest final-cycle/tests -q

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


FD = date(2026, 11, 17)


def prepared(spot=100.0, iv=0.5):
    from signals import prepare_chain
    return prepare_chain(flat_chain(spot=spot, iv=iv), spot, ASOF, Config())


STRUCTS = [("STRIKER", "long_option", 1), ("STRIKER", "long_option", -1), ("STRIKER", "debit_spread", 1),
           ("STRIKER", "debit_spread", -1), ("SLEEPER", "bull_put_spread", 1), ("SLEEPER", "bear_call_spread", -1)]


def dec_for(mode, structure, d, qty=3, points=4):
    return {"mode": mode, "structure": structure, "direction": d, "qty": qty, "points": points,
            "strikes": {"long": f"pct:{2 * d}", "short": f"pct:{8 * d}" if mode == "STRIKER" else f"pct:{-6 * d}",
                        "wing": f"pct:{-12 * d}"}}


@pytest.mark.parametrize("pricing", ["smart", "market"])
def test_every_structure_builds_legal_concrete_orders(pricing):
    pc = prepared()
    for mode, structure, d in STRUCTS:
        orders, rows = book.plan_entry("TSLA", 76792991, FD, dec_for(mode, structure, d), {"atm_iv": 0.5, "ret1": -0.01},
                                       pc, 100.0, book.Pricer(pricing))
        assert orders and book.check_all(orders) == []
        for o in orders:          # exact contracts: Sanji needs no chain snapshot for these
            assert isinstance(o["strike"], float) and o["expiry"] == "2026-11-27"
            assert o["order_type"] == "mkt" or isinstance(o["lmt_prc"], float)
        assert book.naked_risk(orders, []) == []                        # a short never goes out alone
        if mode == "SLEEPER":
            wing = next(o for o in orders if "-wing-" in o["id"])
            short = next(o for o in orders if o["action"] == "SHT")
            assert wing["order_type"] == "mkt"
            assert (wing["strike"] < short["strike"] < 100) if d > 0 else (100 < short["strike"] < wing["strike"])
            assert short["order_type"] == "mkt"                       # default: Sleeper trades at market (see backtest)
        if structure == "debit_spread":
            # smart: only the long goes out; the short is legged in once the long is owned
            assert len(orders) == (1 if pricing == "smart" else 2)
            assert rows[-1]["plan"].get("short_pct") == (8 * d if pricing == "smart" else None)


def test_sleeper_limit_style_still_never_naked():
    from pricing import PricingConfig
    pc = prepared()
    orders, _ = book.plan_entry("TSLA", 1, FD, dec_for("SLEEPER", "bull_put_spread", 1), {"atm_iv": 0.5}, pc, 100.0,
                                book.Pricer("smart", PricingConfig(sleeper_style="limit")))
    short = next(o for o in orders if o["action"] == "SHT")
    assert short["order_type"] == "lmt" and book.naked_risk(orders, []) == []


def test_striker_styles():
    from pricing import PricingConfig
    pc = prepared()
    feat = {"atm_iv": 0.5, "ret1": -0.02}
    get = lambda style: book.plan_entry("TSLA", 1, FD, dec_for("STRIKER", "long_option", 1), feat, pc, 100.0,
                                        book.Pricer("smart", PricingConfig(striker_style=style)))[0]
    assert get("market")[0]["order_type"] == "mkt"
    dip, prot = get("dip")[0], get("protected")[0]
    assert dip["lmt_prc"] < prot["lmt_prc"]                           # dip bids low, protected pays up
    split = get("split")
    assert [o["qty"] for o in split] == [2, 1] and split[0]["lmt_prc"] > split[1]["lmt_prc"]


def test_naked_short_is_caught():
    o = book.make_order("x", FD, "SHT", 1, 1, "put", "2026-11-27", 95.0, 1.2)
    assert book.naked_risk([o], []) == ["x"]
    wing_lmt = book.make_order("w", FD, "BUY", 1, 1, "put", "2026-11-27", 90.0, 0.4)
    assert book.naked_risk([o, wing_lmt], []) == ["x"]                # a limit wing might not fill
    held = [{"conid": 1, "contract": "put", "expiry": "2026-11-27", "qty": 1}]
    assert book.naked_risk([o], held) == []


# ---------------------------------------------------------------- pricing
def row_at(pc, contract, strike, expiry=date(2026, 11, 27)):
    from pricing import find_row
    return find_row(pc, contract, expiry.isoformat(), strike)


def test_sanji_fill_rule():
    from pricing import sanji_fill
    o, h, l = np.array([2.0, 2.0, 2.0]), np.array([2.5, 2.5, 2.1]), np.array([1.5, 1.9, 1.95])
    f, px = sanji_fill("buy", 1.9, o, h, l)
    assert f.tolist() == [True, True, False] and px[0] == 1.9
    f, px = sanji_fill("buy", 2.2, o, h, l)
    assert f.all() and (px == 2.0).all()                                # open already through: fill at open
    f, px = sanji_fill("sell", 2.4, o, h, l)
    assert f.tolist() == [True, True, False]


def test_limit_price_behaves():
    from pricing import price_limit, round_to_tick
    pc = prepared()
    r = row_at(pc, "call", 102.5)
    sd = 0.5 / np.sqrt(252)
    lo = price_limit("buy", r, 100, FD, sd, None, 0.5)
    hi = price_limit("buy", r, 100, FD, sd, None, 0.9)
    assert lo["limit"] < hi["limit"] <= r["ask"]                      # more urgency -> pay more, never above ask
    falling = price_limit("buy", r, 100, FD, sd, -0.04, 0.6)
    rising = price_limit("buy", r, 100, FD, sd, +0.04, 0.6)
    assert falling["limit"] < rising["limit"]                         # sliding into the open -> bid lower
    assert abs(falling["fill_prob"] - 0.6) < 0.08
    s = price_limit("sell", r, 100, FD, sd, None, 0.5)
    assert s["limit"] >= r["bid"] and s["edge_vs_mid"] > 0
    assert round_to_tick(2.987, "buy") == 2.98 and round_to_tick(3.02, "buy") == 3.0 and round_to_tick(3.02, "sell") == 3.05


# ---------------------------------------------------------------- managing the book
def book_night(decision=None, spot=100.0):
    return {"TSLA": {"decision": decision or {}, "feat": {"atm_iv": 0.5, "ret1": 0.0}, "pc": prepared(spot), "spot": spot}}


def test_exit_rules():
    P = book.Pricer("smart")
    long_call = [{"ticker": "TSLA", "conid": 1, "contract": "call", "expiry": "2026-11-27", "strike": 102.5,
                  "qty": 2, "avg_price": 1.0, "mark": 2.5}]
    o, n, _ = book.manage(long_call, [], book_night(), FD, P)
    assert o and o[0]["action"] == "SEL" and o[0]["order_type"] == "lmt" and "take profit" in n[0]
    o, _, _ = book.manage([dict(long_call[0], avg_price=2.5, mark=2.4)], [], book_night(), FD, P)
    assert o == []                                                     # nothing fires
    o, n, _ = book.manage([dict(long_call[0], avg_price=2.5, mark=2.4)], [],
                          book_night({"mode": "STRIKER", "direction": -1}), FD, P)
    assert o and "against" in n[0] and o[0]["order_type"] == "mkt"    # urgent -> market
    o, n, _ = book.manage([dict(long_call[0], expiry="2026-11-18", avg_price=2.5, mark=2.4)], [], book_night(), FD, P)
    assert o and "expiry" in n[0] and o[0]["order_type"] == "mkt"

    put_spread = [
        {"ticker": "TSLA", "conid": 1, "contract": "put", "expiry": "2026-11-27", "strike": 95.0, "qty": -1,
         "avg_price": 2.0, "mark": 0.6},
        {"ticker": "TSLA", "conid": 1, "contract": "put", "expiry": "2026-11-27", "strike": 90.0, "qty": 1,
         "avg_price": 0.8, "mark": 0.2}]
    o, n, _ = book.manage(put_spread, [], book_night(), FD, P)             # credit 1.20, now 0.40 -> kept 67%
    assert [x["action"] for x in o] == ["BTC"] and "credit" in n[0]   # cover the short first, alone
    hold = [dict(put_spread[0], mark=2.5), dict(put_spread[1], mark=1.0)]
    o, n, _ = book.manage(hold, [], book_night(spot=94.0), FD, P)          # spot under the short strike
    assert {x["action"] for x in o} == {"BTC", "SEL"} and "breached" in n[0] and all(x["order_type"] == "mkt" for x in o)
    for orders in (o,):
        assert book.check_all(orders) == []


def test_leg_in_short_after_long_fills():
    P = book.Pricer("smart")
    ledger = [{"id": "tsla-striker-long-2026-11-16", "ticker": "TSLA", "conid": 1, "for": "2026-11-16", "mode": "STRIKER",
               "structure": "debit_spread", "role": "long", "contract": "call", "expiry": "2026-11-27",
               "strike": 102.5, "qty": 2, "plan": {"short_pct": 8}}]
    held = [{"ticker": "TSLA", "conid": 1, "contract": "call", "expiry": "2026-11-27", "strike": 102.5,
             "qty": 2, "avg_price": 1.4, "mark": 1.5}]
    o, n, rows = book.manage(held, ledger, book_night(), FD, P)
    assert len(o) == 1 and o[0]["action"] == "SHT" and o[0]["strike"] > 102.5 and o[0]["qty"] == 2
    assert o[0]["expiry"] == "2026-11-27" and "-legin-" in o[0]["id"] and rows[0]["role"] == "short"
    assert book.naked_risk(o, held) == []                              # covered by the long we own


def test_orphan_wing_retries_or_sells():
    P = book.Pricer("smart")
    ledger = [{"id": "w", "ticker": "TSLA", "conid": 1, "for": "2026-11-16", "mode": "SLEEPER", "structure": "bull_put_spread",
               "role": "wing", "contract": "put", "expiry": "2026-11-27", "strike": 87.5, "qty": 1, "plan": {}}]
    wing = [{"ticker": "TSLA", "conid": 1, "contract": "put", "expiry": "2026-11-27", "strike": 87.5, "qty": 1,
             "avg_price": 0.5, "mark": None}]
    again = {"mode": "SLEEPER", "structure": "bull_put_spread", "direction": 1, "strikes": {"short": "pct:-6", "wing": "pct:-12"}}
    o, n, _ = book.manage([dict(wing[0])], ledger, book_night(again), FD, P)
    assert o[0]["action"] == "SHT" and 87.5 < o[0]["strike"] < 100 and "re-trying" in n[0]
    o, n, _ = book.manage([dict(wing[0])], ledger, book_night({"mode": "IDLE"}), FD, P)
    assert o[0]["action"] == "SEL" and "wing without" in n[0]


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
    assert json.loads((root / "mkt" / "open_orders.json").read_text()) == orders
    assert isinstance(orders, list) and book.check_all(orders) == []
    assert all(o["for"] == "2026-11-17" for o in orders)
    modes = {t["symbol"]: t["decision"]["mode"] for t in latest["tickers"]}
    assert modes["TSLA"] == "STRIKER"                                  # planted bullish sweep
    assert modes["NVDA"] != "STRIKER"                                  # planted straddle noise
    assert any(o["underlying"] == 76792991 and o["action"] == "BUY" and o["contract"] == "call" for o in orders)
    assert all(isinstance(o["strike"], float) and len(o["expiry"]) == 10 for o in orders)   # exact contracts
    assert (root / "final-cycle" / "data" / "latest.json").exists()
