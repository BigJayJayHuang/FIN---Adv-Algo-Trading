/* HW2 poor man's covered call - renders everything from window.PMCC_BOOK (book2.js). No libraries, no network. */
(function () {
  "use strict";
  const B = window.PMCC_BOOK;
  if (!B) {
    document.body.textContent = "book2.js is missing. Run: python3 build_book2.py";
    return;
  }
  const M = B.meta, CFG = B.config, CC = B.cc, PM = B.pmcc;
  const CS = CC.summary, PS = PM.summary;

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
  const Lc = CC.ledger, Lp = PM.ledger;
  const first = Lc[0], last = Lc[Lc.length - 1];
  $("#title").textContent = `${M.ticker} poor man's covered call`;
  $("#subtitle").textContent = `A diagonal call spread as a lower-capital stand-in for a covered call: one long, deep-in-the-money call held the whole test, with a weekly 3% OTM short call sold against it. ${md(first.d)} to ${md(last.d)}, ${first.d.slice(0, 4)}; ${M.interval} bars; compared against a real covered call and plain buy-and-hold on the same stock.`;
  if (M.demo) $("#demo-banner").hidden = false;
  document.title = "HW2 - Poor Man's Covered Call";

  /* ---------------------------------------------------------------- summary */
  const capPct = (PS.capitalRequired / CS.capitalRequired) * 100;
  $("#summary-lede").textContent =
    `The long call cost ${usd(PS.longCost, 0)} - ${capPct.toFixed(0)}% of the ${usd(CS.capitalRequired, 0)} it takes to buy 100 shares for the covered call. ` +
    `PMCC ended at ${pct(PS.ret)} on its own capital; the covered call ended at ${pct(CS.ret)} on its (much larger) capital; buy-and-hold was ${pct(CS.holdRet)}.`;
  const tiles = [
    { cls: "hero-t", lab: "Capital required: PMCC", val: usd(PS.capitalRequired, 0), note: `${capPct.toFixed(0)}% of the covered call's ${usd(CS.capitalRequired, 0)}` },
    { lab: "PMCC return on that capital", val: pct(PS.ret), note: `NAV ${usd(PS.startNav, 0)} → ${usd(PS.endNav, 0)}` },
    { lab: "Covered call return", val: pct(CS.ret), note: `on ${usd(CS.capitalRequired, 0)} of capital` },
    { lab: "Buy & hold return", val: pct(CS.holdRet), note: "same stock, same entry day, no options" },
    { lab: "PMCC weeks", val: `${PS.expired} expired · ${PS.closed} closed`, note: `${PS.skipped} skipped of ${PS.weeks}` },
  ];
  $("#tiles").append(...tiles.map(t => el("div", { class: "card tile " + (t.cls || "") },
    el("div", { class: "lab", text: t.lab }), el("div", { class: "val", text: t.val }), el("div", { class: "note", text: t.note }))));

  /* ---------------------------------------------------------------- write-up */
  const holdDD = maxDD(Lc.map(r => r.hold));
  const monthsOut = Math.round((new Date(M.longExpiry) - new Date(CC.ledger[0].t)) / (30 * 86400000));
  const longEntryRow = Lp.find(r => r.long) || {};
  const longEntrySpot = longEntryRow.S || first.S;
  const longItmPct = (1 - PS.longStrike / longEntrySpot) * 100;
  const wb = $("#writeup-body");
  wb.innerHTML = `
    <p><b>What a poor man's covered call is.</b> A regular covered call ties up the full price of 100 shares. A PMCC swaps the shares for one long-dated, deep-in-the-money call - it moves almost one-for-one with the stock (high delta), so it behaves like a cheaper stand-in for owning it - and sells the same kind of weekly call against that instead of against real shares. It's "poor man's" because the capital required is the price of that one long call, not the full stock position.</p>
    <p><b>What I tested.</b> ${M.ticker}, ${PS.weeks} weeks (${md(first.d)} to ${md(last.d)}). I ran two books on the exact same stock and option data: this PMCC, and a plain covered call (same engine as my HW1.2, same ticker, same weekly strike rule), so the only thing that differs between them is what backs the short call - one long call here, 100 real shares there. A third line, buy-and-hold, just owns the stock with no options at all.</p>
    <ol class="rules">
      <li><b>Rule 1, the long call (once).</b> At the first available quote on/after the window's first Monday 10:00 ET, buy 1 call roughly ${longItmPct.toFixed(0)}% in the money - strike ${num(PS.longStrike, PS.longStrike % 1 ? 1 : 0)}, expiring ${md(PS.longExpiry)} (${monthsOut} months out) - at mid. I hold it, marked every bar, for the whole test. It is never rolled or sold early.</li>
      <li><b>Rule 2, the short call (weekly).</b> Every Monday at the first bar at/after 10:00 ET, if the long call is open, sell 1 call expiring that Friday: the first listed strike at or above spot &times; 1.03. Limit = fill = mid, same as HW1.2. No bid/ask on that call &rarr; skip the week, no price invented.</li>
      <li><b>Rule 3, Friday.</b> Stock at or below the short strike &rarr; it expires worthless, I keep the premium. Stock above the short strike &rarr; there are no shares to deliver, so instead of letting it be assigned I buy it back at its own last mark. This is the real mechanical difference from a covered call: a PMCC trader can't let the short call run into an assignment with nothing to cover it, short of exercising the long call early and giving up whatever time value is left in it. Buying back is the more common practice and the simpler rule to backtest honestly.</li>
    </ol>

    <h3>Why this takes less capital</h3>
    <p>The short call's strike (spot + 3%) is always above the long call's strike (about 20% below spot) in this test - I check that on every single sale (see Checks). FINRA's margin rule for a diagonal call spread treats a short call as fully covered, with $0 of additional margin required, whenever the short strike is at or above the long strike and the long call doesn't expire first. Both hold here by construction. So the only capital actually at risk is what I paid for the long call up front: ${usd(PS.longCost, 0)}. The covered call, by contrast, needs the full ${usd(CS.capitalRequired, 0)} to own 100 shares (I size its starting cash the same way I did in HW1.2: enough to buy the shares with a buffer above the Reg T 50% initial requirement).</p>
    <p>Trading less capital cuts both ways, though: the same dollar move in ${M.ticker} is a much bigger percentage move on ${usd(PS.capitalRequired, 0)} than on ${usd(CS.capitalRequired, 0)}. PMCC's worst drawdown here was ${Math.abs(maxDD(Lp.map(r => r.nav))).toFixed(1)}%, against ${Math.abs(maxDD(Lc.map(r => r.nav))).toFixed(1)}% for the covered call and ${Math.abs(holdDD).toFixed(1)}% for buy-and-hold. Less capital deployed does not mean less risk taken - it means the same risk is being taken with less money, which is exactly the point of the strategy, but it's worth saying plainly.</p>

    <h3>Why I picked these numbers</h3>
    <p>LSEG's hourly history has price fields only, no option greeks, so "deep in the money" here is done by moneyness (~20% below spot) rather than an actual delta target - a real desk would target delta (commonly 0.75-0.85) directly. ~${monthsOut} months keeps the long call's own time decay slow relative to the ~${PS.weeks}-week test, and is short enough that the historical option chain that far out still has real quotes. The weekly short call uses the same 3% OTM rule as my HW1.2 "my version," so the comparison isn't also mixing in a different strike choice.</p>

    <h3>Honesty on the fills</h3>
    <p>Both legs are priced from the same hourly bar as the stock print. The Mid vs trade chart checks the weekly short-call fills the same way HW1.2 did<span id="fit-note">.</span></p>

    <h3>Simplifications</h3>
    <p>No commissions or fees. No early exercise of the long call and no dividends. I only check the short call against the stock at Friday's last regular-session bar. The long call's own expiry (${md(PS.longExpiry)}) is well after this test ends, so it is never itself exercised, assigned, or rolled here - the backtest just marks it to market through the last bar and stops.</p>`;
  const CC_FIT_TEXT_EL = wb.querySelector("#fit-note");

  /* ------------------------------------------------------------- PMCC blotter */
  const pmccBlotterCols = [
    { h: "#", cls: "n", f: r => r.n },
    { h: "Time (ET)", f: r => r.lbl },
    { h: "Instrument", f: r => r.instr },
    { h: "Side", f: r => chip(r.side) },
    { h: "Qty", cls: "n", f: r => r.qty },
    { h: "Limit", cls: "n", f: r => r.limit == null ? "—" : num(r.limit, 3) },
    { h: "Fill", cls: "n", f: r => r.side === "EXPIRE" ? "0.000" : num(r.fill, 3) },
    { h: "Bid / Ask", cls: "n", f: r => r.bid == null ? "—" : `${num(r.bid)} / ${num(r.ask)}` },
    { h: "Cash Δ", cls: "n", f: r => el("span", { class: r.dcash < 0 ? "neg" : "", text: usd(r.dcash) }) },
    { h: "Cash after", cls: "n", f: r => usd(r.cash) },
    { h: "NAV after", cls: "n", f: r => usd(r.nav) },
    { h: "Note (rule)", cls: "wrapc", f: r => el("span", {}, el("b", { text: r.rule + ". " }), r.note) },
  ];
  const blotterRow = r => (r.side === "CLOSE" || r.side === "EXPIRE") ? "ev" : "";
  table($("#pmcc-blotter-tbl"), pmccBlotterCols, PM.blotter, blotterRow);

  /* --------------------------------------------------------------- PMCC weeks */
  table($("#pmcc-weeks-tbl"), [
    { h: "Wk", cls: "n", f: r => r.week },
    { h: "Expiry", f: r => md(r.expiry) },
    { h: "Entry (ET)", f: r => r.entry_lbl || "—" },
    { h: "Spot", cls: "n", f: r => num(r.spot) },
    { h: "Short strike", cls: "n", f: r => r.strike == null ? "—" : `${num(r.strike, r.strike % 1 ? 1 : 0)}` + (r.spot ? ` (${pct((r.strike / r.spot - 1) * 100)})` : "") },
    { h: "Mid → premium", cls: "n", f: r => r.mid == null ? "—" : `${num(r.mid, 3)} → ${usd(r.mid * 100)}` },
    { h: "Stock at expiry", cls: "n", f: r => num(r.S_T) },
    { h: "Outcome", f: r => chip(r.outcome || "—") },
    { h: "NAV start → end", cls: "n", f: r => r.nav_start == null ? "—" : `${usd(r.nav_start, 0)} → ${usd(r.nav_end, 0)}` },
  ], PM.weeks);
  const psk = $("#pmcc-skips");
  if (!PM.skips.length) psk.hidden = true;
  for (const s of PM.skips) psk.append(el("li", { text: `Week ${s.week} skipped: ${s.reason}.` }));

  /* -------------------------------------------------------------- PMCC ledger */
  let rowsMode = "daily";
  function visibleRows(list) {
    if (rowsMode === "all") return list;
    return list.filter((r, i) => r.event || i === list.length - 1 || list[i + 1].d !== r.d);
  }
  function renderPmccLedger() {
    table($("#pmcc-ledger-tbl"), [
      { h: "Time (ET)", f: r => r.lbl },
      { h: "Stock", cls: "n", f: r => num(r.S) },
      { h: "Long call", f: r => r.long ? el("span", { title: r.long.stale ? "no quote this bar: last mid carried forward" : "", text: `${num(r.long.k, r.long.k % 1 ? 1 : 0)}C exp ${md(r.long.exp)} @ ${num(r.long.mark, 3)}${r.long.stale ? "*" : ""}` }) : "—" },
      { h: "Short call", f: r => r.call ? el("span", { title: r.call.stale ? "no quote this bar: last mid carried forward" : "", text: `${num(r.call.k, r.call.k % 1 ? 1 : 0)}C exp ${md(r.call.exp)} @ ${num(r.call.mark, 3)}${r.call.stale ? "*" : ""}` }) : "—" },
      { h: "Cash", cls: "n", f: r => usd(r.cash) },
      { h: "Long MV", cls: "n", f: r => usd(r.longMV) },
      { h: "Short MV", cls: "n", f: r => usd(r.shortMV) },
      { h: "NAV", cls: "n", f: r => usd(r.nav) },
      { h: "Event", f: r => r.event ? chip(r.event) : "" },
    ], visibleRows(Lp), r => r.event ? "ev" : "");
  }
  document.querySelectorAll("[data-rows]").forEach(b => b.addEventListener("click", () => {
    rowsMode = b.dataset.rows;
    document.querySelectorAll("[data-rows]").forEach(x => x.setAttribute("aria-pressed", String(x === b)));
    renderPmccLedger();
  }));

  /* ---------------------------------------------------------------- compare */
  const cmpRows = [
    ["Capital required", usd(CS.capitalRequired, 0), usd(PS.capitalRequired, 0), usd(CS.capitalRequired, 0)],
    ["Ending NAV", usd(CS.endNav, 0), usd(PS.endNav, 0), usd(last.hold, 0)],
    ["Return on capital", pct(CS.ret), pct(PS.ret), pct(CS.holdRet)],
    ["Worst drop from a peak", pct(maxDD(Lc.map(r => r.nav))), pct(maxDD(Lp.map(r => r.nav))), pct(holdDD)],
    ["Net premium collected", usd(CS.premium, 0), usd(PS.premium, 0), "—"],
    ["Weeks expired worthless", `${CS.expired} of ${CS.traded}`, `${PS.expired} of ${PS.traded}`, "—"],
    ["Weeks assigned / closed", `${CS.assigned} assigned`, `${PS.closed} closed`, "—"],
    ["Weeks skipped", String(CS.skipped), String(PS.skipped), "—"],
  ];
  table($("#cmp-tbl"), [
    { h: "", f: r => r[0] },
    { h: "Covered call", cls: "n", f: r => r[1] },
    { h: "PMCC", cls: "n", f: r => r[2] },
    { h: "Buy & hold", cls: "n", f: r => r[3] },
  ], cmpRows);

  /* --------------------------------------------------------------- CC section */
  table($("#cc-weeks-tbl"), [
    { h: "Wk", cls: "n", f: r => r.week },
    { h: "Expiry", f: r => md(r.expiry) },
    { h: "Spot", cls: "n", f: r => num(r.spot) },
    { h: "Strike", cls: "n", f: r => r.strike == null ? "—" : `${num(r.strike, r.strike % 1 ? 1 : 0)}` + (r.spot ? ` (${pct((r.strike / r.spot - 1) * 100)})` : "") },
    { h: "Mid → premium", cls: "n", f: r => r.mid == null ? "—" : `${num(r.mid, 3)} → ${usd(r.mid * 100)}` },
    { h: "Stock at expiry", cls: "n", f: r => num(r.S_T) },
    { h: "Outcome", f: r => chip(r.outcome || "—") },
  ], CC.weeks);
  const csk = $("#cc-skips");
  if (!CC.skips.length) csk.hidden = true;
  for (const s of CC.skips) csk.append(el("li", { text: `Week ${s.week} skipped: ${s.reason}.` }));
  const ccBlotterCols = [
    { h: "#", cls: "n", f: r => r.n }, { h: "Time (ET)", f: r => r.lbl }, { h: "Instrument", f: r => r.instr },
    { h: "Side", f: r => chip(r.side) }, { h: "Qty", cls: "n", f: r => r.qty },
    { h: "Fill", cls: "n", f: r => r.side === "EXPIRE" ? "0.000" : num(r.fill, r.side === "SELL" ? 3 : 2) },
    { h: "Cash Δ", cls: "n", f: r => el("span", { class: r.dcash < 0 ? "neg" : "", text: usd(r.dcash) }) },
    { h: "Cash after", cls: "n", f: r => usd(r.cash) }, { h: "Shares after", cls: "n", f: r => r.shares },
    { h: "Note (rule)", cls: "wrapc", f: r => el("span", {}, el("b", { text: r.rule + ". " }), r.note) },
  ];
  table($("#cc-blotter-tbl"), ccBlotterCols, CC.blotter, r => (r.side === "ASSIGN" || r.side === "EXPIRE") ? "ev" : "");

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
    const c1 = css("--s1"), c2 = css("--s2"), c3 = css("--s3");
    legend($("#lg-cmp"), [{ label: "Covered call (% of its own capital)", color: c1 }, { label: "PMCC (% of its own capital)", color: c2 }, { label: "Buy & hold (% of covered-call capital)", color: c3 }]);
    const rows = Lc.map((r, i) => ({ d: r.d, lbl: r.lbl, c: r, p: Lp[i] || r }));
    lineChart($("#ch-cmp"), {
      rows, height: 340, zero: false, fmt: v => v.toFixed(0) + "%",
      aria: "Line chart of NAV as a percent of starting capital for the covered call, PMCC, and buy and hold",
      series: [
        { get: r => r.c.nav / CS.capitalRequired * 100, color: c1 },
        { get: r => r.p.nav / PS.capitalRequired * 100, color: c2 },
        { get: r => r.c.hold / CS.capitalRequired * 100, color: c3 },
      ],
      tip: r => ({
        head: r.lbl,
        rows: [
          [c1, "Covered call", (r.c.nav / CS.capitalRequired * 100).toFixed(1) + "%"],
          [c2, "PMCC", (r.p.nav / PS.capitalRequired * 100).toFixed(1) + "%"],
          [c3, "Buy & hold", (r.c.hold / CS.capitalRequired * 100).toFixed(1) + "%"], null,
          [null, "Stock", usd(r.c.S)],
          [null, "Long call", r.p.long ? `${num(r.p.long.k, r.p.long.k % 1 ? 1 : 0)}C @ ${num(r.p.long.mark, 3)}` : "none"],
          [null, "PMCC short call", r.p.call ? `${num(r.p.call.k, r.p.call.k % 1 ? 1 : 0)}C @ ${num(r.p.call.mark, 3)}` : "none"],
        ],
      }),
    });
  }

  /* ---------------------------------------------------------------- scatter */
  const SC = { mid: 0, trd: 1, ric: 2, t: 3, k: 4, S: 5, bid: 6, ask: 7 };
  function regress(pts) {
    const n = pts.length; if (n < 3) return null;
    let sx = 0, sy = 0; for (const p of pts) { sx += p[0]; sy += p[1]; }
    const mx = sx / n, my = sy / n; let sxx = 0, sxy = 0, syy = 0;
    for (const p of pts) { sxx += (p[0] - mx) ** 2; sxy += (p[0] - mx) * (p[1] - my); syy += (p[1] - my) ** 2; }
    if (!sxx || !syy) return null;
    const b1 = sxy / sxx, b0 = my - b1 * mx;
    let res = 0; for (const p of pts) res += (p[1] - (b0 + b1 * p[0])) ** 2;
    return { n, b0, b1, r2: 1 - res / syy };
  }
  function drawScatter() {
    const host = $("#ch-sc"); host.textContent = "";
    const pts = CC.scatter.rows;
    const rawFit = CC.scatter.fit;
    const f = rawFit ? { n: rawFit.n, b0: rawFit.intercept, b1: rawFit.slope, r2: rawFit.r2 } : regress(pts);
    const c1 = css("--s1"), c2 = css("--s2");
    legend($("#lg-sc"), [{ label: "Weekly short-call bars with a real bid/ask and a trade", color: c1, type: "dt" },
      { label: "Least-squares fit", color: c2 }, { label: "y = x (trade equals mid)", color: css("--ink-3") }]);
    const stat = $("#sc-stat"); stat.textContent = "";
    if (!pts.length) { host.append(el("p", { class: "muted", text: "No points match this filter." })); return; }
    const insidePct = 100 * pts.filter(p => p[6] <= p[1] + 1e-9 && p[1] <= p[7] + 1e-9).length / pts.length;
    if (f) stat.append(el("span", {}, "n = ", el("b", { text: f.n.toLocaleString() })),
      el("span", {}, "trade = ", el("b", { text: f.b0.toFixed(3) }), " + ", el("b", { text: f.b1.toFixed(3) }), " × mid"),
      el("span", {}, "R² = ", el("b", { text: f.r2.toFixed(4) })),
      el("span", {}, "trades inside the bid/ask: ", el("b", { text: insidePct.toFixed(0) + "%" })));
    if (CC_FIT_TEXT_EL && f) CC_FIT_TEXT_EL.textContent =
      `: across ${f.n.toLocaleString()} bars, trade = ${f.b0.toFixed(3)} + ${f.b1.toFixed(3)} × mid with R² = ${f.r2.toFixed(4)}, and ${insidePct.toFixed(0)}% of those trades were at or inside the quoted bid/ask.`;
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
    g.fillStyle = c1; g.globalAlpha = 0.5;
    const xs = new Float32Array(pts.length), ys = new Float32Array(pts.length);
    pts.forEach((p, i) => { xs[i] = X(p[0]); ys[i] = Y(p[1]); g.beginPath(); g.arc(xs[i], ys[i], 3.2, 0, 6.2832); g.fill(); });
    g.globalAlpha = 1;
    if (f) { g.strokeStyle = c2; g.lineWidth = 2; g.beginPath(); g.moveTo(X(0), Y(Math.max(0, f.b0))); g.lineTo(X(top), Y(f.b0 + f.b1 * top)); g.stroke(); }
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
      tip.append(el("div", { class: "hd", text: p[SC.ric] }), el("div", { class: "row" }, el("span", { class: "k", text: "Time (ET)" }), el("span", { class: "v", text: p[SC.t] })),
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
  for (const c of CC.checks.concat(PM.checks.map(c => ({ ...c, name: "[PMCC] " + c.name }))))
    cl.append(el("li", {}, el("span", { class: "badge " + (c.ok ? "ok" : "bad"), text: c.ok ? "✓ PASS" : "✕ FAIL" }),
      el("span", {}, el("b", { text: c.name }), el("span", { class: "muted", text: " — " + c.detail }))));

  $("#foot").append(el("p", {}, `Data: ${M.source}${M.fetchedAt ? ", pulled " + M.fetchedAt : ""}. Built ${M.built}. Timestamps: ${M.tz}. `),
    el("p", {}, "Published from book2.js only; the local LSEG data tool is on the ", el("a", { href: "data.html", text: "Data page" }), "."));

  /* ------------------------------------------------------------------ theme */
  const modes = ["auto", "light", "dark"];
  let mode = "auto";
  try { mode = localStorage.getItem("hw2-theme") || "auto"; } catch (e) { /* storage blocked: fine */ }
  function applyTheme() {
    if (mode === "auto") document.documentElement.removeAttribute("data-theme"); else document.documentElement.setAttribute("data-theme", mode);
    $("#theme").textContent = "Theme: " + mode;
  }
  $("#theme").addEventListener("click", () => {
    mode = modes[(modes.indexOf(mode) + 1) % 3]; applyTheme();
    try { localStorage.setItem("hw2-theme", mode); } catch (e) { /* ignore */ }
    drawAll();
  });
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => drawAll());

  function drawAll() { drawLineCharts(); drawScatter(); }
  applyTheme(); renderPmccLedger(); drawAll();
  let rt; window.addEventListener("resize", () => { clearTimeout(rt); rt = setTimeout(drawAll, 120); });
})();
