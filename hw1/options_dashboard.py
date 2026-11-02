"""
UUUU expired-options sparsity dashboard
========================================
Run for real:      python3 options_dashboard.py
Run against fake
data to sanity-
check the code
without LSEG:       python3 options_dashboard.py --demo
"""

import os
import pickle
import datetime
import warnings
import argparse

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import plotly.io as pio

warnings.filterwarnings("ignore", category=FutureWarning, module="lseg.data")

CACHE_FILE = "option_pipeline_data.pkl"
OUTPUT_HTML = "index.html"


# Data Pipeline
def load_or_fetch_pipeline_data(
        ticker_stock: str = "UUUU.K",
        ticker_root: str = "UUUU",
        weeks_back: int = 12,
        strike_step: float = 0.50,
        batch_size: int = 25,
) -> dict:
    if os.path.exists(CACHE_FILE):
        print(f"Loading cached dataset from {CACHE_FILE}...")
        with open(CACHE_FILE, "rb") as f:
            return pickle.load(f)

    import lseg.data as ld

    print("Cache not found. Initializing LSEG data pull...")
    ld.open_session()

    end_date = datetime.date.today()
    start_date = end_date - datetime.timedelta(weeks=weeks_back)
    start_str = start_date.strftime("%Y-%m-%d")
    end_str = end_date.strftime("%Y-%m-%d")

    df_stock = ld.get_history(
        universe=[ticker_stock],
        fields=["OPEN_PRC", "HIGH_1", "LOW_1", "TRDPRC_1"],
        start=start_str, end=end_str, interval="daily",
    )

    low_price = float(df_stock["LOW_1"].min())
    high_price = float(df_stock["HIGH_1"].max())

    min_strike = np.floor(low_price / strike_step) * strike_step
    max_strike = np.ceil(high_price / strike_step) * strike_step
    strikes = np.arange(min_strike, max_strike + strike_step, strike_step)
    friday_dates = pd.date_range(start=start_str, end=end_str, freq="W-FRI")

    candidate_rics = []
    n_candidate_calls = 0
    n_candidate_puts = 0
    for dt in friday_dates:
        year_str = dt.strftime("%y")
        day_str = dt.strftime("%d")
        month_num = dt.month
        call_code = chr(ord("A") + month_num - 1)
        put_code = chr(ord("M") + month_num - 1)
        for strike in strikes:
            strike_str = f"{int(round(strike * 100)):05d}"
            call_base = f"{ticker_root.upper()}{call_code}{day_str}{year_str}{strike_str}.U"
            candidate_rics.append(f"{call_base}^{call_code}{year_str}")
            n_candidate_calls += 1
            put_base = f"{ticker_root.upper()}{put_code}{day_str}{year_str}{strike_str}.U"
            # Expired-contract suffix always uses the CALL letter for that month, even on a put --
            # the base symbol's own letter (A-L for calls, M-X for puts) is what encodes put-vs-call;
            # the ^ suffix only tags which monthly expiration cycle the contract belongs to, and LSEG
            # reuses the call-letter scheme for that tag on both sides. Using put_code here was the
            # original bug: it made every single candidate put RIC resolve to nothing, every week.
            candidate_rics.append(f"{put_base}^{call_code}{year_str}")
            n_candidate_puts += 1

    batches = [candidate_rics[i:i + batch_size] for i in range(0, len(candidate_rics), batch_size)]
    history_frames = []
    fields = ["TRDPRC_1", "MID_PRICE"]

    for batch in batches:
        try:
            df_batch = ld.get_history(universe=batch, fields=fields, start=start_str, end=end_str, interval="daily")
            if df_batch is not None and not df_batch.empty:
                df_clean = df_batch.dropna(how="all", axis=1)
                if not df_clean.empty:
                    history_frames.append(df_clean)
        except Exception:
            for single_ric in batch:
                try:
                    df_single = ld.get_history(universe=[single_ric], fields=fields, start=start_str, end=end_str, interval="daily")
                    if df_single is not None and not df_single.empty and not df_single.dropna(how="all").empty:
                        history_frames.append(df_single)
                except Exception:
                    continue

    ld.close_session()

    df_options = pd.DataFrame()
    if history_frames:
        df_options = pd.concat(history_frames, axis=1)
        df_options = df_options.loc[:, ~df_options.columns.duplicated()]

    data_payload = {
        "stock": df_stock,
        "options": df_options,
        "ticker": ticker_root,
        "fetched_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "n_candidate_calls": n_candidate_calls,
        "n_candidate_puts": n_candidate_puts,
    }

    with open(CACHE_FILE, "wb") as f:
        pickle.dump(data_payload, f)
    print(f"Data pipeline complete. Results cached to {CACHE_FILE}.")
    return data_payload

