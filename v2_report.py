#!/usr/bin/env python3
"""
v2_report.py -- results page for the V2 study (deploy/v2.html -> /v2).

    python v2_report.py

Reads results/v2/{summary.csv,selection.json} and the per-run trade ledgers.
Self-contained HTML, inline SVG, no external libraries, light + dark.
"""

from __future__ import annotations

import json
import os
import sys

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
R = os.path.join(HERE, "results", "v2")
ORD = (("adverse-first", ""), ("favourable-first", "_favfirst"))


def closed(path: str) -> pd.DataFrame:
    try:
        t = pd.read_csv(path, parse_dates=["entry_datetime", "exit_datetime"])
    except pd.errors.EmptyDataError:
        return pd.DataFrame()
    return t[t["exit_reason"] != "DATASET_END"].sort_values("exit_datetime")


def main() -> int:
    s = pd.read_csv(os.path.join(R, "summary.csv"))
    sel = json.load(open(os.path.join(R, "selection.json")))

    # 1. V1 cumulative net across TRAIN then VALIDATE
    equity = []
    for o, sfx in ORD:
        pts, cum = [], 0.0
        for w in ("TRAIN", "VALIDATE"):
            t = closed(os.path.join(R, w, f"V1{sfx}", "trades.csv"))
            for r in t.itertuples():
                cum += r.net_pnl
                pts.append([r.exit_datetime.strftime("%Y-%m-%d"), round(cum)])
        equity.append({"name": o, "points": pts})

    # 2. where the money goes: V1 TRAIN (canonical ordering) net by exit reason
    m = json.load(open(os.path.join(R, "TRAIN", "V1", "metrics.json")))
    reasons = [{"reason": k, "net": round(v), "count": m["counts_by_exit_reason"][k]}
               for k, v in sorted(m["net_by_exit_reason"].items(), key=lambda x: -x[1])]
    gross, costs = m["total_gross_pnl"], m["total_transaction_costs"]

    # 3. early-stop width (V1 = 60, H4 family) vs net, both windows
    fam = [("V1", 60), ("H4_75", 75), ("H4_90", 90), ("H4_120", 120), ("H4_150", 150)]
    g = lambda w, v, o, c: float(s[(s.window == w) & (s.variant == v) & (s.ordering == o)].iloc[0][c])
    stop = {w: [{"name": o, "points": [[x, round(g(w, v, o, "net"))] for v, x in fam]} for o, _ in ORD]
            for w in ("TRAIN", "VALIDATE")}

    # 4. table
    rows = []
    for v in s.variant.unique():
        r = {"variant": v}
        for w in ("TRAIN", "VALIDATE"):
            for o, _ in ORD:
                k = "adv" if o == "adverse-first" else "fav"
                r[f"{w}_{k}_net"] = round(g(w, v, o, "net"))
                r[f"{w}_{k}_dd"] = round(g(w, v, o, "max_dd"))
        pv = sel["per_variant"].get(v)
        r["rule"] = None if pv is None else {"a": pv["a_validate_beats_v1_net_and_dd"],
                                              "b": pv["b_neighbours_beat_v1"], "c": pv["c_train_not_worse"],
                                              "promoted": pv["promoted"]}
        rows.append(r)

    v1 = {o: {w: {"net": g(w, "V1", o, "net"), "pf": g(w, "V1", o, "profit_factor"),
                  "trades": int(g(w, "V1", o, "trades"))} for w in ("TRAIN", "VALIDATE")} for o, _ in ORD}
    data = {"equity": equity, "reasons": reasons, "gross": round(gross), "costs": round(costs),
            "stop": stop, "rows": rows, "v1": v1, "promoted": sel["promoted"],
            "final_pick": sel.get("final_pick")}
    out = os.path.join(HERE, "deploy", "v2.html")
    with open(out, "w") as fh:
        fh.write(TEMPLATE.replace("__DATA__", json.dumps(data, separators=(",", ":"))))
    print(f"wrote {out} ({os.path.getsize(out) / 1e3:.0f} kB)")
    return 0


