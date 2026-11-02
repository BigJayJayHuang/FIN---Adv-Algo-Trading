/* The Wheel - renders everything from window.WHEEL_BOOK (book3.js). No libraries, no network. */
(function () {
  "use strict";
  const B = window.WHEEL_BOOK;
  if (!B) {
    document.body.textContent = "book3.js is missing. Run: python3 build_book3.py";
    return;
  }
  const M = B.meta, P = B.params, S = B.summary;
  const L = B.ledger, BL = B.blotter;
  const allWeeks = B.weeks, weeks = allWeeks.filter(w => !w.skip);
  const skips = B.skips;

  /* ---------------------------------------------------------------- helpers */
  const $ = (s, root) => (root || document).querySelector(s);
  const NS = "http://www.w3.org/2000/svg";
  function el(tag, attrs, ...kids) {
    const n = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (k === "class") n.className = v;
      else if (k === "text") n.textContent = v;
      else n.setAttribute(k, v);
    }
    for (const c of kids.flat()) if (c != null) n.append(c.nodeType ? c : document.createTextNode(String(c)));
    return n;
  }
  function sv(tag, attrs, ...kids) {
    const n = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs || {})) n.setAttribute(k, v);
    for (const c of kids.flat()) if (c != null) n.append(c.nodeType ? c : document.createTextNode(String(c)));
    return n;
  }
  const usd = (x, d = 2) => x == null ? "—" : (x < 0 ? "−" : "") + "$" + Math.abs(x).toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
  const num = (x, d = 2) => x == null ? "—" : x.toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
  const pct = (x, d = 2) => x == null ? "—" : (x > 0 ? "+" : x < 0 ? "−" : "") + Math.abs(x).toFixed(d) + "%";
  const md = (iso) => { const [, m, d] = iso.slice(0, 10).split("-"); return ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"][+m - 1] + " " + (+d); };
  const mondayOf = (d) => { const t = new Date(d + "T12:00:00Z"); t.setUTCDate(t.getUTCDate() - ((t.getUTCDay() + 6) % 7)); return t.toISOString().slice(0, 10); };
  const chip = (s) => el("span", { class: "chip " + s, text: s });
  const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  const maxDD = (xs) => { let peak = xs[0], worst = 0; for (const v of xs) { peak = Math.max(peak, v); worst = Math.min(worst, (v / peak - 1) * 100); } return worst; };
  // Weeks rows carry a raw ISO timestamp (entry / earlyTs), not a pre-formatted label like the
  // ledger/blotter rows do. This reproduces build_book3.py's lbl(ts) ("%a %b %d %H:%M", leading
  // zero stripped) straight from the ISO string's own wall-clock digits, so it never gets
  // reinterpreted through the viewer's browser timezone.
  function lblIso(s) {
    if (!s) return null;
    const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/.exec(s);
    if (!m) return s;
    const Y = +m[1], Mo = +m[2], D = +m[3], H = +m[4], Mi = +m[5];
    const days = ["Sun","Mon","Tue","Wed","Thu","Fri","Sat"];
    const months = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
    const wd = days[new Date(Date.UTC(Y, Mo - 1, D)).getUTCDay()];
    return `${wd} ${months[Mo - 1]} ${D} ${H}:${String(Mi).padStart(2, "0")}`;
  }
  const sideWord = (s) => s === "P" ? "Put" : s === "C" ? "Call" : "—";
  const otmPct = (strike, spot) => spot ? ` (${pct((strike / spot - 1) * 100)})` : "";

  function table(host, cols, rows, rowClass) {
    host.textContent = "";
    const thead = el("thead", {}, el("tr", {}, cols.map(c => el("th", { class: c.cls || "", text: c.h }))));
    const tbody = el("tbody");
    for (const r of rows) {
      const tr = el("tr", { class: rowClass ? rowClass(r) || "" : "" });
      for (const c of cols) tr.append(el("td", { class: c.cls || "" }, c.f(r)));
      tbody.append(tr);
    }
    host.append(el("table", {}, thead, tbody));
  }

  /* --------------------------------------------------------------- headline */
  const first = L[0], last = L[L.length - 1];
  $("#title").textContent = `${M.ticker} - the wheel`;
  $("#subtitle").textContent = `Cash-secured puts while flat, covered calls while holding: puts struck off a 3-tier trailing realized-vol regime, calls struck off spot vs. cost basis, either leg bought back early at ${P.early_close_mult}x its own entry premium. ${md(first.d)} to ${md(last.d)}, ${first.d.slice(0, 4)}; ${M.interval} bars; compared against plain buy-and-hold on the same stock.`;
  if (M.demo) $("#demo-banner").hidden = false;
  document.title = "The Wheel";

  /* ---------------------------------------------------------------- summary */
  const beatReturn = S.ret > S.holdRet;
  const beatSharpe = (S.sharpe != null && S.holdSharpe != null) ? S.sharpe > S.holdSharpe : null;
  $("#summary-lede").textContent =
    `Starting from ${usd(S.startCash, 0)}, the wheel ended at ${usd(S.endNav, 0)} (${pct(S.ret)}) against buy-and-hold's ${pct(S.holdRet)} on the same stock and the same starting dollars. ` +
    `${beatSharpe == null ? "Not enough history yet for a Sharpe comparison." : beatSharpe ? "It did better per unit of risk: a higher Sharpe ratio than just holding the stock." : "It did not improve on buy-and-hold's Sharpe ratio over this particular window."}`;
  const tiles = [
    { cls: "hero-t", lab: "Ending NAV / return", val: pct(S.ret), note: `${usd(S.startCash, 0)} → ${usd(S.endNav, 0)}` },
    { lab: "Buy & hold return", val: pct(S.holdRet), note: "same stock, same entry day, no options" },
    { lab: "Net premium collected", val: usd(S.premium, 0), note: "sales minus early-close buybacks" },
    { lab: "Trades", val: `${S.puts} puts · ${S.calls} calls`, note: `${S.assigned} assigned · ${S.calledAway} called away` },
    { lab: "Outcomes", val: `${S.expired} expired · ${S.closedEarly} closed early`, note: `${S.skipped} skipped of ${S.weeks} weeks` },
    { lab: "Sharpe (wheel vs hold)", val: `${S.sharpe == null ? "—" : S.sharpe.toFixed(2)} / ${S.holdSharpe == null ? "—" : S.holdSharpe.toFixed(2)}`, note: `vol ${S.vol == null ? "—" : S.vol.toFixed(1) + "%"} vs ${S.holdVol == null ? "—" : S.holdVol.toFixed(1) + "%"}` },
  ];
  $("#tiles").append(...tiles.map(t => el("div", { class: "card tile " + (t.cls || "") },
    el("div", { class: "lab", text: t.lab }), el("div", { class: "val", text: t.val }), el("div", { class: "note", text: t.note }))));

  /* ---------------------------------------------------------------- write-up */
  const putOtmTxt = Object.entries(P.put_otm).map(([k, v]) => `${k} ${(v * 100).toFixed(1)}%`).join(", ");
  const callOtmTxt = Object.entries(P.call_otm).map(([k, v]) => `${k.replace("_", " ")} ${(v * 100).toFixed(1)}%`).join(", ");
  const wb = $("#writeup-body");
  wb.innerHTML = `
    <h3 style="margin-top:0">Thesis</h3>
    <p>Selling a cash-secured put while flat and a covered call while holding collects option premium every single week, in either direction, in exchange for giving up some upside and taking on the obligation to buy (or sell) the stock at the strike. Over a multi-month window that's mostly sideways-to-up chop, a wheel that is <i>never idle</i> - always short something, on whichever side of the stock it currently holds - should out-earn simply owning the stock on a risk-adjusted basis, even if it can't match a strong rally dollar-for-dollar, because premium income smooths the ride: it pads small down weeks and caps (rather than eliminates) the upside of up weeks. Sizing the strikes off actually-measured conditions, rather than a single flat OTM% every week, is the specific bet this version makes: a put sold into elevated realized vol can sit closer to the money (richer premium, and a dip to it is more plausible anyway), while a calm week's put sits further out (premium is thin, no edge in forcing an assignment); a call sold while still underwater on the shares sits further out (don't hand away the recovery cheaply), while one sold already above cost basis sits tight (lean into getting called away and restarting the cycle). The early-close rule - buy back either leg once its own price round-trips to ${P.early_close_mult}&times; what was collected for it - exists so a single bad week can't turn into an open-ended loss while the position rides to Friday. The thesis is specifically that this adaptive sizing beats a flat-rule wheel and improves the Sharpe ratio versus buy-and-hold, not necessarily the raw return, since the stock's own upside is uncapped and the wheel's income per week is not.</p>

    <h3>What I tested</h3>
    <p>${M.ticker}, ${S.weeks} weeks (${md(first.d)} to ${md(last.d)}). Every Monday at the first bar at/after 10:00 ET: if flat, sell 1 cash-secured put; if holding 100 shares from a prior assignment, sell 1 covered call instead. Friday's last regular-session print decides the outcome - assigned / called away, or expires worthless - unless the early-close rule already closed it out mid-week. The backtest trusts the data source for exactly one thing: that a given option RIC is a real, quoted contract. Every regime label, strike-rule check, fill, early-close trigger, and Friday settlement is independently re-derived from the raw saved bars for the Checks section below, never taken from the fetch script's own bookkeeping.</p>
    <ol class="rules">
      <li><b>Rule 1, puts (while flat).</b> Trailing realized vol - annualized stdev of daily log returns over the last ${P.vol_lookback_days} trading days, using only days strictly before the decision - is classified against the trailing weekly history into calm / normal / elevated (tertile split; the first few weeks default to "normal" for lack of history). OTM target: ${putOtmTxt}. Sell the first real listed strike at or below that target.</li>
      <li><b>Rule 2, calls (while holding).</b> Spot vs. the shares' cost basis (the put strike that got assigned) decides the target: ${callOtmTxt}. Sell the first real listed strike at or above that target.</li>
      <li><b>Rule 3, early close (either leg).</b> Checked every bar of the option's own week: if its mid price reaches ${P.early_close_mult}&times; the premium collected at entry, buy it back rather than let a loser run to the last cents of extrinsic value for a shrinking reward.</li>
      <li><b>Rule 4, Friday.</b> Put struck above Friday's last print &rarr; assigned: buy 100 shares at the strike, cost basis becomes that strike. Call struck below Friday's last print &rarr; called away: sell the 100 shares at the strike, flat again. Otherwise it expires worthless and the position carries into the next week unchanged.</li>
    </ol>

    <h3>Fill-price assumption, and why</h3>
    <p>Every entry and every early-close buyback fills at the contract's own quoted mid, (BID+ASK)/2, on the same bar as the decision - never a more favorable price, and never a trade print assumed where no real quote exists (no bid/ask that bar &rarr; the week is skipped, not faked). This is the same assumption used for HW1.2 and HW2, carried over because it's testable: the Mid vs trade chart below plots every bar where a short option - put or call, whichever leg was open - had both a real quote <i>and</i> a real trade print, mid on the X axis against the actual trade on the Y axis. If trades cluster on the y = x line, assuming the mid is a fair stand-in for where a real order would have filled; the regression stats (slope, intercept, R&sup2;, and the share of trades inside the quoted bid/ask) on that chart quantify exactly how fair.</p>

    <h3>Starting capital</h3>
    <p>The put leg is fully cash-secured - no margin - so starting cash has to cover the largest strike the backtest ever gets assigned at, net of every dollar already spent buying back a losing early close before that point. Rather than guess a worst case up front, the backtest runs once to find the lowest cumulative cash balance the actual trade path reaches, then funds exactly enough above that (plus a small buffer) so cash never goes negative anywhere in the run - checked explicitly below. Buy-and-hold is funded with that same starting cash, in the same shares at the first entry's stock price, so the comparison is dollar-for-dollar on identical starting capital, not just percent returns on different bases.</p>

    <h3>Verdict</h3>
    <p id="verdict-text"></p>

    <h3>Simplifications</h3>
    <p>No commissions or fees. No dividends. Exactly one option position open at a time (put or call, never both), 100 shares per lot. Settlement checked only against Friday's last regular-session print, not intraday. ${M.demo ? "This particular run is on synthetic demo data (see the banner above) - built only to exercise the engine and its checks before spending real LSEG pulls; the numbers above mean nothing about actual INTC." : ""}</p>

    <h3>Enhancement: earnings blackout</h3>
    <p id="blackout-text"></p>

    <p class="muted" style="margin-top:18px">Why this result looks the way it does, how the engine was actually built and debugged, and why INTC &mdash; <a href="story.html">the full story</a>.</p>`;

  const verdictEl = $("#verdict-text");
  (function writeVerdict() {
    const retTxt = beatReturn
      ? `The wheel's raw return, ${pct(S.ret)}, actually beat buy-and-hold's ${pct(S.holdRet)} outright over this window`
      : `Buy-and-hold's raw return, ${pct(S.holdRet)}, beat the wheel's ${pct(S.ret)} outright over this window - expected, since the thesis never claimed to out-run an uncapped stock move, just to do so more efficiently`;
    let sharpeTxt;
    if (S.sharpe == null || S.holdSharpe == null) {
      sharpeTxt = "there wasn't enough daily history in this window for a reliable Sharpe comparison";
    } else if (beatSharpe) {
      sharpeTxt = `on a risk-adjusted basis the thesis held up: Sharpe ${S.sharpe.toFixed(2)} for the wheel against ${S.holdSharpe.toFixed(2)} for buy-and-hold, on realized vol of ${S.vol.toFixed(1)}% vs ${S.holdVol.toFixed(1)}%`;
    } else {
      sharpeTxt = `the thesis did not hold up here: Sharpe came in at ${S.sharpe.toFixed(2)} for the wheel against ${S.holdSharpe.toFixed(2)} for buy-and-hold (realized vol ${S.vol.toFixed(1)}% vs ${S.holdVol.toFixed(1)}%)${S.premium < 0 ? `, and net premium was actually negative (${usd(S.premium, 0)}) - the early-close rule paid out more buying back trouble than the weekly sales brought in, over this particular path` : ""}`;
    }
    const ddTxt = `Max drawdown was ${pct(S.maxDD)} for the wheel vs ${pct(S.holdMaxDD)} for buy-and-hold.`;
    verdictEl.textContent = `${retTxt}. On the Sharpe question the thesis actually hinges on, ${sharpeTxt}. ${ddTxt}`;
  })();

  (function writeBlackout() {
    const blackoutWeeks = (B.weeks || []).filter(w => w.skip && w.skip.toLowerCase().includes("earnings blackout"));
    const el2 = $("#blackout-text");
    if (!blackoutWeeks.length) {
      el2.textContent = "No earnings fell inside this run's window, so the blackout rule never actually fired here - included anyway as the engine's standing risk rule.";
      return;
    }
    const weekList = blackoutWeeks.map(w => md(w.expiry)).join(" and ");
    el2.textContent = `This strategy sells one week of premium at a time, sized off realized volatility - it is never compensated for the kind of overnight gap a quarterly earnings print can cause, where the stock can move straight through any reasonable OTM strike before the position can react. So the rule: don't open a new put or call the week OF earnings, or the week immediately before it. Over this window that meant skipping ${weekList} (${blackoutWeeks.length} of 22 weeks) - no new premium collected those weeks, but also no earnings-gap risk taken on a position the model has no edge pricing. The ${pct(S.ret)} result above already includes that cost.`;
  })();

  /* ------------------------------------------------------------------ blotter */
  const blotterCols = [
    { h: "#", cls: "n", f: r => r.n },
    { h: "Time (ET)", f: r => r.lbl },
    { h: "Instrument", f: r => r.instr },
    { h: "Side", f: r => chip(r.side) },
    { h: "Qty", cls: "n", f: r => r.qty },
    { h: "Limit", cls: "n", f: r => r.limit == null ? "—" : num(r.limit, 3) },
    { h: "Fill", cls: "n", f: r => (r.side === "EXPIRE") ? "0.000" : num(r.fill, 3) },
    { h: "Bid / Ask", cls: "n", f: r => r.bid == null ? "—" : `${num(r.bid)} / ${num(r.ask)}` },
    { h: "Cash Δ", cls: "n", f: r => el("span", { class: r.dcash < 0 ? "neg" : "", text: usd(r.dcash) }) },
    { h: "Cash after", cls: "n", f: r => usd(r.cash) },
    { h: "Shares after", cls: "n", f: r => r.shares },
    { h: "Note (rule)", cls: "wrapc", f: r => el("span", {}, el("b", { text: r.rule + ". " }), r.note) },
  ];
  const blotterRowClass = r => (r.side === "CLOSE" || r.side === "EXPIRE" || r.side === "ASSIGN" || r.side === "CALLED_AWAY") ? "ev" : "";
  table($("#blotter-tbl"), blotterCols, BL, blotterRowClass);

  /* ------------------------------------------------------------------- weeks */
  table($("#weeks-tbl"), [
    { h: "Wk", cls: "n", f: r => r.week },
    { h: "Expiry", f: r => md(r.expiry) },
    { h: "Side", f: r => chip(r.side) },
    { h: "Entry (ET)", f: r => lblIso(r.entry) || "—" },
    { h: "Regime / tier", f: r => r.side === "P" ? (r.regime || "—") : (r.tier ? r.tier.replace("_", " ") : "—") },
    { h: "Spot", cls: "n", f: r => num(r.spot) },
    { h: "Strike", cls: "n", f: r => r.strike == null ? "—" : `${num(r.strike, r.strike % 1 ? 1 : 0)}${otmPct(r.strike, r.spot)}` },
    { h: "Cost basis", cls: "n", f: r => r.costBasis == null ? "—" : num(r.costBasis, r.costBasis % 1 ? 1 : 0) },
    { h: "Mid → premium", cls: "n", f: r => r.premium == null ? "—" : `${num(r.premium, 3)} → ${usd(r.premium * 100)}` },
    { h: "Stock at expiry", cls: "n", f: r => num(r.fridaySpot) },
    { h: "Outcome", f: r => chip(r.outcome || "—") },
  ], weeks);
  const skipEl = $("#skips");
  if (!skips.length) skipEl.hidden = true;
  for (const s of skips) skipEl.append(el("li", { text: `Week ${s.week} skipped: ${s.reason}.` }));

  /* ------------------------------------------------------------------ ledger */
  let rowsMode = "daily";
  function visibleRows(list) {
    if (rowsMode === "all") return list;
    return list.filter((r, i) => r.event || i === list.length - 1 || list[i + 1].d !== r.d);
  }
  function posText(pos) {
    if (!pos) return "—";
    const txt = `${num(pos.k, pos.k % 1 ? 1 : 0)}${pos.side} exp ${md(pos.exp)} @ ${num(pos.mark, 3)}${pos.stale ? "*" : ""}`;
    return pos.stale ? el("span", { title: "no quote this bar: last mid carried forward", text: txt }) : txt;
  }
  function renderLedger() {
    table($("#ledger-tbl"), [
      { h: "Time (ET)", f: r => r.lbl },
      { h: "Stock", cls: "n", f: r => num(r.S) },
      { h: "State", f: r => chip(r.state) },
      { h: "Shares", cls: "n", f: r => r.shares },
      { h: "Short position", f: r => posText(r.pos) },
      { h: "Cash", cls: "n", f: r => usd(r.cash) },
      { h: "Stock MV", cls: "n", f: r => usd(r.stockMV) },
      { h: "Option MV", cls: "n", f: r => usd(r.optMV) },
      { h: "NAV", cls: "n", f: r => usd(r.nav) },
      { h: "Buy & hold NAV", cls: "n", f: r => usd(r.hold) },
      { h: "Event", f: r => r.event ? chip(r.event) : "" },
    ], visibleRows(L), r => r.event ? "ev" : "");
  }
  document.querySelectorAll("[data-rows]").forEach(b => b.addEventListener("click", () => {
    rowsMode = b.dataset.rows;
    document.querySelectorAll("[data-rows]").forEach(x => x.setAttribute("aria-pressed", String(x === b)));
    renderLedger();
  }));

  /* ---------------------------------------------------------------- compare */
  const cmpRows = [
    ["Starting capital", usd(S.startCash, 0), usd(S.startCash, 0)],
    ["Ending NAV", usd(S.endNav, 0), usd(last.hold, 0)],
    ["Return", pct(S.ret), pct(S.holdRet)],
    ["Worst drop from a peak", pct(S.maxDD), pct(S.holdMaxDD)],
    ["Annualized vol", S.vol == null ? "—" : S.vol.toFixed(1) + "%", S.holdVol == null ? "—" : S.holdVol.toFixed(1) + "%"],
    ["Sharpe (rf " + (M.riskFree * 100).toFixed(1) + "%)", S.sharpe == null ? "—" : S.sharpe.toFixed(2), S.holdSharpe == null ? "—" : S.holdSharpe.toFixed(2)],
    ["Net premium collected", usd(S.premium, 0), "—"],
    ["Weeks expired worthless / closed early", `${S.expired} / ${S.closedEarly}`, "—"],
    ["Weeks assigned / called away", `${S.assigned} / ${S.calledAway}`, "—"],
    ["Weeks skipped", String(S.skipped), "—"],
  ];
  table($("#cmp-tbl"), [
    { h: "", f: r => r[0] },
    { h: "The wheel", cls: "n", f: r => r[1] },
    { h: "Buy & hold", cls: "n", f: r => r[2] },
  ], cmpRows);

  /* ------------------------------------------------------------ line charts */
  function niceStep(raw) { const p = Math.pow(10, Math.floor(Math.log10(raw))); const f = raw / p; return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 2.5 ? 2.5 : f <= 5 ? 5 : 10) * p; }
  function niceTicks(lo, hi, n) {
    if (hi === lo) hi = lo + 1;
    const step = niceStep((hi - lo) / n), a = Math.floor(lo / step) * step, b = Math.ceil(hi / step) * step, out = [];
    for (let v = a; v <= b + step / 2; v += step) out.push(+v.toFixed(10));
    return out;
  }
  function legend(host, items) {
    host.textContent = "";
    for (const it of items) host.append(el("span", {}, el("i", { class: it.type || "ln", style: `border-color:${it.color};background:${it.type === "dt" ? it.color : "transparent"}` }), it.label));
  }
  function lineChart(host, o) {
    host.textContent = "";
    host.tabIndex = 0;
    host.setAttribute("role", "img");
    host.setAttribute("aria-label", o.aria);
    const W = Math.max(240, host.clientWidth), H = o.height || 320, m = { l: 66, r: 14, t: 10, b: 28 };
    const iw = W - m.l - m.r, ih = H - m.t - m.b, n = o.rows.length;
    const vals = o.series.flatMap(s => o.rows.map(s.get)).filter(v => v != null);
    let lo = o.zero ? 0 : Math.min(...vals), hi = Math.max(...vals);
    const pad = (hi - lo) * 0.06 || 1; if (!o.zero) lo -= pad; hi += pad;
    const ticks = niceTicks(lo, hi, 5); lo = ticks[0]; hi = ticks[ticks.length - 1];
    const X = i => m.l + (n <= 1 ? 0 : i / (n - 1)) * iw, Y = v => m.t + ih - (v - lo) / (hi - lo) * ih;

    const svg = sv("svg", { viewBox: `0 0 ${W} ${H}`, width: W, height: H });
    const starts = []; let prev = null;
    o.rows.forEach((r, i) => { const k = mondayOf(r.d); if (k !== prev) { starts.push(i); prev = k; } });
    starts.forEach((s, j) => {
      const e = j + 1 < starts.length ? starts[j + 1] : n - 1;
      if (j % 2 === 1) svg.append(sv("rect", { x: X(s), y: m.t, width: Math.max(0, X(e) - X(s)), height: ih, style: "fill:var(--band)" }));
    });
    for (const t of ticks) {
      svg.append(sv("line", { x1: m.l, x2: W - m.r, y1: Y(t), y2: Y(t), style: "stroke:var(--grid);stroke-width:1" }));
      svg.append(sv("text", { x: m.l - 8, y: Y(t) + 4, "text-anchor": "end", class: "axis-t" }, o.fmt ? o.fmt(t) : usd(t, 0)));
    }
    const every = Math.ceil(starts.length / Math.max(2, Math.floor(iw / 62)));
    starts.forEach((s, j) => { if (j % every === 0) svg.append(sv("text", { x: X(s), y: H - 8, "text-anchor": "start", class: "axis-t" }, md(o.rows[s].d))); });
    for (const s of o.series) {
      let d = "";
      o.rows.forEach((r, i) => { const v = s.get(r); if (v != null) d += (d ? "L" : "M") + X(i).toFixed(1) + "," + Y(v).toFixed(1); });
      svg.append(sv("path", { d, fill: "none", "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round", style: `stroke:${s.color}` }));
    }
    const cross = sv("g", { style: "display:none" });
    const vline = sv("line", { y1: m.t, y2: m.t + ih, style: "stroke:var(--ink-3);stroke-width:1" });
    cross.append(vline);
    const dots = o.series.map(s => { const c = sv("circle", { r: 5, "stroke-width": 2, style: `fill:${s.color};stroke:var(--surface)` }); cross.append(c); return c; });
    svg.append(cross);
    host.append(svg);
    const tip = el("div", { class: "tip" }); host.append(tip);

    let cur = -1;
    function show(i) {
      i = Math.max(0, Math.min(n - 1, i)); cur = i;
      const r = o.rows[i], x = X(i);
      cross.style.display = ""; vline.setAttribute("x1", x); vline.setAttribute("x2", x);
      o.series.forEach((s, k) => { const v = s.get(r); if (v == null) dots[k].style.display = "none"; else { dots[k].style.display = ""; dots[k].setAttribute("cx", x); dots[k].setAttribute("cy", Y(v)); } });
      const t = o.tip(r);
      tip.textContent = "";
      tip.append(el("div", { class: "hd", text: t.head }));
      for (const row of t.rows) {
        if (!row) { tip.append(el("div", { class: "sep" })); continue; }
        tip.append(el("div", { class: "row" },
          el("span", { class: "k" }, row[0] ? el("i", { style: `border-color:${row[0]}` }) : null, row[1]),
          el("span", { class: "v", text: row[2] })));
      }
      tip.style.display = "block";
      const tw = tip.offsetWidth; let lf = x + 14 + tw > W ? x - tw - 14 : x + 14; lf = Math.max(0, Math.min(W - tw, lf)); tip.style.left = lf + "px"; tip.style.top = (m.t + 6) + "px";
    }
    function hide() { cross.style.display = "none"; tip.style.display = "none"; cur = -1; }
    svg.addEventListener("pointermove", e => { const rc = svg.getBoundingClientRect(); show(Math.round(((e.clientX - rc.left) * (W / rc.width) - m.l) / iw * (n - 1))); });
    svg.addEventListener("pointerleave", hide);
    host.addEventListener("focus", () => show(cur < 0 ? n - 1 : cur));
    host.addEventListener("blur", hide);
    host.addEventListener("keydown", e => {
      if (e.key === "ArrowRight") { show((cur < 0 ? 0 : cur) + 1); e.preventDefault(); }
      else if (e.key === "ArrowLeft") { show((cur < 0 ? n - 1 : cur) - 1); e.preventDefault(); }
      else if (e.key === "Home") { show(0); e.preventDefault(); }
      else if (e.key === "End") { show(n - 1); e.preventDefault(); }
    });
  }

  function drawLineCharts() {
    const c1 = css("--s1"), c3 = css("--s3");
    legend($("#lg-cmp"), [{ label: "The wheel (NAV)", color: c1 }, { label: "Buy & hold (NAV)", color: c3 }]);
    lineChart($("#ch-cmp"), {
      rows: L, height: 340, zero: false, fmt: v => usd(v, 0),
      aria: "Line chart of NAV in dollars for the wheel and buy and hold, both starting from the same capital",
      series: [
        { get: r => r.nav, color: c1 },
        { get: r => r.hold, color: c3 },
      ],
      tip: r => ({
        head: r.lbl,
        rows: [
          [c1, "The wheel", usd(r.nav)],
          [c3, "Buy & hold", usd(r.hold)], null,
          [null, "Stock", usd(r.S)],
          [null, "State", r.state],
          [null, "Position", r.pos ? `${num(r.pos.k, r.pos.k % 1 ? 1 : 0)}${r.pos.side} @ ${num(r.pos.mark, 3)}` : "none"],
        ],
      }),
    });
  }

  /* ---------------------------------------------------------------- scatter */
  const SC = { mid: 0, trd: 1, ric: 2, t: 3, k: 4, S: 5, bid: 6, ask: 7, side: 8 };
  function drawScatter() {
    const host = $("#ch-sc"); host.textContent = "";
    const pts = B.scatter.rows;
    const f = B.scatter.fit ? { n: B.scatter.fit.n, b0: B.scatter.fit.intercept, b1: B.scatter.fit.slope, r2: B.scatter.fit.r2 } : null;
    const c1 = css("--s1"), c2 = css("--s2"), c3 = css("--s3");
    legend($("#lg-sc"), [{ label: "Put weeks", color: c1, type: "dt" }, { label: "Call weeks", color: c2, type: "dt" },
      { label: "Least-squares fit", color: c3 }, { label: "y = x (trade equals mid)", color: css("--ink-3") }]);
    const stat = $("#sc-stat"); stat.textContent = "";
    if (!pts.length) { host.append(el("p", { class: "muted", text: "No points match this filter." })); return; }
    const insidePct = 100 * pts.filter(p => p[6] <= p[1] + 1e-9 && p[1] <= p[7] + 1e-9).length / pts.length;
    if (f) stat.append(el("span", {}, "n = ", el("b", { text: f.n.toLocaleString() })),
      el("span", {}, "trade = ", el("b", { text: f.b0.toFixed(3) }), " + ", el("b", { text: f.b1.toFixed(3) }), " × mid"),
      el("span", {}, "R² = ", el("b", { text: f.r2.toFixed(4) })),
      el("span", {}, "trades inside the bid/ask: ", el("b", { text: insidePct.toFixed(0) + "%" })));
    const W = Math.max(240, host.clientWidth), side = Math.min(W, 560), m = { l: 56, r: 12, t: 10, b: 40 };
    const H = side + m.t + m.b, ox = Math.max(0, (W - side) / 2);
    const iw = side - m.l - m.r, ih = H - m.t - m.b;
    let mx = 0; for (const p of pts) mx = Math.max(mx, p[0], p[1]);
    const ticks = niceTicks(0, mx, 5), top = ticks[ticks.length - 1];
    const X = v => ox + m.l + v / top * iw, Y = v => m.t + ih - v / top * ih;
    const dpr = window.devicePixelRatio || 1;
    function mk(z) { const c = el("canvas"); c.width = W * dpr; c.height = H * dpr; c.style.cssText = `width:${W}px;height:${H}px;position:absolute;left:0;top:0;z-index:${z}`; host.append(c); const g = c.getContext("2d"); g.scale(dpr, dpr); return [c, g]; }
    host.style.height = H + "px";
    const [, g] = mk(1), [over, go] = mk(2);
    g.font = "11.5px ui-sans-serif, system-ui, sans-serif"; g.fillStyle = css("--ink-3"); g.strokeStyle = css("--grid"); g.lineWidth = 1;
    for (const t of ticks) {
      g.beginPath(); g.moveTo(X(0), Y(t)); g.lineTo(X(top), Y(t)); g.stroke();
      g.beginPath(); g.moveTo(X(t), Y(0)); g.lineTo(X(t), Y(top)); g.stroke();
      g.textAlign = "right"; g.fillText(usd(t, t < 10 && t % 1 ? 2 : 0), X(0) - 6, Y(t) + 4);
      g.textAlign = "center"; g.fillText(usd(t, t < 10 && t % 1 ? 2 : 0), X(t), Y(0) + 16);
    }
    g.textAlign = "center"; g.fillText("mid = (BID + ASK) / 2", X(top / 2), H - 6);
    g.save(); g.translate(ox + 12, m.t + ih / 2); g.rotate(-Math.PI / 2); g.fillText("TRDPRC_1", 0, 0); g.restore();
    g.strokeStyle = css("--ink-3"); g.lineWidth = 1; g.beginPath(); g.moveTo(X(0), Y(0)); g.lineTo(X(top), Y(top)); g.stroke();
    const xs = new Float32Array(pts.length), ys = new Float32Array(pts.length);
    pts.forEach((p, i) => {
      xs[i] = X(p[0]); ys[i] = Y(p[1]);
      g.fillStyle = p[SC.side] === "P" ? c1 : c2; g.globalAlpha = 0.5;
      g.beginPath(); g.arc(xs[i], ys[i], 3.2, 0, 6.2832); g.fill();
    });
    g.globalAlpha = 1;
    if (f) { g.strokeStyle = c3; g.lineWidth = 2; g.beginPath(); g.moveTo(X(0), Y(Math.max(0, f.b0))); g.lineTo(X(top), Y(f.b0 + f.b1 * top)); g.stroke(); }
    const tip = el("div", { class: "tip" }); host.append(tip); tip.style.zIndex = 6;
    over.addEventListener("pointermove", e => {
      const rc = over.getBoundingClientRect(), px = e.clientX - rc.left, py = e.clientY - rc.top;
      let best = -1, bd = 22 * 22;
      for (let i = 0; i < xs.length; i++) { const d = (xs[i] - px) ** 2 + (ys[i] - py) ** 2; if (d < bd) { bd = d; best = i; } }
      go.clearRect(0, 0, W, H);
      if (best < 0) { tip.style.display = "none"; return; }
      const p = pts[best];
      go.strokeStyle = css("--ink"); go.lineWidth = 2; go.beginPath(); go.arc(xs[best], ys[best], 6, 0, 6.2832); go.stroke();
      tip.textContent = "";
      tip.append(el("div", { class: "hd", text: `${p[SC.ric]} (${sideWord(p[SC.side])})` }), el("div", { class: "row" }, el("span", { class: "k", text: "Time (ET)" }), el("span", { class: "v", text: p[SC.t] })),
        el("div", { class: "row" }, el("span", { class: "k", text: "Strike / spot" }), el("span", { class: "v", text: `${num(p[SC.k], p[SC.k] % 1 ? 1 : 0)} / ${p[SC.S] == null ? "—" : num(p[SC.S])}` })),
        el("div", { class: "row" }, el("span", { class: "k", text: "Bid / ask" }), el("span", { class: "v", text: `${num(p[SC.bid])} / ${num(p[SC.ask])}` })),
        el("div", { class: "row" }, el("span", { class: "k", text: "Mid" }), el("span", { class: "v", text: num(p[SC.mid], 3) })),
        el("div", { class: "row" }, el("span", { class: "k", text: "Trade" }), el("span", { class: "v", text: num(p[SC.trd], 2) })));
      tip.style.display = "block"; const tw = tip.offsetWidth;
      tip.style.left = Math.min(W - tw - 4, Math.max(4, xs[best] + 14)) + "px"; tip.style.top = Math.max(4, ys[best] - 20) + "px";
    });
    over.addEventListener("pointerleave", () => { go.clearRect(0, 0, W, H); tip.style.display = "none"; });
  }

  /* --------------------------------------------------------------- checks */
  const cl = $("#checks-list");
  for (const c of B.checks)
    cl.append(el("li", {}, el("span", { class: "badge " + (c.ok ? "ok" : "bad"), text: c.ok ? "✓ PASS" : "✕ FAIL" }),
      el("span", {}, el("b", { text: c.name }), el("span", { class: "muted", text: " — " + c.detail }))));

  $("#foot").append(el("p", {}, `Data: ${M.source}${M.fetchedAt ? ", pulled " + M.fetchedAt : ""}. Built ${M.built}. Timestamps: ${M.tz}. `),
    el("p", {}, "Published from book3.js only; the local LSEG data tool is on the ", el("a", { href: "data.html", text: "Data page" }), "."),
    el("p", {}, el("a", { href: "story.html", text: "Read the full story" }), " - why INTC, how this was built, and the bugs that nearly sank it."));

  /* ------------------------------------------------------------------ theme */
  const modes = ["auto", "light", "dark"];
  let mode = "auto";
  try { mode = localStorage.getItem("wheel-theme") || "auto"; } catch (e) { /* storage blocked: fine */ }
  function applyTheme() {
    if (mode === "auto") document.documentElement.removeAttribute("data-theme"); else document.documentElement.setAttribute("data-theme", mode);
    $("#theme").textContent = "Theme: " + mode;
  }
  $("#theme").addEventListener("click", () => {
    mode = modes[(modes.indexOf(mode) + 1) % 3]; applyTheme();
    try { localStorage.setItem("wheel-theme", mode); } catch (e) { /* ignore */ }
    drawAll();
  });
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => drawAll());

  function drawAll() { drawLineCharts(); drawScatter(); }
  applyTheme(); renderLedger(); drawAll();
  let rt; window.addEventListener("resize", () => { clearTimeout(rt); rt = setTimeout(drawAll, 120); });
})();