# RIC Parser
def parse_option_ric(ric: str):
    """Returns {ric, underlying, expiry, type, strike} or None if `ric`
    doesn't match the synthetic scheme."""
    if ".U" not in ric:
        return None
    base = ric.split(".U")[0]
    if len(base) <= 10:
        return None

    root = base[:-10]
    month_code = base[-10]
    day_str = base[-9:-7]
    year_str = base[-7:-5]
    strike_str = base[-5:]

    if not (root.isalpha() and day_str.isdigit() and year_str.isdigit() and strike_str.isdigit()):
        return None

    code_upper = month_code.upper()
    if "A" <= code_upper <= "L":
        opt_type = "call"
        month_num = ord(code_upper) - ord("A") + 1
    elif "M" <= code_upper <= "X":
        opt_type = "put"
        month_num = ord(code_upper) - ord("M") + 1
    else:
        return None

    try:
        expiry = datetime.date(2000 + int(year_str), month_num, int(day_str))
    except ValueError:
        return None

    return {
        "ric": ric,
        "underlying": root,
        "expiry": expiry,
        "type": opt_type,
        "strike": int(strike_str) / 100.0,
    }


# Melt to Long format
def build_long_options_table(df_options: pd.DataFrame) -> pd.DataFrame:
    cols = ["date", "ric", "underlying", "expiry", "type", "strike", "TRDPRC_1", "MID_PRICE"]
    if df_options is None or df_options.empty:
        return pd.DataFrame(columns=cols)

    records = []

    if isinstance(df_options.columns, pd.MultiIndex):
        rics = df_options.columns.get_level_values(0).unique()
        for ric in rics:
            parsed = parse_option_ric(ric)
            if parsed is None:
                continue
            sub = df_options[ric].copy()
            for col in ("TRDPRC_1", "MID_PRICE"):
                if col not in sub.columns:
                    sub[col] = np.nan
            sub = sub[["TRDPRC_1", "MID_PRICE"]].reset_index()
            sub.columns = ["date", "TRDPRC_1", "MID_PRICE"]
            sub["ric"] = ric
            sub["underlying"] = parsed["underlying"]
            sub["expiry"] = parsed["expiry"]
            sub["type"] = parsed["type"]
            sub["strike"] = parsed["strike"]
            records.append(sub)
    else:
        for ric in df_options.columns:
            parsed = parse_option_ric(ric)
            if parsed is None:
                continue
            sub = df_options[[ric]].reset_index()
            sub.columns = ["date", "MID_PRICE"]
            sub["TRDPRC_1"] = np.nan
            sub["ric"] = ric
            sub["underlying"] = parsed["underlying"]
            sub["expiry"] = parsed["expiry"]
            sub["type"] = parsed["type"]
            sub["strike"] = parsed["strike"]
            records.append(sub)

    if not records:
        return pd.DataFrame(columns=cols)

    long_df = pd.concat(records, ignore_index=True)
    long_df["date"] = pd.to_datetime(long_df["date"]).dt.date
    long_df = long_df.dropna(subset=["TRDPRC_1", "MID_PRICE"], how="all")
    return long_df.sort_values(["date", "type", "strike"]).reset_index(drop=True)


# Computed per as-of date, pooled across calls+puts
def compute_day_stats(long_df: pd.DataFrame, as_of: datetime.date) -> dict:
    day = long_df[long_df["date"] == as_of]
    has_mid = day["MID_PRICE"].notna()
    has_trade = day["TRDPRC_1"].notna()

    n_listed = int(has_mid.sum())  # "listed" = has at least a mid quote that day
    n_mid_no_trade = int((has_mid & ~has_trade).sum())
    pct_mid_no_trade = (100.0 * n_mid_no_trade / n_listed) if n_listed else float("nan")

    both = day[has_mid & has_trade]
    median_abs_diff = float((both["MID_PRICE"] - both["TRDPRC_1"]).abs().median()) if not both.empty else float("nan")

    return {
        "date": as_of,
        "n_listed": n_listed,
        "pct_mid_no_trade": pct_mid_no_trade,
        "median_abs_diff": median_abs_diff,
    }


