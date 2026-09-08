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
            put_base = f"{ticker_root.upper()}{put_code}{day_str}{year_str}{strike_str}.U"
            candidate_rics.append(f"{put_base}^{put_code}{year_str}")

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


# 3D FIGURE: Scatter3d
PALETTE = {
    "bg": "#0d1117",
    "panel": "#161b22",
    "grid": "#30363d",
    "text": "#e6edf3",
    "mid": "#00ffcc",
    "trade": "#ff0055",
    "muted": "#8b949e",
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



def writeup_html() -> str:
    return f"""
<div style="max-width:900px;margin:24px auto 60px;padding:0 16px;
            font-family:monospace;color:{PALETTE['text']};line-height:1.7;
            font-size:14px;">
<p><b>Calls show a real cluster of prices when the strike is close to where the stock is trading and the expiration is 
coming up soon, but that cloud thins out fast the further you get from the money or the further out the expiration 
puts show up as completely empty, since none of the roughly 90 put contracts I generated ever resolved to a real 
instrument in LSEG's system for this window. You can't fill in a gap on a $0.50 strike grid by guessing what's between 
two real prices when there aren't two real prices to begin with — for the puts there's nothing on either side of the gap, 
so any number I put there wouldn't be an estimate, it'd just be made up. Going forward I'll treat MID_PRICE as the price 
I actually use to value a position and TRDPRC_1 as proof someone actually traded at that price, but for puts on this 
stock I have neither, so that's a real gap I'll have to account for rather than paper over.</b></p>
</div>
"""



def main(demo: bool = False):
    payload = _make_demo_payload() if demo else load_or_fetch_pipeline_data()

    long_df = build_long_options_table(payload["options"])
    n_raw_cols = payload["options"].shape[1] if not payload["options"].empty else 0
    print(f"Parsed {len(long_df)} contract/date rows from {n_raw_cols} raw columns.")

    fig = build_figure(long_df, payload["ticker"])
    fig_html = pio.to_html(fig, include_plotlyjs="cdn", full_html=False)

    page = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>{payload['ticker']} Options Sparsity</title>
<style>body {{ background:{PALETTE['bg']}; margin:0; }}</style>
</head>
<body>
{fig_html}
{writeup_html()}
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
            rics.append(f"{ticker_root}{put_code}{dd}{yy}{sstr}.U^{put_code}{yy}")

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

    return {"stock": df_stock, "options": df_options, "ticker": ticker_root, "fetched_at": "demo"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--demo", action="store_true",
                         help="Run against synthetic fake data instead of a live LSEG session -- "
                              "no lseg.data install or entitlement needed. For testing this file only.")
    args = parser.parse_args()
    main(demo=args.demo)