TEMPLATE = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>V2 Research Results</title>
<meta name="description" content="NIFTY 180-point swing: pre-registered V2 study on a 3-year spot proxy">
<style>
:root{
  color-scheme:light;
  --page:#f9f9f7; --surface-1:#fcfcfb; --ink:#0b0b0b; --ink-2:#52514e; --muted:#898781;
  --grid:#e1e0d9; --axis:#c3c2b7; --ring:rgba(11,11,11,.10);
  --series-1:#2a78d6; --series-2:#eb6834; --pos:#2a78d6; --neg:#e34948; --mid:#f0efec;
  --good:#006300;
}
@media (prefers-color-scheme: dark){
  :root:where(:not([data-theme="light"])){
    color-scheme:dark;
    --page:#0d0d0d; --surface-1:#1a1a19; --ink:#ffffff; --ink-2:#c3c2b7; --muted:#898781;
    --grid:#2c2c2a; --axis:#383835; --ring:rgba(255,255,255,.10);
    --series-1:#3987e5; --series-2:#d95926; --pos:#3987e5; --neg:#e66767; --mid:#383835;
    --good:#0ca30c;
  }
}
:root[data-theme="dark"]{
  color-scheme:dark;
  --page:#0d0d0d; --surface-1:#1a1a19; --ink:#ffffff; --ink-2:#c3c2b7; --muted:#898781;
  --grid:#2c2c2a; --axis:#383835; --ring:rgba(255,255,255,.10);
  --series-1:#3987e5; --series-2:#d95926; --pos:#3987e5; --neg:#e66767; --mid:#383835;
  --good:#0ca30c;
}
*{box-sizing:border-box}
body{margin:0;background:var(--page);color:var(--ink);font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
.wrap{max-width:1080px;margin:0 auto;padding:24px 16px 48px}
h1{font-size:24px;margin:0 0 4px;letter-spacing:-.01em}
h2{font-size:17px;margin:36px 0 4px}
.lede{color:var(--ink-2);margin:0 0 20px;max-width:760px}
.sub{color:var(--ink-2);margin:0 0 12px;max-width:760px;font-size:14px}
.card{background:var(--surface-1);border:1px solid var(--ring);border-radius:12px;padding:16px}
.verdict{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px;margin:8px 0 4px}
.tile .k{color:var(--ink-2);font-size:13px}
.tile .v{font-size:26px;font-weight:650;margin-top:2px;letter-spacing:-.01em}
.tile .n{color:var(--muted);font-size:12.5px;margin-top:2px}
.legend{display:flex;gap:18px;flex-wrap:wrap;font-size:13px;color:var(--ink-2);margin:0 0 6px}
.sw{display:inline-block;width:14px;height:3px;border-radius:2px;margin-right:6px;vertical-align:middle}
svg{display:block;width:100%;height:auto;overflow:visible}
svg text{fill:var(--muted);font-size:11px;font-variant-numeric:tabular-nums}
svg .lbl{fill:var(--ink-2);font-size:12px}
.two{display:grid;grid-template-columns:1fr;gap:12px}
@media(min-width:760px){.two{grid-template-columns:1fr 1fr}}
.tip{position:fixed;pointer-events:none;background:var(--surface-1);color:var(--ink);border:1px solid var(--ring);
  border-radius:8px;padding:8px 10px;font-size:12.5px;box-shadow:0 6px 24px rgba(0,0,0,.18);display:none;z-index:5;max-width:280px}
.tip b{font-variant-numeric:tabular-nums}
.tscroll{overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:13px;font-variant-numeric:tabular-nums}
th,td{padding:6px 8px;border-bottom:1px solid var(--grid);text-align:right;white-space:nowrap}
th{color:var(--ink-2);font-weight:600} th:first-child,td:first-child{text-align:left}
td.neg{color:var(--neg)} td.pos{color:var(--good)}
.chip{display:inline-block;padding:1px 7px;border-radius:6px;font-size:11.5px;border:1px solid var(--ring);color:var(--ink-2)}
ul{margin:8px 0;padding-left:20px} li{margin:6px 0}
.note{color:var(--muted);font-size:12.5px;margin-top:8px}
a{color:var(--series-1)}
</style></head>
<body><div class="wrap">
<h1>V2 research: can the model be improved?</h1>
<p class="lede">Six pre-registered fixes were tested against V1 on 2¼ years of NIFTY data (a spot proxy for futures),
with the selection rule fixed and committed before any run. <b>None passed.</b> V1 stays the only model, and the holdout stays
unopened. The bigger finding is V1 itself, shown first.</p>

<div class="verdict" id="tiles"></div>

<h2>V1 over the long run</h2>
<p class="sub">Cumulative net P&amp;L after costs, trade by trade, Sep 2023 → Dec 2025. The June–August 2026 in-sample you saw before (+₹3.1 L) was a good stretch, not the norm.</p>
<div class="card"><div class="legend" id="eqlegend"></div><svg id="eq" viewBox="0 0 1000 340" role="img" aria-label="V1 cumulative net P&L"></svg></div>

<h2>Where the money goes</h2>
<p class="sub">V1 on TRAIN (canonical adverse-first ordering), net ₹ by how each trade ended. The trend-following exit earns;
the protection machinery (early stop + reverse, breaker, gap exit) gives back more than that.</p>
<div class="card"><svg id="rsn" viewBox="0 0 1000 230" role="img" aria-label="Net by exit reason"></svg>
<div class="note" id="grossnote"></div></div>

<h2>The one clear signal: the 60-point early stop</h2>
<p class="sub">Net ₹ as the early stop (and arm level) widens from V1's 60 points. On TRAIN, wider was better at nearly every step. On VALIDATE
it was not, so under the rule it's not promoted. It is the lead for the next study, not a fix.</p>
<div class="two">
  <div class="card"><div class="legend" id="stlegend"></div><div class="sub" style="margin:0">TRAIN · 438 sessions</div><svg id="stT" viewBox="0 0 480 260"></svg></div>
  <div class="card"><div class="legend" id="stlegend2"></div><div class="sub" style="margin:0">VALIDATE · 126 sessions</div><svg id="stV" viewBox="0 0 480 260"></svg></div>
</div>

<h2>Every variant against the rule</h2>
<p class="sub">Promoted only if, on VALIDATE and under both intra-minute orderings, it beats V1 on net <b>and</b> drawdown (a), its grid neighbours
beat V1 too (b), and it is not worse than V1 on TRAIN (c). S150/S210/S240 and the neighbours are reference runs, never promotable.</p>
<div class="card tscroll"><table id="tbl"></table></div>

<h2>What this taught us</h2>
<div class="card"><ul>
<li><b>The strategy loses before costs over 2023–25</b> on this proxy (profit factor 0.62–0.73 over 310+ trades). Costs make it worse; they are not the cause.</li>
<li><b>The 60-point early stop is the main drain.</b> NIFTY ranged more than 60 points on 97% of days, so the stop is inside the everyday noise:
it fires, reverses, and the reversal gets stopped too. Widening it improved TRAIN at nearly every step (one small dip, under one ordering).</li>
<li><b>The breaker helps.</b> Removing it (H3) made losses much larger, not smaller.</li>
<li><b>Re-entry chains are not the problem.</b> FLAT_ONLY (H2) changed little.</li>
<li><b>Distances as % of the index (H1)</b> helped on VALIDATE but not TRAIN. It's not robust, so it's not adopted.</li>
</ul>
<p class="note">Caveats: futures = spot in this proxy, so there's no basis P&amp;L and no futures-confirmation filter, costs are on spot notional, and there's no expiry roll.
Against 55 real-futures sessions it matched the trade count within 5% but found only 5 of 8 fresh entries and a third of the rupees. Read the comparisons
between variants, not the absolute rupees. Pre-registration and results: <a href="https://github.com/prarabdha-soni/nifty_180_point/blob/main/V2_RESEARCH.md">V2_RESEARCH.md</a>.</p></div>
</div>
<div class="tip" id="tip"></div>
<script>
const D = __DATA__;
const css = v => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
const lakh = v => (v < 0 ? "−" : "") + "₹" + (Math.abs(v) / 1e5).toFixed(1) + " L";
const inr = v => (v < 0 ? "−" : "") + "₹" + Math.abs(Math.round(v)).toLocaleString("en-IN");
const tip = document.getElementById("tip");
const show = (h, e) => { tip.innerHTML = h; tip.style.display = "block"; const r = tip.getBoundingClientRect();
  tip.style.left = Math.min(e.clientX + 14, innerWidth - r.width - 8) + "px"; tip.style.top = Math.min(e.clientY + 14, innerHeight - r.height - 8) + "px"; };
const hide = () => tip.style.display = "none";
const NS = "http://www.w3.org/2000/svg";
const el = (tag, at, parent) => { const n = document.createElementNS(NS, tag); for (const k in at) n.setAttribute(k, at[k]); parent && parent.appendChild(n); return n; };
const SER = ["--series-1", "--series-2"];
function niceTicks(lo, hi, n = 5) {   // ticks that COVER [lo, hi]: first <= lo, last >= hi
  const span = (hi - lo) || 1, step0 = span / n, mag = Math.pow(10, Math.floor(Math.log10(step0)));
  const step = [1, 2, 2.5, 5, 10].map(m => m * mag).find(s => s >= step0); const out = [];
  for (let v = Math.floor(lo / step) * step; v < hi + step - 1e-9; v += step) out.push(Math.round(v)); return out; }
function legend(id, names) { document.getElementById(id).innerHTML = names.map((n, i) => `<span><span class="sw" style="background:var(${SER[i]})"></span>${n}</span>`).join(""); }

// tiles
(() => { const a = D.v1["adverse-first"].TRAIN, f = D.v1["favourable-first"].TRAIN;
  const tiles = [["Hypotheses promoted", `${D.promoted.length} of 6`, "holdout not opened"],
    ["V1 net, TRAIN", `${lakh(a.net)} / ${lakh(f.net)}`, "Sep 2023 – Jun 2025 · adverse / favourable ordering"],
    ["V1 profit factor, TRAIN", `${a.pf.toFixed(2)} / ${f.pf.toFixed(2)}`, "below 1 = losing"],
    ["V1 trades, TRAIN", `${a.trades} / ${f.trades}`, "438 sessions"]];
  document.getElementById("tiles").innerHTML = tiles.map(([k, v, n]) => `<div class="card tile"><div class="k">${k}</div><div class="v">${v}</div><div class="n">${n}</div></div>`).join(""); })();

// 1. equity (time axis)
function lineTime(svg, series) {
  const W = 1000, H = 340, L = 64, R = 96, T = 12, B = 30; svg.innerHTML = "";
  const all = series.flatMap(s => s.points), ts = all.map(p => Date.parse(p[0])), ys = all.map(p => p[1]).concat([0]);
  const x0 = Math.min(...ts), x1 = Math.max(...ts), yt = niceTicks(Math.min(...ys), Math.max(...ys)), y0 = yt[0], y1 = yt[yt.length - 1];
  const X = t => L + (W - L - R) * (t - x0) / (x1 - x0), Y = v => T + (H - T - B) * (1 - (v - y0) / (y1 - y0));
  for (const v of yt) { el("line", { x1: L, x2: W - R, y1: Y(v), y2: Y(v), stroke: css(v === 0 ? "--axis" : "--grid"), "stroke-width": 1 }, svg);
    el("text", { x: L - 8, y: Y(v) + 4, "text-anchor": "end" }, svg).textContent = lakh(v); }
  for (let y = new Date(x0).getFullYear(); y <= new Date(x1).getFullYear(); y++) for (const mo of [0, 6]) { const t = Date.UTC(y, mo, 1); if (t < x0 || t > x1) continue;
    el("text", { x: X(t), y: H - 8, "text-anchor": "middle" }, svg).textContent = (mo ? "Jul " : "Jan ") + y; }
  const vs = Date.parse("2025-07-01"); el("line", { x1: X(vs), x2: X(vs), y1: T, y2: H - B, stroke: css("--axis"), "stroke-dasharray": "3 3" }, svg);
  el("text", { x: X(vs) + 6, y: T + 12, class: "lbl" }, svg).textContent = "VALIDATE →";
  series.forEach((s, i) => { const d = s.points.map((p, k) => `${k ? "L" : "M"}${X(Date.parse(p[0])).toFixed(1)},${Y(p[1]).toFixed(1)}`).join("");
    el("path", { d, fill: "none", stroke: css(SER[i]), "stroke-width": 2, "stroke-linejoin": "round" }, svg);
    const last = s.points[s.points.length - 1]; el("text", { x: X(Date.parse(last[0])) + 8, y: Y(last[1]) + 4 + (i ? 12 : -4), class: "lbl" }, svg).textContent = lakh(last[1]); });
  const cross = el("line", { y1: T, y2: H - B, stroke: css("--axis"), visibility: "hidden" }, svg);
  const dots = series.map((s, i) => el("circle", { r: 4.5, fill: css(SER[i]), stroke: css("--surface-1"), "stroke-width": 2, visibility: "hidden" }, svg));
  const hit = el("rect", { x: L, y: T, width: W - L - R, height: H - T - B, fill: "transparent" }, svg);
  hit.addEventListener("mousemove", e => { const r = svg.getBoundingClientRect(), t = x0 + (x1 - x0) * ((e.clientX - r.left) * W / r.width - L) / (W - L - R);
    let html = ""; series.forEach((s, i) => { let best = s.points[0]; for (const p of s.points) if (Date.parse(p[0]) <= t) best = p;
      dots[i].setAttribute("cx", X(Date.parse(best[0]))); dots[i].setAttribute("cy", Y(best[1])); dots[i].setAttribute("visibility", "visible");
      html += `<div><span class="sw" style="background:var(${SER[i]})"></span>${s.name}: <b>${inr(best[1])}</b></div>`; });
    cross.setAttribute("x1", X(t)); cross.setAttribute("x2", X(t)); cross.setAttribute("visibility", "visible");
    show(`<div style="color:var(--ink-2)">${new Date(t).toISOString().slice(0, 10)}</div>${html}`, e); });
  hit.addEventListener("mouseleave", () => { hide(); cross.setAttribute("visibility", "hidden"); dots.forEach(d => d.setAttribute("visibility", "hidden")); });
}

// 2. diverging bars
function bars(svg, rows) {
  const W = 1000, H = 230, L = 250, R = 110, T = 8, rowH = 50; svg.innerHTML = "";
  const m = Math.max(...rows.map(r => Math.abs(r.net))), xt = niceTicks(-m, m, 4), a = xt[0], b = xt[xt.length - 1];
  const X = v => L + (W - L - R) * (v - a) / (b - a);
  for (const v of xt) { el("line", { x1: X(v), x2: X(v), y1: T, y2: T + rows.length * rowH, stroke: css(v === 0 ? "--axis" : "--grid") }, svg);
    el("text", { x: X(v), y: T + rows.length * rowH + 16, "text-anchor": "middle" }, svg).textContent = lakh(v); }
  rows.forEach((r, i) => { const y = T + i * rowH + 12, h = 26, x = Math.min(X(0), X(r.net)), w = Math.abs(X(r.net) - X(0));
    el("text", { x: L - 12, y: y + 17, "text-anchor": "end", class: "lbl" }, svg).textContent = r.reason.replace(/_/g, " ").toLowerCase() + ` (${r.count})`;
    const bar = el("rect", { x, y, width: Math.max(w, 2), height: h, rx: 4, fill: css(r.net >= 0 ? "--pos" : "--neg") }, svg);
    el("text", { x: r.net >= 0 ? X(r.net) + 8 : X(r.net) - 8, y: y + 17, "text-anchor": r.net >= 0 ? "start" : "end", class: "lbl" }, svg).textContent = lakh(r.net);
    const hit = el("rect", { x: L, y: y - 8, width: W - L - R, height: h + 16, fill: "transparent" }, svg);
    hit.addEventListener("mousemove", e => show(`<b>${r.reason}</b><div>${r.count} trades · net <b>${inr(r.net)}</b></div><div style="color:var(--ink-2)">${inr(r.net / r.count)} per trade</div>`, e));
    hit.addEventListener("mouseleave", hide); });
}

// 3. line on ordered x (stop width)
function lineOrd(svg, series) {
  const W = 480, H = 260, L = 58, R = 16, T = 12, B = 36; svg.innerHTML = "";
  const xs = series[0].points.map(p => p[0]), ys = series.flatMap(s => s.points.map(p => p[1])).concat([0]);
  const yt = niceTicks(Math.min(...ys), Math.max(...ys), 4), y0 = yt[0], y1 = yt[yt.length - 1];
  const PX = 22, X = v => L + PX + (W - L - R - 2 * PX) * (v - xs[0]) / (xs[xs.length - 1] - xs[0]), Y = v => T + (H - T - B) * (1 - (v - y0) / (y1 - y0));
  for (const v of yt) { el("line", { x1: L, x2: W - R, y1: Y(v), y2: Y(v), stroke: css(v === 0 ? "--axis" : "--grid") }, svg);
    el("text", { x: L - 6, y: Y(v) + 4, "text-anchor": "end" }, svg).textContent = lakh(v); }
  for (const x of xs) el("text", { x: X(x), y: H - 18, "text-anchor": "middle" }, svg).textContent = x === 60 ? "60 (V1)" : x;
  el("text", { x: (L + W - R) / 2, y: H - 2, "text-anchor": "middle" }, svg).textContent = "early stop = arm distance (points)";
  series.forEach((s, i) => { el("path", { d: s.points.map((p, k) => `${k ? "L" : "M"}${X(p[0])},${Y(p[1])}`).join(""), fill: "none", stroke: css(SER[i]), "stroke-width": 2 }, svg);
    s.points.forEach(p => { const c = el("circle", { cx: X(p[0]), cy: Y(p[1]), r: 4.5, fill: css(SER[i]), stroke: css("--surface-1"), "stroke-width": 2 }, svg);
      const hit = el("circle", { cx: X(p[0]), cy: Y(p[1]), r: 12, fill: "transparent" }, svg);
      hit.addEventListener("mousemove", e => show(`<b>S = A = ${p[0]}</b>${p[0] === 60 ? " (V1)" : ""}<div>${s.name}: <b>${inr(p[1])}</b></div>`, e));
      hit.addEventListener("mouseleave", hide); }); });
}

// 4. table
function table() {
  const f = v => `<td class="${v < 0 ? "neg" : "pos"}">${lakh(v)}</td>`, rule = r => !r ? `<span class="chip">reference</span>` :
    `(a) ${r.a ? "✓" : "✗"} &nbsp;(b) ${r.b === "n/a" ? "–" : r.b ? "✓" : "✗"} &nbsp;(c) ${r.c ? "✓" : "✗"} &nbsp;<b>${r.promoted ? "PROMOTED" : "not promoted"}</b>`;
  document.getElementById("tbl").innerHTML = `<thead><tr><th>variant</th><th>TRAIN adv</th><th>TRAIN fav</th><th>VALIDATE adv</th><th>VALIDATE fav</th><th>VALIDATE max DD (adv)</th><th style="text-align:left">rule</th></tr></thead><tbody>` +
    D.rows.map(r => `<tr><td>${r.variant}</td>${f(r.TRAIN_adv_net)}${f(r.TRAIN_fav_net)}${f(r.VALIDATE_adv_net)}${f(r.VALIDATE_fav_net)}<td>${lakh(r.VALIDATE_adv_dd)}</td><td style="text-align:left">${rule(r.rule)}</td></tr>`).join("") + "</tbody>";
}

function render() {
  legend("eqlegend", D.equity.map(s => s.name)); legend("stlegend", D.stop.TRAIN.map(s => s.name)); legend("stlegend2", D.stop.VALIDATE.map(s => s.name));
  lineTime(document.getElementById("eq"), D.equity); bars(document.getElementById("rsn"), D.reasons);
  lineOrd(document.getElementById("stT"), D.stop.TRAIN); lineOrd(document.getElementById("stV"), D.stop.VALIDATE); table();
  document.getElementById("grossnote").textContent = `Total: gross ${inr(D.gross)}, costs ${inr(D.costs)}, net ${inr(D.gross - D.costs)}. The trades lose before costs.`;
}
render();
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", render);
</script>
</body></html>
"""

if __name__ == "__main__":
    sys.exit(main())