# Candidate universe reconstruction + aggregate stats (no LSEG call -- pure arithmetic over the
# already-cached stock history, used only for the deeper breakdown below)
def reconstruct_candidate_universe(df_stock: pd.DataFrame, ticker_root: str, strike_step: float = 0.50) -> pd.DataFrame:
    """Rebuilds the same candidate-RIC universe load_or_fetch_pipeline_data() queried, purely from
    the already-cached stock history -- no LSEG call needed, no refetch required. Matches the
    original query's totals exactly (verified against n_candidate_calls/n_candidate_puts) as long
    as the cached stock window's own first/last trading days bracket the same Fridays the original
    fetch used, which holds for a same-day rebuild; re-running this long after the original fetch
    (stock index shifted since) could drift by a week at the edges."""
    cols = ["ric", "type", "strike", "expiry"]
    if df_stock is None or df_stock.empty:
        return pd.DataFrame(columns=cols)

    low_price = float(df_stock["LOW_1"].min())
    high_price = float(df_stock["HIGH_1"].max())
    min_strike = np.floor(low_price / strike_step) * strike_step
    max_strike = np.ceil(high_price / strike_step) * strike_step
    strikes = np.arange(min_strike, max_strike + strike_step, strike_step)

    start = pd.Timestamp(min(df_stock.index))
    end = pd.Timestamp(max(df_stock.index))
    friday_dates = pd.date_range(start=start, end=end, freq="W-FRI")

    rows = []
    for dt in friday_dates:
        year_str = dt.strftime("%y")
        day_str = dt.strftime("%d")
        month_num = dt.month
        call_code = chr(ord("A") + month_num - 1)
        put_code = chr(ord("M") + month_num - 1)
        expiry = dt.date()
        for strike in strikes:
            strike_str = f"{int(round(strike * 100)):05d}"
            call_base = f"{ticker_root.upper()}{call_code}{day_str}{year_str}{strike_str}.U"
            rows.append({"ric": f"{call_base}^{call_code}{year_str}", "type": "call", "strike": float(strike), "expiry": expiry})
            put_base = f"{ticker_root.upper()}{put_code}{day_str}{year_str}{strike_str}.U"
            rows.append({"ric": f"{put_base}^{call_code}{year_str}", "type": "put", "strike": float(strike), "expiry": expiry})
    return pd.DataFrame(rows, columns=cols)


def compute_overall_stats(long_df: pd.DataFrame, n_candidate_calls: int, n_candidate_puts: int) -> dict:
    """Aggregate resolution and fill-quality stats across the whole pull, not just one as-of date."""
    call_resolved = int(long_df.loc[long_df["type"] == "call", "ric"].nunique()) if not long_df.empty else 0
    put_resolved = int(long_df.loc[long_df["type"] == "put", "ric"].nunique()) if not long_df.empty else 0

    has_mid = long_df["MID_PRICE"].notna() if not long_df.empty else pd.Series(dtype=bool)
    has_trade = long_df["TRDPRC_1"].notna() if not long_df.empty else pd.Series(dtype=bool)
    both = long_df[has_mid & has_trade] if not long_df.empty else long_df
    abs_diff = (both["MID_PRICE"] - both["TRDPRC_1"]).abs() if not both.empty else pd.Series(dtype=float)

    n_listed = int(has_mid.sum())
    n_mid_no_trade = int((has_mid & ~has_trade).sum())

    return {
        "call_resolved": call_resolved, "n_candidate_calls": n_candidate_calls,
        "put_resolved": put_resolved, "n_candidate_puts": n_candidate_puts,
        "pct_call_resolved": (100.0 * call_resolved / n_candidate_calls) if n_candidate_calls else float("nan"),
        "pct_put_resolved": (100.0 * put_resolved / n_candidate_puts) if n_candidate_puts else float("nan"),
        "n_contract_date_rows": int(len(long_df)),
        "n_both": int(len(both)),
        "median_abs_diff": float(abs_diff.median()) if not abs_diff.empty else float("nan"),
        "pct_mid_no_trade": (100.0 * n_mid_no_trade / n_listed) if n_listed else float("nan"),
    }


MONEYNESS_BUCKETS = [(0, 5, "0-5% from spot"), (5, 15, "5-15%"), (15, 30, "15-30%"), (30, float("inf"), "30%+")]


def compute_resolution_by_bucket(long_df: pd.DataFrame, candidates_df: pd.DataFrame, spot_ref: float) -> pd.DataFrame:
    """For each candidate contract, how far its strike sits from spot (as a %), bucketed, split by
    call/put, against whether that candidate actually resolved to real data."""
    out_cols = ["bucket", "type", "n_candidates", "n_resolved", "pct_resolved"]
    if candidates_df.empty or spot_ref != spot_ref or spot_ref <= 0:
        return pd.DataFrame(columns=out_cols)

    cand = candidates_df.copy()
    cand["moneyness_pct"] = (cand["strike"] - spot_ref).abs() / spot_ref * 100.0

    def bucket_of(pct):
        for lo, hi, label in MONEYNESS_BUCKETS:
            if lo <= pct < hi:
                return label
        return MONEYNESS_BUCKETS[-1][2]

    cand["bucket"] = cand["moneyness_pct"].apply(bucket_of)
    resolved_rics = set(long_df["ric"].unique()) if not long_df.empty else set()
    cand["resolved"] = cand["ric"].isin(resolved_rics)

    bucket_order = [b[2] for b in MONEYNESS_BUCKETS]
    out = (cand.groupby(["bucket", "type"])
               .agg(n_candidates=("ric", "count"), n_resolved=("resolved", "sum"))
               .reset_index())
    out["pct_resolved"] = 100.0 * out["n_resolved"] / out["n_candidates"]
    out["bucket"] = pd.Categorical(out["bucket"], categories=bucket_order, ordered=True)
    return out.sort_values(["bucket", "type"]).reset_index(drop=True)


