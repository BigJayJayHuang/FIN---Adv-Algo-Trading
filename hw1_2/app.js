/* HW1.2 covered call - renders everything from window.BOOK (book.js). No libraries, no network. */
(function () {
  "use strict";
  const B = window.BOOK;
  if (!B) {
    document.body.textContent = "book.js is missing. Run: python3 build_book.py";
    return;
  }
  const S = B.summary, M = B.meta, L = B.ledger;
  const V = B.variant, VS = V.summary, VL = V.ledger;

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

  function table(host, cols, rows, rowClass) {
    host.textContent = "";
    const thead = el("thead", {}, el("tr", {}, cols.map(c => el("th", { class: c.cls || "", text: c.h }))));
    const tbody = el("tbody");
    for (const r of rows) {
      const tr = el("tr", { class: rowClass ? rowClass(r) || "" : "" });
      for (const c of cols) {
        const v = c.f(r);
        tr.append(el("td", { class: c.cls || "" }, v));
      }
      tbody.append(tr);
    }
    host.append(el("table", {}, thead, tbody));
  }

  /* --------------------------------------------------------------- headline */
  const first = L[0], last = L[L.length - 1];
  $("#title").textContent = "HW1.2 - Cover Calls";
  $("#subtitle").textContent = `${M.ticker}: long 100 shares, short 1 weekly call. ${md(first.d)} to ${md(last.d)}, ${first.d.slice(0, 4)}; ` +
    `${M.interval} bars for stock and options; Reg T margin; starting cash ${usd(B.config.startCash, 0)}. ` +
    `The main run is the assignment's basic version (nearest strike); my more aggressive ${V.label} version is compared under "My version".`;
  if (M.demo) $("#demo-banner").hidden = false;
  document.title = "HW1.2 - Cover Calls";

  /* ---------------------------------------------------------------- summary */
  const sold = B.blotter.filter(b => b.side === "SELL");
  $("#summary-lede").textContent =
    `${S.weeks} expiry Fridays. Weeks with a completed trade: ${S.traded} (${S.expired} expired worthless, ${S.assigned} assigned). ` +
    `Skipped: ${S.skipped}. My ${V.label} version ended at ${usd(VS.endNav, 0)} (${pct(VS.ret)}); see "My version" below.`;
  const tiles = [
    { cls: "hero-t", lab: "Ending NAV", val: usd(S.endNav, 0), note: `${pct(S.ret)} on ${usd(B.config.startCash, 0)} starting cash` },
    { lab: "Just holding the shares", val: pct(S.holdRet), note: "100 shares bought at the first entry, never sold" },
    { lab: "Premium collected", val: usd(S.premium, 0), note: `${sold.length} calls written, ${usd(S.premium / Math.max(1, sold.length), 0)} average` },
    { lab: "Weeks traded", val: String(S.traded), note: `${S.expired} expired · ${S.assigned} assigned · ${S.skipped} skipped` },
    { lab: "Lowest available funds", val: usd(S.minAvail, 0), note: S.minAvail >= 0 ? "never below zero: every trade could be put on" : "went negative: see the Reg T table" },
  ];
  $("#tiles").append(...tiles.map(t => el("div", { class: "card tile " + (t.cls || "") },
    el("div", { class: "lab", text: t.lab }), el("div", { class: "val", text: t.val }), el("div", { class: "note", text: t.note }))));

  /* ---------------------------------------------------------------- write-up */
  const fit = B.scatter.fit;
  const inside = (() => { const rows = B.scatter.rows; if (!rows.length) return null;
    return 100 * rows.filter(p => p[6] <= p[1] + 1e-9 && p[1] <= p[7] + 1e-9).length / rows.length; })();
  const beatBase = VS.endNav > S.endNav, beatHold = VS.ret > S.holdRet;
  const vsBase = beatBase ? "better than" : "worse than";
  const vsHold = beatHold ? "ahead of" : "behind";
  const wb = $("#writeup-body");
  wb.innerHTML = `
    <p><b>What I tested.</b> I own 100 shares of ${M.ticker} and every week I sell one call that expires that Friday. That's a covered call. The stock and the options both come from LSEG hourly bars, and the run covers ${S.weeks} weeks (${md(first.d)} to ${md(last.d)}). I start with ${usd(B.config.startCash, 0)} in cash and no shares. I ran it two ways: the basic version from the assignment, and my own more aggressive version. Everything is the same between the two except which strike I sell.</p>
    <ol class="rules">
      <li><b>Rule 1, entry.</b> Every Monday, at the first hourly bar at or after 10:00 ET (Tuesday if Monday is a holiday), if I'm flat I buy 100 shares at the stock price. Cash goes down.</li>
      <li><b>Rule 2, strike.</b> Basic version: the nearest strike at or above the stock price for that Friday (at the money if the stock sits exactly on a strike). My version: the first strike at or above ${(V.otmPct * 100).toFixed(0)}% over the stock price.</li>
      <li><b>Rule 3, fill.</b> I sell 1 call with a limit at mid, <span class="code">(BID+ASK)/2</span>, from the same hourly bar as the stock price. Cash goes up by 100 &times; mid. If that call has no bid or ask at that time, I skip the week. I never make up a price.</li>
      <li><b>Rule 4, wait through expiry.</b> I just wait. No rolling and no buying the call back. On Friday's last bar: if the stock is at or below the strike, the call expires and I keep the shares and the premium. If it's above, I'm assigned: my 100 shares are sold at the strike, cash goes up by 100 &times; strike, and I'm flat.</li>
    </ol>

    <h3>Why I picked ${M.ticker} and my strike</h3>
    <p>I'm bullish on Apple. There are new product release dates coming up, including the most anticipated iPhone drop, the "iPhone Duo". Around these dates a new product usually wins favor for the company, and that tends to push the stock price up. That's my view, not something this backtest can prove, since it only looks at the last ${S.weeks} weeks. But it's the reason I don't want to sell a call that caps my gains right at today's price.</p>
    <p>The basic rule sells the strike right above the stock price. It pays the most (about ${usd(S.avgPrem, 0)} per call), but the call is only about ${S.avgOtm.toFixed(1)}% out of the money, so almost any rally takes my shares away. That happened in ${S.assigned} of ${S.traded} weeks. I don't like giving up the upside that fast.</p>
    <p>So, since I expect ${M.ticker} to go up, in my version I sell a strike about ${(V.otmPct * 100).toFixed(0)}% higher. The premium is a lot smaller (about ${usd(VS.avgPrem, 0)} per call), but I keep most of a rally and only get assigned if the stock really runs: ${VS.assigned} of ${VS.traded} weeks. I'm taking more risk on purpose. Less premium means less cushion if ${M.ticker} falls (my worst drop from a peak was ${Math.abs(VS.maxDD).toFixed(1)}% versus ${Math.abs(S.maxDD).toFixed(1)}% for the basic version), but if it goes up I go up with it. It's still a mechanical rule that only uses the stock price at the time of the order.</p>
    <p>In this run my version ended at ${usd(VS.endNav, 0)} (${pct(VS.ret)}). The basic version ended at ${usd(S.endNav, 0)} (${pct(S.ret)}) and just holding the shares was ${pct(S.holdRet)}. So mine did ${vsBase} the basic version and finished ${vsHold} plain buy-and-hold. It's only ${S.weeks} weeks, though, so I wouldn't read too much into it.</p>

    <h3>Waiting through expiry</h3>
    <p>I don't touch the position. If the call expires worthless I keep the shares and just sell the next Friday call on Monday. If I'm assigned I'm flat, so the next Monday I start over with a buy and a sale. Cash only moves when there's a blotter row: buying stock, collecting premium, expiring (zero), and assignment at the strike.</p>

    <h3>Fill at mid</h3>
    <p>Both legs are priced from the same hourly bar, so the stock and the option are at the same time. Mid is a bit generous to me as the seller, since a real fill would probably land somewhere between the bid and the mid. To check that it's reasonable, the Mid vs trade chart compares each call's actual trade price to its mid in the same bar. ${fit ? `Across ${fit.n.toLocaleString()} bars the fit is trade = ${fit.intercept.toFixed(3)} + ${fit.slope.toFixed(3)} &times; mid with R&sup2; = ${fit.r2.toFixed(4)}` : "There weren't enough trades to fit a line"}${inside != null ? `, and ${inside.toFixed(0)}% of those trades were at or inside the quoted bid/ask` : ""}. If there's no bid or ask, there's no fill.</p>

    <h3>Why Reg T</h3>
    <p>Portfolio margin needs a broker's risk engine and I don't have one. I can work Reg T out by hand from the ledger: the initial requirement is 50% of the stock's value (a covered call adds nothing), maintenance is 25%, available funds are NAV minus initial, and excess is NAV minus maintenance. If available funds went negative I couldn't have put the trade on. ${Math.min(S.minAvail, VS.minAvail) >= 0 ? `That never happened here: the lowest was ${usd(S.minAvail, 0)} for the basic version and ${usd(VS.minAvail, 0)} for mine.` : `It did happen here: the lowest was ${usd(S.minAvail, 0)} for the basic version and ${usd(VS.minAvail, 0)} for mine, see the Reg T table.`} A book that works under Reg T also works under portfolio margin, but not the other way around.</p>

    <h3>Simplifications</h3>
    <p>No commissions or fees. No early assignment and no dividends. I only check whether the call is in the money at Friday's last regular-session bar, using that bar's stock price as the close. Only regular-session bars are used. Each hourly bar is stamped at its start, but the stock price and the option quote are both the last values in that bar, so they line up.</p>`;

  /* ---------------------------------------------------------------- blotter */
  const blotterCols = [
    { h: "#", cls: "n", f: r => r.n },
    { h: "Time (ET)", f: r => r.lbl },
    { h: "Instrument", f: r => r.instr },
    { h: "Side", f: r => chip(r.side) },
    { h: "Qty", cls: "n", f: r => r.qty },
    { h: "Limit", cls: "n", f: r => r.limit == null ? "—" : num(r.limit, 3) },
    { h: "Fill", cls: "n", f: r => r.side === "EXPIRE" ? "0.000" : num(r.fill, r.side === "SELL" ? 3 : 2) },
    { h: "Bid / Ask", cls: "n", f: r => r.bid == null ? "—" : `${num(r.bid)} / ${num(r.ask)}` },
    { h: "Cash Δ", cls: "n", f: r => el("span", { class: r.dcash < 0 ? "neg" : "", text: usd(r.dcash) }) },
    { h: "Cash after", cls: "n", f: r => usd(r.cash) },
    { h: "Shares after", cls: "n", f: r => r.shares },
    { h: "Avail. funds", cls: "n", f: r => usd(r.avail, 0) },
    { h: "Note (rule)", cls: "wrapc", f: r => el("span", {}, el("b", { text: r.rule + ". " }), r.note) },
  ];
  const blotterRow = r => (r.side === "ASSIGN" || r.side === "EXPIRE") ? "ev" : "";
  table($("#blotter-tbl"), blotterCols, B.blotter, blotterRow);

  /* ------------------------------------------------------------------ weeks */
  table($("#weeks-tbl"), [
    { h: "Wk", cls: "n", f: r => r.week },
    { h: "Expiry", f: r => md(r.expiry) },
    { h: "Entry (ET)", f: r => r.entry_lbl || "—" },
    { h: "Spot", cls: "n", f: r => num(r.spot) },
    { h: "Strike", cls: "n", f: r => r.strike == null ? "—" : `${num(r.strike, r.strike % 1 ? 1 : 0)}` + (r.spot ? ` (${pct((r.strike / r.spot - 1) * 100)})` : "") },
    { h: "Bid / Ask", cls: "n", f: r => r.bid == null ? "—" : `${num(r.bid)} / ${num(r.ask)}` },
    { h: "Mid → premium", cls: "n", f: r => r.mid == null ? "—" : `${num(r.mid, 3)} → ${usd(r.mid * 100)}` },
    { h: "Stock at expiry", cls: "n", f: r => num(r.S_T) },
    { h: "Outcome", f: r => chip(r.outcome || "—") },
    { h: "NAV start → end", cls: "n", f: r => r.nav_start == null ? "—" : `${usd(r.nav_start, 0)} → ${usd(r.nav_end, 0)}` },
  ], B.weeks);
  const sk = $("#skips");
  if (!B.skips.length) sk.hidden = true;
  for (const s of B.skips) sk.append(el("li", { text: `Week ${s.week} skipped: ${s.reason}.` + (s.holding ? " I already held the shares, so they sat uncovered that week." : " I stayed flat.") }));

  /* ------------------------------------------------------- ledger + Reg T */
  let rowsMode = "daily";
  function visibleRows(list) {
    list = list || L;
    if (rowsMode === "all") return list;
    return list.filter((r, i) => r.event || i === list.length - 1 || list[i + 1].d !== r.d);
  }
  function renderLedger() {
    const rows = visibleRows();
    renderMineLedger();
    table($("#ledger-tbl"), [
      { h: "Time (ET)", f: r => r.lbl },
      { h: "Stock", cls: "n", f: r => num(r.S) },
      { h: "Shares", cls: "n", f: r => r.sh },
      { h: "Short call", f: r => r.call ? `${num(r.call.k, r.call.k % 1 ? 1 : 0)}C exp ${md(r.call.exp)}` : "—" },
      { h: "Call mark", cls: "n", f: r => r.call ? el("span", { title: r.call.stale ? "no quote this bar: last mid carried forward" : "", text: num(r.call.mark, 3) + (r.call.stale ? "*" : "") }) : "—" },
      { h: "Cash", cls: "n", f: r => usd(r.cash) },
      { h: "Stock MV", cls: "n", f: r => usd(r.stockMV) },
      { h: "Option MV", cls: "n", f: r => usd(r.optMV) },
      { h: "NAV", cls: "n", f: r => usd(r.nav) },
      { h: "Event", f: r => r.event ? chip(r.event === "OPEN" ? "SELL" : r.event) : "" },
    ], rows, r => r.event ? "ev" : "");
    table($("#regt-tbl"), [
      { h: "Time (ET)", f: r => r.lbl },
      { h: "Stock LMV", cls: "n", f: r => usd(r.lmv) },
      { h: "Initial (50%)", cls: "n", f: r => usd(r.init) },
      { h: "Maint. (25%)", cls: "n", f: r => usd(r.maint) },
      { h: "NAV", cls: "n", f: r => usd(r.nav) },
      { h: "Available funds", cls: "n", f: r => el("span", { class: r.avail < 0 ? "neg" : "", text: usd(r.avail) }) },
      { h: "Excess", cls: "n", f: r => el("span", { class: r.excess < 0 ? "neg" : "", text: usd(r.excess) }) },
    ], rows, r => r.event ? "ev" : "");
  }
  document.querySelectorAll("[data-rows]").forEach(b => b.addEventListener("click", () => {
    rowsMode = b.dataset.rows;
    document.querySelectorAll("[data-rows]").forEach(x => x.setAttribute("aria-pressed", String(x === b)));
    renderLedger();
  }));
  $("#regt-note").append(
    el("p", { style: "margin:0 0 8px" }, el("b", { text: "Reading the table. " }), "A covered call adds nothing to the initial requirement, so it moves with the stock only. When I'm flat (after an assignment) every requirement is $0 and available funds equal NAV."),
    el("p", { style: "margin:0", class: "muted" }, `Lowest available funds: ${usd(S.minAvail)}. ` + (S.minAvail >= 0 ? "Every trade could be put on." : "Negative rows mean the trade could not have been put on.")));

  /* ------------------------------------------------------------ line charts */
  function niceStep(raw) { const p = Math.pow(10, Math.floor(Math.log10(raw))); const f = raw / p; return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 2.5 ? 2.5 : f <= 5 ? 5 : 10) * p; }
  function niceTicks(lo, hi, n) {
    if (hi === lo) { hi = lo + 1; }
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
    const pad = (hi - lo) * 0.06 || 1; if (!o.zero) { lo -= pad; } hi += pad;
    const ticks = niceTicks(lo, hi, 5); lo = ticks[0]; hi = ticks[ticks.length - 1];
    const X = i => m.l + (n <= 1 ? 0 : i / (n - 1)) * iw, Y = v => m.t + ih - (v - lo) / (hi - lo) * ih;

    const svg = sv("svg", { viewBox: `0 0 ${W} ${H}`, width: W, height: H });
    // week bands + x ticks
    const starts = []; let prev = null;
    o.rows.forEach((r, i) => { const k = mondayOf(r.d); if (k !== prev) { starts.push(i); prev = k; } });
    starts.forEach((s, j) => {
      const e = j + 1 < starts.length ? starts[j + 1] : n - 1;
      if (j % 2 === 1) svg.append(sv("rect", { x: X(s), y: m.t, width: Math.max(0, X(e) - X(s)), height: ih, style: "fill:var(--band)" }));
    });
    for (const t of ticks) {
      svg.append(sv("line", { x1: m.l, x2: W - m.r, y1: Y(t), y2: Y(t), style: "stroke:var(--grid);stroke-width:1" }));
      svg.append(sv("text", { x: m.l - 8, y: Y(t) + 4, "text-anchor": "end", class: "axis-t" }, usd(t, 0)));
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

  const callTxt = r => r.call ? `${num(r.call.k, r.call.k % 1 ? 1 : 0)}C exp ${md(r.call.exp)} @ ${num(r.call.mark, 3)}${r.call.stale ? "*" : ""}` : "none";
  const evTxt = r => r.event ? " · " + (r.event === "OPEN" ? "call sold" : r.event === "SKIP" ? "week skipped" : r.event.toLowerCase()) : "";

  function drawLineCharts() {
    const c1 = css("--s1"), c2 = css("--s2"), c3 = css("--s3");
    legend($("#lg-nav"), [{ label: "NAV (covered call book)", color: c1 }, { label: "Buy and hold 100 shares", color: c3 }]);
    lineChart($("#ch-nav"), {
      rows: L, height: 320, aria: "Line chart of NAV over time compared with buy and hold",
      series: [{ get: r => r.nav, color: c1 }, { get: r => r.hold, color: c3 }],
      tip: r => ({ head: r.lbl + evTxt(r), rows: [[c1, "NAV", usd(r.nav)], [c3, "Buy and hold", usd(r.hold)], null,
        [null, "Stock", usd(r.S)], [null, "Shares", String(r.sh)], [null, "Short call", callTxt(r)], [null, "Cash", usd(r.cash)]] }),
    });
    legend($("#lg-mine"), [{ label: "Basic (nearest strike)", color: c1 }, { label: `My version (${V.label})`, color: c2 }, { label: "Buy and hold 100 shares", color: c3 }]);
    const cr = L.map((r, i) => ({ d: r.d, lbl: r.lbl, b: r, m: VL[i] || r }));
    lineChart($("#ch-mine"), {
      rows: cr, height: 320, aria: "Line chart of NAV for the basic covered call, my more aggressive version, and buy and hold",
      series: [{ get: r => r.b.nav, color: c1 }, { get: r => r.m.nav, color: c2 }, { get: r => r.b.hold, color: c3 }],
      tip: r => ({ head: r.lbl + (r.m.event === "OPEN" ? " \u00b7 calls sold" : r.m.event === "ASSIGN" || r.m.event === "EXPIRE" ? " \u00b7 expiry" : ""),
        rows: [[c1, "Basic NAV", usd(r.b.nav)], [c2, "My NAV", usd(r.m.nav)], [c3, "Buy and hold", usd(r.b.hold)], null,
          [null, "Stock", usd(r.b.S)], [null, "Basic call", callTxt(r.b)], [null, "My call", callTxt(r.m)],
          [null, "My available funds", usd(r.m.avail)]] }),
    });
    legend($("#lg-margin"), [{ label: "NAV", color: c1 }, { label: "Initial requirement (50% of stock LMV)", color: c2 }, { label: "Maintenance requirement (25%)", color: c3 }]);
    lineChart($("#ch-margin"), {
      rows: L, height: 300, zero: true, aria: "Line chart of NAV against Reg T initial and maintenance requirements",
      series: [{ get: r => r.nav, color: c1 }, { get: r => r.init, color: c2 }, { get: r => r.maint, color: c3 }],
      tip: r => ({ head: r.lbl + evTxt(r), rows: [[c1, "NAV", usd(r.nav)], [c2, "Initial (50%)", usd(r.init)], [c3, "Maintenance (25%)", usd(r.maint)], null,
        [null, "Available funds", usd(r.avail)], [null, "Excess", usd(r.excess)], [null, "Stock LMV", usd(r.lmv)], [null, "Short call", callTxt(r)]] }),
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
  function filteredPts() {
    const money = +$("#f-money").value, mm = +$("#f-mid").value;
    return B.scatter.rows.filter(p => p[0] >= mm && (p[5] == null || Math.abs(p[4] / p[5] - 1) <= money));
  }
  function drawScatter() {
    const host = $("#ch-sc"); host.textContent = "";
    const pts = filteredPts(), f = regress(pts);
    const c1 = css("--s1"), c2 = css("--s2");
    legend($("#lg-sc"), [{ label: "Bars with a real bid/ask and a trade", color: c1, type: "dt" },
      { label: "Least-squares fit", color: c2 }, { label: "y = x (trade equals mid)", color: css("--ink-3") }]);
    const stat = $("#sc-stat"); stat.textContent = "";
    if (!pts.length) { host.append(el("p", { class: "muted", text: "No points match this filter." })); return; }
    const insidePct = 100 * pts.filter(p => p[6] <= p[1] + 1e-9 && p[1] <= p[7] + 1e-9).length / pts.length;
    if (f) stat.append(el("span", {}, "n = ", el("b", { text: f.n.toLocaleString() })),
      el("span", {}, "trade = ", el("b", { text: f.b0.toFixed(3) }), " + ", el("b", { text: f.b1.toFixed(3) }), " × mid"),
      el("span", {}, "R² = ", el("b", { text: f.r2.toFixed(4) })),
      el("span", {}, "trades inside the bid/ask: ", el("b", { text: insidePct.toFixed(0) + "%" })));
    const W = Math.max(240, host.clientWidth), side = Math.min(W, 560), m = { l: 56, r: 12, t: 10, b: 40 };
    const H = side + m.t + m.b - 0, ox = Math.max(0, (W - side) / 2);
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
  $("#f-money").addEventListener("change", drawScatter);
  $("#f-mid").addEventListener("change", drawScatter);
  $("#dl-csv").addEventListener("click", e => {
    const rows = filteredPts();
    const csv = "mid,trdprc_1,ric,time_et,strike,spot,bid,ask\n" + rows.map(p => p.map(v => v == null ? "" : v).join(",")).join("\n");
    e.currentTarget.href = URL.createObjectURL(new Blob([csv], { type: "text/csv" }));
    e.currentTarget.download = `${M.ticker}_mid_vs_trade.csv`;
  });

  /* ------------------------------------------------------------ my version */
  $("#mine-name").textContent = V.label;
  $("#mine-lede").textContent = `Same data, same fills, same rules. The only thing I changed is the strike: instead of the nearest strike above the stock, I sell the first strike at or above spot × ${(1 + V.otmPct).toFixed(2)}. It's a more aggressive bet: less premium, more upside kept, and less cushion if the stock drops.`;
  const cmpRows = [
    ["Ending NAV", usd(S.endNav, 0), usd(VS.endNav, 0)],
    ["Return", pct(S.ret), pct(VS.ret)],
    ["Worst drop from a peak", pct(S.maxDD), pct(VS.maxDD)],
    ["Premium collected", usd(S.premium, 0), usd(VS.premium, 0)],
    ["Average premium per call", usd(S.avgPrem, 0), usd(VS.avgPrem, 0)],
    ["Average strike above spot", pct(S.avgOtm), pct(VS.avgOtm)],
    ["Weeks assigned", `${S.assigned} of ${S.traded}`, `${VS.assigned} of ${VS.traded}`],
    ["Weeks expired worthless", `${S.expired} of ${S.traded}`, `${VS.expired} of ${VS.traded}`],
    ["Weeks skipped", String(S.skipped), String(VS.skipped)],
    ["Shares held at the end", String(S.endShares), String(VS.endShares)],
    ["Lowest available funds", usd(S.minAvail, 0), usd(VS.minAvail, 0)],
    ["Just holding 100 shares (return)", pct(S.holdRet), pct(S.holdRet)],
  ];
  table($("#cmp-tbl"), [
    { h: "", f: r => r[0] },
    { h: "Basic (nearest strike)", cls: "n", f: r => r[1] },
    { h: `My version (${V.label})`, cls: "n", f: r => r[2] },
  ], cmpRows);
  const strikeTxt = w => w.strike == null ? "—" : `${num(w.strike, w.strike % 1 ? 1 : 0)} (${pct((w.strike / w.spot - 1) * 100, 1)})`;
  table($("#cmp-weeks"), [
    { h: "Wk", cls: "n", f: (r, i) => r.b.week },
    { h: "Entry (ET)", f: r => r.b.entry_lbl || "—" },
    { h: "Spot", cls: "n", f: r => num(r.b.spot) },
    { h: "Basic strike", cls: "n", f: r => strikeTxt(r.b) },
    { h: "Basic premium", cls: "n", f: r => r.b.mid == null ? "—" : usd(r.b.mid * 100) },
    { h: "Basic result", f: r => chip(r.b.outcome || "—") },
    { h: "My strike", cls: "n", f: r => strikeTxt(r.m) },
    { h: "My premium", cls: "n", f: r => r.m.mid == null ? "—" : usd(r.m.mid * 100) },
    { h: "My result", f: r => chip(r.m.outcome || "—") },
    { h: "Stock at expiry", cls: "n", f: r => num(r.b.S_T) },
  ], B.weeks.map((w, i) => ({ b: w, m: V.weeks[i] })));
  const vsk = $("#mine-skips");
  if (!V.skips.length) vsk.hidden = true;
  for (const s2 of V.skips) vsk.append(el("li", { text: `My version, week ${s2.week} skipped: ${s2.reason}.` }));
  table($("#mine-blotter"), blotterCols, V.blotter, blotterRow);
  function renderMineLedger() {
    table($("#mine-ledger"), [
      { h: "Time (ET)", f: r => r.lbl },
      { h: "Stock", cls: "n", f: r => num(r.S) },
      { h: "Shares", cls: "n", f: r => r.sh },
      { h: "Short call", f: r => r.call ? `${num(r.call.k, r.call.k % 1 ? 1 : 0)}C exp ${md(r.call.exp)}` : "—" },
      { h: "Cash", cls: "n", f: r => usd(r.cash) },
      { h: "NAV", cls: "n", f: r => usd(r.nav) },
      { h: "Initial (50%)", cls: "n", f: r => usd(r.init) },
      { h: "Maint. (25%)", cls: "n", f: r => usd(r.maint) },
      { h: "Available", cls: "n", f: r => el("span", { class: r.avail < 0 ? "neg" : "", text: usd(r.avail) }) },
      { h: "Excess", cls: "n", f: r => el("span", { class: r.excess < 0 ? "neg" : "", text: usd(r.excess) }) },
    ], visibleRows(VL), r => r.event ? "ev" : "");
  }

  /* --------------------------------------------------------- checks / cover */
  const cl = $("#checks-list");
  for (const c of B.checks.concat(V.checks)) cl.append(el("li", {}, el("span", { class: "badge " + (c.ok ? "ok" : "bad"), text: c.ok ? "✓ PASS" : "✕ FAIL" }),
    el("span", {}, el("b", { text: c.name }), el("span", { class: "muted", text: " — " + c.detail }))));
  const entryOk = w => { const r = B.weeks.find(x => x.week === w.week); return r && r.outcome === "SKIP" ? "skipped" : "traded"; };
  table($("#cov-tbl"), [
    { h: "Wk", cls: "n", f: r => r.week }, { h: "Expiry", f: r => md(r.expiry) },
    { h: "Option RICs with data", cls: "n", f: r => r.contracts },
    { h: "Strikes", cls: "n", f: r => r.strikes ? `${r.strikes[0]} – ${r.strikes[1]}` : "—" },
    { h: "Bars", cls: "n", f: r => r.bars.toLocaleString() },
    { h: "Quoted", cls: "n", f: r => r.bars ? `${r.quoted.toLocaleString()} (${Math.round(100 * r.quoted / r.bars)}%)` : "—" },
    { h: "Traded", cls: "n", f: r => r.bars ? `${r.traded.toLocaleString()} (${Math.round(100 * r.traded / r.bars)}%)` : "—" },
    { h: "Week result", f: r => entryOk(r) },
  ], B.coverage);
  $("#foot").append(el("p", {}, `Data: ${M.source}${M.fetchedAt ? ", pulled " + M.fetchedAt : ""}. Built ${M.built}. Timestamps: ${M.tz}. `),
    el("p", {}, "Published from book.js only; the local LSEG data tool is on the ", el("a", { href: "data.html", text: "Data page" }), "."));

  /* ------------------------------------------------------------------ theme */
  const modes = ["auto", "light", "dark"];
  let mode = "auto";
  try { mode = localStorage.getItem("hw12-theme") || "auto"; } catch (e) { /* storage blocked: fine */ }
  function applyTheme() {
    if (mode === "auto") document.documentElement.removeAttribute("data-theme"); else document.documentElement.setAttribute("data-theme", mode);
    $("#theme").textContent = "Theme: " + mode;
  }
  $("#theme").addEventListener("click", () => {
    mode = modes[(modes.indexOf(mode) + 1) % 3]; applyTheme();
    try { localStorage.setItem("hw12-theme", mode); } catch (e) { /* ignore */ }
    drawAll();
  });
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => drawAll());

  function drawAll() { drawLineCharts(); drawScatter(); }
  applyTheme(); renderLedger(); drawAll();
  let rt; window.addEventListener("resize", () => { clearTimeout(rt); rt = setTimeout(drawAll, 120); });
})();