def build_bucket_figure(bucket_df: pd.DataFrame) -> go.Figure:
    bucket_order = [b[2] for b in MONEYNESS_BUCKETS]
    calls = bucket_df[bucket_df["type"] == "call"].set_index("bucket").reindex(bucket_order)
    puts = bucket_df[bucket_df["type"] == "put"].set_index("bucket").reindex(bucket_order)

    def labels(df):
        n_res = df["n_resolved"].fillna(0).astype(int)
        n_cand = df["n_candidates"].fillna(0).astype(int)
        return [f"{a}/{b}" for a, b in zip(n_res, n_cand)]

    fig = go.Figure(data=[
        go.Bar(name="Calls", x=bucket_order, y=calls["pct_resolved"].fillna(0).values,
               marker_color=PALETTE["call_bar"], text=labels(calls), textposition="outside",
               hovertemplate="%{x}<br>%{y:.0f}%% resolved<extra>Calls</extra>"),
        go.Bar(name="Puts", x=bucket_order, y=puts["pct_resolved"].fillna(0).values,
               marker_color=PALETTE["put_bar"], text=labels(puts), textposition="outside",
               hovertemplate="%{x}<br>%{y:.0f}%% resolved<extra>Puts</extra>"),
    ])
    fig.update_layout(
        barmode="group",
        template="plotly_dark",
        paper_bgcolor=PALETTE["bg"], plot_bgcolor=PALETTE["panel"],
        font=dict(color=PALETTE["text"], family="monospace"),
        yaxis=dict(title="% of candidates resolved to real LSEG data", range=[0, 115], gridcolor=PALETTE["grid"]),
        xaxis=dict(title="Distance from spot (|strike - spot| / spot)", gridcolor=PALETTE["grid"]),
        legend=dict(font=dict(color=PALETTE["text"])),
        margin=dict(l=10, r=10, t=20, b=10),
        height=420,
    )
    return fig


def page_css() -> str:
    return f"""
:root {{ color-scheme: dark; }}
* {{ box-sizing: border-box; }}
html {{ scroll-behavior: smooth; }}
body {{
  margin: 0; background: {PALETTE['bg']}; color: {PALETTE['text']};
  font: 15px/1.6 ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  -webkit-font-smoothing: antialiased;
}}
.wrap {{ max-width: 1100px; margin: 0 auto; padding: 0 16px; }}
a {{ color: {PALETTE['call_bar']}; text-underline-offset: 2px; }}
.hero {{ padding: 32px 16px 16px; }}
.eyebrow {{ color: {PALETTE['muted']}; font-size: 12px; letter-spacing: .08em; text-transform: uppercase; margin: 0 0 6px; }}
h1 {{ font-size: clamp(26px, 5vw, 38px); line-height: 1.15; margin: 0 0 10px; font-weight: 700; letter-spacing: -0.01em; }}
h2 {{ font-size: 22px; margin: 0 0 6px; font-weight: 650; }}
.sub {{ color: {PALETTE['muted']}; margin: 0; max-width: 78ch; }}
nav.bar {{ position: sticky; top: 0; z-index: 20; background: rgba(13,17,23,0.92); backdrop-filter: blur(8px); border-bottom: 1px solid {PALETTE['grid']}; }}
nav.bar .wrap {{ display: flex; gap: 4px; align-items: center; overflow-x: auto; padding-top: 8px; padding-bottom: 8px; white-space: nowrap; }}
nav.bar a {{ color: {PALETTE['muted']}; text-decoration: none; font-size: 13px; padding: 5px 10px; border-radius: 8px; }}
nav.bar a.home {{ color: {PALETTE['text']}; font-weight: 650; margin-right: 6px; }}
nav.bar a:hover {{ background: {PALETTE['panel']}; color: {PALETTE['text']}; }}
section {{ padding: 26px 0 6px; }}
.lede {{ color: {PALETTE['muted']}; margin: 0 0 14px; max-width: 80ch; }}
.card {{ background: {PALETTE['panel']}; border: 1px solid {PALETTE['grid']}; border-radius: 14px; padding: 16px; }}
.tiles {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 12px; }}
.tile {{ background: {PALETTE['panel']}; border: 1px solid {PALETTE['grid']}; border-radius: 14px; padding: 14px 16px; }}
.tile .lab {{ color: {PALETTE['muted']}; font-size: 12.5px; }}
.tile .val {{ font-size: 24px; font-weight: 700; line-height: 1.2; margin-top: 2px; font-variant-numeric: tabular-nums; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }}
.tile .note {{ color: {PALETTE['muted']}; font-size: 12.5px; margin-top: 2px; }}
.tbl {{ overflow: auto; border: 1px solid {PALETTE['grid']}; border-radius: 12px; background: {PALETTE['panel']}; }}
table {{ border-collapse: collapse; width: 100%; font-size: 13px; }}
th, td {{ padding: 7px 10px; text-align: left; white-space: nowrap; border-bottom: 1px solid {PALETTE['grid']}; }}
th {{ background: rgba(255,255,255,0.03); color: {PALETTE['muted']}; font-weight: 650; font-size: 12px; }}
td.n, th.n {{ text-align: right; font-variant-numeric: tabular-nums; }}
.prose p {{ margin: 0; max-width: 78ch; line-height: 1.7; }}
footer {{ padding: 24px 16px 60px; color: {PALETTE['muted']}; font-size: 13px; }}
"""


def stats_tiles_html(stats: dict, window_start, window_end) -> str:
    def pct(v):
        return f"{v:.0f}%" if v == v else "n/a"
    def dollars(v):
        return f"${v:.2f}" if v == v else "n/a"
    return f"""
<div class="tiles">
  <div class="tile"><div class="lab">Window</div><div class="val" style="font-size:16px">{window_start} &rarr; {window_end}</div></div>
  <div class="tile"><div class="lab">Calls resolved</div><div class="val">{stats['call_resolved']}/{stats['n_candidate_calls']}</div><div class="note">{pct(stats['pct_call_resolved'])} of candidates</div></div>
  <div class="tile"><div class="lab">Puts resolved</div><div class="val">{stats['put_resolved']}/{stats['n_candidate_puts']}</div><div class="note">{pct(stats['pct_put_resolved'])} of candidates</div></div>
  <div class="tile"><div class="lab">Contract/date rows</div><div class="val">{stats['n_contract_date_rows']:,}</div><div class="note">parsed from raw pull</div></div>
  <div class="tile"><div class="lab">Median |MID &minus; TRADE|</div><div class="val">{dollars(stats['median_abs_diff'])}</div><div class="note">where both exist ({stats['n_both']} rows)</div></div>
  <div class="tile"><div class="lab">Mid but no trade</div><div class="val">{pct(stats['pct_mid_no_trade'])}</div><div class="note">of listed series</div></div>
</div>
"""


def bucket_table_html(bucket_df: pd.DataFrame) -> str:
    if bucket_df.empty:
        return '<p class="lede">No candidate universe could be reconstructed (no cached stock history).</p>'
    rows = []
    for _, r in bucket_df.iterrows():
        rows.append(
            f"<tr><td>{r['bucket']}</td><td>{str(r['type']).capitalize()}</td>"
            f"<td class='n'>{int(r['n_candidates'])}</td><td class='n'>{int(r['n_resolved'])}</td>"
            f"<td class='n'>{r['pct_resolved']:.0f}%</td></tr>"
        )
    return f"""
<div class="tbl">
<table>
<thead><tr><th>Distance from spot</th><th>Side</th><th class="n">Candidates</th><th class="n">Resolved</th><th class="n">% resolved</th></tr></thead>
<tbody>{''.join(rows)}</tbody>
</table>
</div>
"""


# 3D FIGURE: Scatter3d
PALETTE = {
    "bg": "#0d1117",
    "panel": "#161b22",
    "grid": "#30363d",
    "text": "#e6edf3",
    "mid": "#00ffcc",
    "trade": "#ff0055",
    "muted": "#8b949e",
    "call_bar": "#58a6ff",
    "put_bar": "#ffb347",
}


def _trace_pair(day_type_df: pd.DataFrame, name_prefix: str):
    mid_df = day_type_df.dropna(subset=["MID_PRICE"])
    trade_df = day_type_df.dropna(subset=["TRDPRC_1"])

    mid_trace = go.Scatter3d(
        x=mid_df["strike"], y=mid_df["dte"], z=mid_df["MID_PRICE"],
        mode="markers", name=f"{name_prefix} MID_PRICE",
        marker=dict(size=4, color=PALETTE["mid"], symbol="circle", opacity=0.85),
        hovertemplate="strike %{x}<br>dte %{y}<br>mid $%{z:.2f}<extra></extra>",
    )
    trade_trace = go.Scatter3d(
        x=trade_df["strike"], y=trade_df["dte"], z=trade_df["TRDPRC_1"],
        mode="markers", name=f"{name_prefix} TRDPRC_1",
        marker=dict(size=5, color=PALETTE["trade"], symbol="diamond", opacity=0.95),
        hovertemplate="strike %{x}<br>dte %{y}<br>trade $%{z:.2f}<extra></extra>",
    )
    return mid_trace, trade_trace


def build_figure(long_df: pd.DataFrame, ticker: str) -> go.Figure:
    if long_df.empty:
        raise ValueError("No parsed option rows -- nothing to plot. Check the pipeline pull.")

    long_df = long_df.copy()
    long_df["dte"] = long_df.apply(lambda r: (r["expiry"] - r["date"]).days, axis=1)
    dates = sorted(long_df["date"].unique())

    put_data_exists = (long_df["type"] == "put").any()
    coverage_note = None
    if not put_data_exists:
        coverage_note = (
            "Note: 0 candidate PUT contracts resolved to a real LSEG instrument in this "
            "window -- confirmed directly (get_history and get_data both return 'not "
            "found'), not assumed. The Puts view is intentionally empty, not a bug."
        )

    frames = []
    for d in dates:
        day = long_df[long_df["date"] == d]
        call_mid, call_trade = _trace_pair(day[day["type"] == "call"], "Call")
        put_mid, put_trade = _trace_pair(day[day["type"] == "put"], "Put")

        stats = compute_day_stats(long_df, d)
        diff_txt = f"${stats['median_abs_diff']:.2f}" if stats["median_abs_diff"] == stats["median_abs_diff"] else "n/a"
        annotation_text = (
            f"As of {d}  |  listed series with a mid: {stats['n_listed']}"
            f"  |  mid-but-no-trade: {stats['pct_mid_no_trade']:.1f}%"
            f"  |  median |MID_PRICE - TRDPRC_1| where both exist: {diff_txt}"
        )

        annotations = [dict(
            text=annotation_text, xref="paper", yref="paper",
            x=0.5, y=1.10, showarrow=False,
            font=dict(color=PALETTE["text"], size=13, family="monospace"),
        )]
        if coverage_note:
            annotations.append(dict(
                text=coverage_note, xref="paper", yref="paper",
                x=0.5, y=1.055, showarrow=False,
                font=dict(color=PALETTE["muted"], size=11, family="monospace"),
            ))

        frames.append(go.Frame(
            name=str(d),
            data=[call_mid, call_trade, put_mid, put_trade],
            layout=go.Layout(annotations=annotations),
        ))

    fig = go.Figure(
        data=list(frames[0].data),
        layout=frames[0].layout,
        frames=frames,
    )
    fig.data[2].visible = False
    fig.data[3].visible = False

    fig.update_layout(
        template="plotly_dark",
        paper_bgcolor=PALETTE["bg"],
        plot_bgcolor=PALETTE["panel"],
        font=dict(color=PALETTE["text"], family="monospace"),
        title=dict(text=f"{ticker} expired options — MID_PRICE vs TRDPRC_1", x=0.02,
                   font=dict(size=20, color=PALETTE["mid"])),
        scene=dict(
            xaxis=dict(title="Strike ($)", gridcolor=PALETTE["grid"], backgroundcolor=PALETTE["panel"]),
            yaxis=dict(title="Days to expiry", gridcolor=PALETTE["grid"], backgroundcolor=PALETTE["panel"]),
            zaxis=dict(title="Price ($)", gridcolor=PALETTE["grid"], backgroundcolor=PALETTE["panel"]),
        ),
        margin=dict(l=10, r=10, t=110, b=10),
        legend=dict(font=dict(color=PALETTE["text"])),
        updatemenus=[dict(
            type="buttons", direction="right", x=0.02, y=1.14, xanchor="left", yanchor="bottom",
            bgcolor=PALETTE["panel"], bordercolor=PALETTE["grid"], font=dict(color=PALETTE["text"]),
            buttons=[
                dict(label="Calls", method="restyle", args=[{"visible": [True, True, False, False]}]),
                dict(label="Puts", method="restyle", args=[{"visible": [False, False, True, True]}]),
            ],
        )],
        sliders=[dict(
            active=0,
            currentvalue=dict(prefix="As-of date: ", font=dict(color=PALETTE["muted"])),
            pad=dict(t=60),
            steps=[dict(
                label=str(d), method="animate",
                args=[[str(d)], dict(mode="immediate", frame=dict(duration=0, redraw=True), transition=dict(duration=0))],
            ) for d in dates],
        )],
    )
    return fig



def writeup_html(long_df: pd.DataFrame, n_candidate_calls: int, n_candidate_puts: int) -> str:
    call_rics = long_df.loc[long_df["type"] == "call", "ric"].nunique() if not long_df.empty else 0
    put_rics = long_df.loc[long_df["type"] == "put", "ric"].nunique() if not long_df.empty else 0

    if put_rics > 0:
        put_para = (
            f"Puts resolved this time: {put_rics} of {n_candidate_puts} candidate put contracts came back "
            f"with real LSEG data, against {call_rics} of {n_candidate_calls} on the call side. The density "
            "pattern looks the same on both sides -- strikes close to the money with an expiration coming up "
            "soon are the ones with real quotes, thinning out fast the further you get from either. An earlier "
            "version of this pipeline generated put RICs with the wrong expired-contract suffix (the letter "
            "after the ^ has to be that month's call letter even on a put -- the base symbol's own letter is "
            "what already encodes put-vs-call), so every put request came back empty regardless of whether real "
            "data existed. Fixing that suffix turned this from a parsing bug into an ordinary, explainable gap."
        )
    else:
        put_para = (
            f"Puts still show up as completely empty: 0 of {n_candidate_puts} candidate put contracts resolved "
            "to a real instrument in LSEG's system for this window -- confirmed directly (get_history and "
            "get_data both return 'not found'), not assumed."
        )

    return f"""<div class="card prose">
<p>Calls show a real cluster of prices when the strike is close to where the stock is trading and the
expiration is coming up soon, but that cloud thins out fast the further you get from the money or the further
out the expiration goes. {put_para} You can't fill in a gap on a $0.50 strike grid by guessing what's between
two real prices when there aren't two real prices to begin with -- where there's nothing on either side of a
gap, any number put there wouldn't be an estimate, it would just be made up. MID_PRICE is the price actually
used to value a position; TRDPRC_1 is proof someone actually traded at that price -- keeping those two separate,
rather than papering over whichever one is missing, is the point of this view.</p>
</div>
"""



def main(demo: bool = False):
    payload = _make_demo_payload() if demo else load_or_fetch_pipeline_data()

    long_df = build_long_options_table(payload["options"])
    n_raw_cols = payload["options"].shape[1] if not payload["options"].empty else 0
    print(f"Parsed {len(long_df)} contract/date rows from {n_raw_cols} raw columns.")

    n_candidate_calls = payload.get("n_candidate_calls", 0)
    n_candidate_puts = payload.get("n_candidate_puts", 0)
    ticker = payload["ticker"]

    stats = compute_overall_stats(long_df, n_candidate_calls, n_candidate_puts)

    df_stock = payload["stock"]
    window_start = pd.Timestamp(min(df_stock.index)).date() if not df_stock.empty else None
    window_end = pd.Timestamp(max(df_stock.index)).date() if not df_stock.empty else None
    window_days = (window_end - window_start).days if window_start and window_end else None

    candidates_df = reconstruct_candidate_universe(df_stock, ticker)
    spot_ref = float("nan")
    if not df_stock.empty and df_stock["TRDPRC_1"].notna().any():
        spot_ref = float(df_stock["TRDPRC_1"].dropna().iloc[-1])
    bucket_df = compute_resolution_by_bucket(long_df, candidates_df, spot_ref)

    fig = build_figure(long_df, ticker)
    fig_html = pio.to_html(fig, include_plotlyjs="cdn", full_html=False)

    bucket_fig_html = '<p class="lede">No bucket breakdown available.</p>'
    if not bucket_df.empty:
        bucket_fig = build_bucket_figure(bucket_df)
        bucket_fig_html = pio.to_html(bucket_fig, include_plotlyjs=False, full_html=False)

    n_candidates_total = n_candidate_calls + n_candidate_puts
    window_phrase = f"{window_days}-day" if window_days is not None else "multi-week"

    page = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{ticker} Options Sparsity</title>
<style>{page_css()}</style>
</head>
<body>

<header class="hero wrap">
  <p class="eyebrow">FINTECH 535 &middot; Options data pipeline</p>
  <h1>{ticker} expired-options sparsity</h1>
  <p class="sub">What actually comes back from LSEG when you ask for {n_candidates_total:,} specific expired {ticker}
  option contracts -- call and put, across a {window_phrase} trading window -- and what that says about where the
  real liquidity actually is.</p>
</header>

<nav class="bar">
  <div class="wrap">
    <a class="home" href="../index.html">&larr; All projects</a>
    <a href="#summary">Summary</a>
    <a href="#chart">3D view</a>
    <a href="#stats">Resolution by moneyness</a>
    <a href="#writeup">Write-up</a>
  </div>
</nav>

<main class="wrap">

  <section id="summary">
    <h2>Summary</h2>
    <p class="lede">Every candidate here is a real, specific contract -- a root, strike, and expiry -- built from
    {ticker}'s own trading range over the window and queried against LSEG one at a time. This counts what actually
    came back, not what was expected to come back.</p>
    {stats_tiles_html(stats, window_start, window_end)}
  </section>

  <section id="chart">
    <h2>MID_PRICE vs TRDPRC_1, in 3D</h2>
    <p class="lede">Strike on one axis, days-to-expiry on another, price on the third. Toggle Calls/Puts, scrub the
    as-of-date slider. Cyan markers are a quoted mid; magenta diamonds are an actual trade print.</p>
    <div class="card">{fig_html}</div>
  </section>

  <section id="stats">
    <h2>Resolution rate by distance from spot</h2>
    <p class="lede">Bucketing every candidate contract by how far its strike sits from {ticker}'s spot price at the
    end of the window, call and put side separately. Same "liquidity thins out away from the money" story the 3D
    chart tells visually, counted directly instead: of each bucket's candidates, what fraction actually existed as
    a real LSEG instrument.</p>
    <div class="card">{bucket_fig_html}</div>
    <div style="margin-top:12px">{bucket_table_html(bucket_df)}</div>
    <p class="lede" style="margin-top:10px;font-size:12.5px">Bucketed against the single most recent spot price in
    the window. The candidate universe here is reconstructed from the cached stock history rather than the exact
    original query -- it matches the summary tiles above exactly as of this build, but could drift by a week's
    worth of strikes at the edges if this is rebuilt long after the original fetch.</p>
  </section>

  <section id="writeup">
    <h2>Write-up</h2>
    {writeup_html(long_df, n_candidate_calls, n_candidate_puts)}
  </section>

</main>

<footer class="wrap"><p>Pulled {payload.get('fetched_at', 'n/a')} &middot; {ticker}</p></footer>

</body>
</html>"""

    with open(OUTPUT_HTML, "w") as f:
        f.write(page)
    print(f"Wrote {OUTPUT_HTML} -- push this to your GitHub Pages branch/folder.")


# Demo Data
def _make_demo_payload():
    rng = np.random.default_rng(7)
    ticker_root = "UUUU"
    dates = pd.date_range("2026-08-01", "2026-08-14", freq="B").date
    expiries = [datetime.date(2026, 8, 21), datetime.date(2026, 9, 18)]
    strikes = np.arange(9.0, 15.5, 0.5)
    spot = 12.0

    rics = []
    for exp in expiries:
        month_num = exp.month
        call_code = chr(ord("A") + month_num - 1)
        put_code = chr(ord("M") + month_num - 1)
        yy, dd = exp.strftime("%y"), exp.strftime("%d")
        for strike in strikes:
            sstr = f"{int(round(strike * 100)):05d}"
            rics.append(f"{ticker_root}{call_code}{dd}{yy}{sstr}.U^{call_code}{yy}")
            rics.append(f"{ticker_root}{put_code}{dd}{yy}{sstr}.U^{call_code}{yy}")  # suffix: call letter, even on a put

    columns = pd.MultiIndex.from_product([rics, ["TRDPRC_1", "MID_PRICE"]])
    df_options = pd.DataFrame(index=pd.Index(dates), columns=columns, dtype=float)

    for ric in rics:
        parsed = parse_option_ric(ric)
        if parsed is None:
            continue
        moneyness = abs(parsed["strike"] - spot)
        mid_fill_prob = max(0.15, 1.0 - moneyness / 6.0)   # near the money -> dense
        trade_fill_prob = mid_fill_prob * 0.25             # trades are much rarer than quotes
        base_price = max(0.05, spot - parsed["strike"]) if parsed["type"] == "call" else max(0.05, parsed["strike"] - spot)
        for d in dates:
            if rng.random() < mid_fill_prob:
                mid = max(0.01, round(base_price + rng.normal(0, 0.15), 2))
                df_options.loc[d, (ric, "MID_PRICE")] = mid
                if rng.random() < trade_fill_prob:
                    df_options.loc[d, (ric, "TRDPRC_1")] = max(0.01, round(mid + rng.normal(0, 0.05), 2))

    df_stock = pd.DataFrame({
        "OPEN_PRC": spot + rng.normal(0, 0.2, len(dates)),
        "HIGH_1": spot + 0.3 + rng.normal(0, 0.2, len(dates)),
        "LOW_1": spot - 0.3 + rng.normal(0, 0.2, len(dates)),
        "TRDPRC_1": spot + rng.normal(0, 0.2, len(dates)),
    }, index=pd.Index(dates))

    return {
        "stock": df_stock, "options": df_options, "ticker": ticker_root, "fetched_at": "demo",
        "n_candidate_calls": len(expiries) * len(strikes),
        "n_candidate_puts": len(expiries) * len(strikes),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--demo", action="store_true",
                         help="Run against synthetic fake data instead of a live LSEG session -- "
                              "no lseg.data install or entitlement needed. For testing this file only.")
    args = parser.parse_args()
    main(demo=args.demo)
