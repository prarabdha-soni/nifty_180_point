#!/usr/bin/env python3
"""
make_report.py -- self-contained HTML report for one or more backtest runs.

    python make_report.py --data data/nifty_3y_insample.csv \\
        ALWAYS=results/insample_always FLAT_ONLY=results/insample_flat_only \\
        --out results/report.html

What the page shows
  * metric tiles for the selected run (switch runs at the top)
  * overview: spot price over the whole period with every entry (triangle),
    exit (cross) and the trade's span shaded by its net result
  * equity curve: cumulative net P&L trade by trade, with drawdown
  * the trade ledger; click a row (or a marker) to open the trade
  * trade view: the price path around one trade with the strategy's own
    levels drawn on it -- entry, early stop (S), arm (A), partial (P) and the
    trailing stop (best - T) as it moved -- plus the engine events that fired

No external libraries; the page works offline and inside a side panel.
Times are shown exactly as they appear in the data file (IST).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import sys
from typing import Any, Dict, List, Optional

import pandas as pd

EPOCH = dt.datetime(1970, 1, 1)


def replay_state_track(run_dir: str, data_path: str) -> Optional[dict]:
    """Re-run the backtest with the run's own config.json and record the
    engine's state after every spot tick, so the trade view draws the levels the
    engine actually used (MFE incl. R6 rebase, early-stop flag, gap regime,
    deferred flip) rather than a reconstruction. Records only state changes.
    Nothing in strategy/ is modified: this subclasses Backtester and calls the
    real on_spot_tick."""
    try:
        from dataclasses import replace
        from strategy.config import Config
        from strategy.data import MarketData
        from strategy.backtester import Backtester
    except ImportError:
        return None
    cfg_path = os.path.join(run_dir, "config.json")
    if not os.path.exists(cfg_path):
        return None
    with open(cfg_path) as fh:
        d = json.load(fh)
    if "tie_break_order" in d:
        d["tie_break_order"] = tuple(d["tie_break_order"])
    cfg = replace(Config(), **d)
    cfg.validate()

    class Recorder(Backtester):
        def __init__(self, c):
            super().__init__(c)
            self.track: List[list] = []
            self._last = None

        def on_spot_tick(self, st, spot_ltp, futures_ltp, ts):
            super().on_spot_tick(st, spot_ltp, futures_ltp, ts)
            key = (int(st.position), st.trade_id, round(st.mfe_points, 2), st.early_stop_active,
                   st.gap_regime, st.deferred_flip_active, st.partial_done,
                   st.pending_spot_level, st.spot_entry_price)
            if key != self._last:
                self._last = key
                self.track.append([
                    to_epoch(ts), int(st.position), st.trade_id,
                    None if st.spot_entry_price is None else round(st.spot_entry_price, 2),
                    round(st.mfe_points, 2), int(st.early_stop_active), int(st.gap_regime),
                    int(st.deferred_flip_active),
                    None if st.deferred_flip_price is None else round(st.deferred_flip_price, 2),
                    int(st.partial_done),
                    None if st.pending_spot_level is None else round(st.pending_spot_level, 2),
                    None if st.pending_side is None else int(st.pending_side),
                ])

    market = MarketData.from_merged_csv(data_path, cfg)
    bt = Recorder(cfg)
    res = bt.run(market)
    closed = [t for t in res.trades if t.is_closed and t.exit_reason != "DATASET_END"]
    fs = res.final_state
    D = cfg.swing_distance
    final = {
        "t": to_epoch(fs.timestamp) if getattr(fs, "timestamp", None) else None,
        "pos": int(fs.position), "trade_id": fs.trade_id,
        "entry": None if fs.spot_entry_price is None else round(fs.spot_entry_price, 2),
        "entry_fut": None if fs.futures_entry_price is None else round(fs.futures_entry_price, 2),
        "entry_t": to_epoch(fs.entry_timestamp) if fs.entry_timestamp else None,
        "qty": fs.qty, "mfe": round(fs.mfe_points, 2), "mae": round(fs.mae_points, 2),
        "early_stop": bool(fs.early_stop_active), "partial_done": bool(fs.partial_done),
        "gap_regime": bool(fs.gap_regime), "deferred": bool(fs.deferred_flip_active),
        "deferred_price": fs.deferred_flip_price,
        "running_high": fs.running_high, "running_low": fs.running_low,
        "long_trigger": None if fs.running_low is None else round(fs.running_low + D, 2),
        "short_trigger": None if fs.running_high is None else round(fs.running_high - D, 2),
        "pending_side": None if fs.pending_side is None else int(fs.pending_side),
        "pending_level": fs.pending_spot_level,
        "consecutive_early_stops": fs.consecutive_early_stops,
        "last_spot": fs.last_spot, "last_fut": fs.last_futures,
    }
    return {"fields": ["t", "pos", "trade_id", "entry", "mfe", "early_stop", "gap_regime",
                       "deferred", "deferred_price", "partial_done", "pending_level", "pending_side"],
            "rows": bt.track, "final": final,
            "replay_trades": len(closed),
            "replay_net": round(sum(t.net_pnl for t in closed), 2)}


def to_epoch(v) -> Optional[int]:
    if v is None or (isinstance(v, float) and math.isnan(v)) or pd.isna(v):
        return None
    ts = pd.Timestamp(v)
    if ts.tzinfo is not None:
        ts = ts.tz_localize(None)
    return int((ts.to_pydatetime() - EPOCH).total_seconds())


def clean(v) -> Any:
    if v is None:
        return None
    if isinstance(v, (bool,)):
        return bool(v)
    if isinstance(v, (int,)):
        return int(v)
    if isinstance(v, float):
        return None if math.isnan(v) else round(v, 4)
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(v, (pd.Timestamp, dt.datetime, dt.date)):
        return str(v)
    return v


def load_ticks(path: str) -> Dict[str, list]:
    df = pd.read_csv(path, usecols=["timestamp", "spot_ltp", "futures_ltp"])
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    t = ((df["timestamp"] - EPOCH).dt.total_seconds()).astype("int64").tolist()
    return {"t": t, "s": df["spot_ltp"].round(2).tolist(), "f": df["futures_ltp"].round(2).tolist()}


def load_run(label: str, path: str) -> dict:
    with open(os.path.join(path, "metrics.json")) as fh:
        metrics = json.load(fh)
    cfg = {}
    if os.path.exists(os.path.join(path, "config.json")):
        with open(os.path.join(path, "config.json")) as fh:
            cfg = json.load(fh)
    try:
        trades = pd.read_csv(os.path.join(path, "trades.csv"))
    except pd.errors.EmptyDataError:          # engine writes a headerless file when nothing closed
        trades = pd.DataFrame()
    if "completed_trades" not in metrics:      # minimal metrics.json for a run with no closed trades
        metrics = {"completed_trades": 0, "total_net_pnl": 0.0, "total_gross_pnl": 0.0,
                   "total_transaction_costs": 0.0, "diagnostics": {}, **metrics}
    tlist: List[dict] = []
    for _, r in trades.iterrows():
        d = {k: clean(r[k]) for k in trades.columns}
        for k in ("entry_datetime", "partial_datetime", "exit_datetime"):
            d[k] = to_epoch(r[k]) if not pd.isna(r[k]) else None
        d["points"] = round(float(r["gross_pnl"]) / float(r["entry_qty"]), 2) if r["entry_qty"] else None
        tlist.append(d)
    events: List[dict] = []
    ep = os.path.join(path, "events.csv")
    ev = None
    if os.path.exists(ep):
        try:
            ev = pd.read_csv(ep)
        except pd.errors.EmptyDataError:
            ev = None
    if ev is not None and len(ev.columns):
        skip = {"timestamp", "event", "session"}
        for _, r in ev.iterrows():
            info = ", ".join(f"{k}={clean(r[k])}" for k in ev.columns
                             if k not in skip and not pd.isna(r[k]))
            events.append({"t": to_epoch(r["timestamp"]), "e": r["event"], "info": info,
                           "trade_id": clean(r["trade_id"]) if "trade_id" in ev.columns else None})
    keep_cfg = {k: cfg.get(k) for k in (
        "extreme_tracking", "gap_retirement_mfe_mode", "breaker_reset_on_reversal_flat",
        "swing_distance", "early_stop_distance", "early_arm_distance",
        "partial_profit_distance", "trail_distance", "partial_fraction", "breaker_limit",
        "gap_threshold", "transaction_cost_per_side", "lot_size", "num_lots") if k in cfg}
    return {"label": label, "dir": path, "config": keep_cfg, "metrics": metrics,
            "trades": tlist, "events": events, "track": None}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("runs", nargs="+", help="LABEL=results_dir  or  LABEL=results_dir@data.csv "
                                             "(a run made on a different data file than --data)")
    ap.add_argument("--data", required=True, help="the tick/bar CSV the runs were made on (default per run)")
    ap.add_argument("--out", default="results/report.html")
    ap.add_argument("--title", default="NIFTY 180-Point Swing — Backtest Report")
    ap.add_argument("--live", action="store_true",
                    help="paper-trading page: banner, live status box, open position drawn")
    ap.add_argument("--no-replay", action="store_true",
                    help="skip re-running the engine to record per-tick state (trade view then "
                         "reconstructs levels from the rules, which ignores gap suspension and R6)")
    args = ap.parse_args()

    datasets: Dict[str, Dict[str, list]] = {args.data: load_ticks(args.data)}
    ticks = datasets[args.data]
    runs = []
    for spec in args.runs:
        label, _, path = spec.partition("=")
        if not path:
            label, path = os.path.basename(spec.rstrip("/")), spec
        path, _, data_path = path.partition("@")
        data_path = data_path or args.data
        if data_path not in datasets:
            datasets[data_path] = load_ticks(data_path)
        r = load_run(label, path)
        r["data"] = data_path
        if not args.no_replay:
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            tr = replay_state_track(path, data_path)
            if tr is not None:
                m = r["metrics"]
                ok = (tr["replay_trades"] == m.get("completed_trades", 0)
                      and abs(tr["replay_net"] - float(m.get("total_net_pnl", 0) or 0)) < 1.0)
                print(f"  replayed {label}: {tr['replay_trades']} trades, net {tr['replay_net']:,.0f} "
                      f"-> {'matches' if ok else 'DOES NOT MATCH'} the saved run "
                      f"({len(tr['rows'])} state records)")
                if not ok:
                    print("  WARNING: replay differs from the saved run; the data file or config "
                          "is not the one this run was produced with. State track dropped.")
                    tr = None
                r["track"] = tr
        runs.append(r)
    first = dt.datetime.utcfromtimestamp(ticks["t"][0]) if ticks["t"] else None
    last = dt.datetime.utcfromtimestamp(ticks["t"][-1]) if ticks["t"] else None
    sessions = len({dt.datetime.utcfromtimestamp(t).date() for t in ticks["t"]})
    p = runs[0]["config"] if runs else {}
    payload = {
        "title": args.title,
        "generated": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "data_file": args.data,
        "period": [str(first), str(last)],
        "sessions": sessions,
        "datasets": datasets,
        "params": {"D": p.get("swing_distance", 180), "S": p.get("early_stop_distance", 60),
                   "A": p.get("early_arm_distance", 60), "P": p.get("partial_profit_distance", 140),
                   "T": p.get("trail_distance", 180), "q": p.get("partial_fraction", 0.5)},
        "runs": runs,
        "live": bool(args.live),
    }
    html = TEMPLATE.replace("__TITLE__", args.title).replace(
        "__DATA__", json.dumps(payload, separators=(",", ":")))
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w") as fh:
        fh.write(html)
    print(f"wrote {args.out}  ({os.path.getsize(args.out) / 1e6:.1f} MB; {len(ticks['t']):,} ticks, "
          f"{sessions} sessions, runs: {', '.join(r['label'] for r in runs)})")
    return 0


TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Backtest Report</title>
<meta name="description" content="__TITLE__ — trades, equity curve and per-trade mechanics">
<style>
:root{
  --bg:#f6f7f9; --panel:#ffffff; --ink:#1b1f24; --muted:#5f6b7a; --line:#dfe3e8; --grid:#eceff3;
  --accent:#2563eb; --long:#0f9d58; --short:#d93025; --win:rgba(15,157,88,.16); --loss:rgba(217,48,37,.14);
  --stop:#d93025; --arm:#f59e0b; --partial:#7c3aed; --trail:#0891b2; --entry:#111827;
  --tile:#ffffff; --hover:#eef2ff; --sel:#dbeafe;
}
@media (prefers-color-scheme: dark){
  :root:not([data-theme="light"]){
    --bg:#0f1216; --panel:#171b21; --ink:#e6e9ee; --muted:#98a2b3; --line:#2a313b; --grid:#20262e;
    --accent:#60a5fa; --long:#34d399; --short:#f87171; --win:rgba(52,211,153,.18); --loss:rgba(248,113,113,.16);
    --stop:#f87171; --arm:#fbbf24; --partial:#a78bfa; --trail:#22d3ee; --entry:#f3f4f6;
    --tile:#171b21; --hover:#1f2937; --sel:#1e3a5f;
  }
}
:root[data-theme="dark"]{
  --bg:#0f1216; --panel:#171b21; --ink:#e6e9ee; --muted:#98a2b3; --line:#2a313b; --grid:#20262e;
  --accent:#60a5fa; --long:#34d399; --short:#f87171; --win:rgba(52,211,153,.18); --loss:rgba(248,113,113,.16);
  --stop:#f87171; --arm:#fbbf24; --partial:#a78bfa; --trail:#22d3ee; --entry:#f3f4f6;
  --tile:#171b21; --hover:#1f2937; --sel:#1e3a5f;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
.wrap{max-width:1240px;margin:0 auto;padding:16px}
h1{font-size:20px;margin:0 0 4px}
h2{font-size:15px;margin:18px 0 8px;color:var(--muted);font-weight:600;letter-spacing:.02em;text-transform:uppercase}
.sub{color:var(--muted);font-size:13px}
.runs{display:flex;gap:8px;flex-wrap:wrap;margin:12px 0}
.runs button{border:1px solid var(--line);background:var(--panel);color:var(--ink);padding:6px 12px;border-radius:8px;cursor:pointer;font-size:13px}
.runs button.on{background:var(--accent);border-color:var(--accent);color:#fff}
.tiles{display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:10px}
.tile{background:var(--tile);border:1px solid var(--line);border-radius:10px;padding:10px 12px}
.tile .k{color:var(--muted);font-size:12px}
.tile .v{font-size:18px;font-weight:600;margin-top:2px;font-variant-numeric:tabular-nums}
.tile .v.pos{color:var(--long)} .tile .v.neg{color:var(--short)}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:12px;margin-top:10px}
canvas{display:block;width:100%;touch-action:none}
.legend{display:flex;gap:14px;flex-wrap:wrap;color:var(--muted);font-size:12px;margin:6px 0 0}
.legend span::before{content:"";display:inline-block;width:12px;height:3px;margin-right:6px;vertical-align:middle;background:var(--c)}
.tip{position:fixed;pointer-events:none;background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:8px 10px;font-size:12px;box-shadow:0 6px 24px rgba(0,0,0,.18);display:none;z-index:9;max-width:320px}
.tip b{font-variant-numeric:tabular-nums}
table{width:100%;border-collapse:collapse;font-size:12.5px;font-variant-numeric:tabular-nums}
th,td{padding:6px 8px;border-bottom:1px solid var(--grid);text-align:right;white-space:nowrap}
th:first-child,td:first-child,th.l,td.l{text-align:left}
th{color:var(--muted);font-weight:600;cursor:pointer;position:sticky;top:0;background:var(--panel)}
tbody tr{cursor:pointer} tbody tr:hover{background:var(--hover)} tbody tr.sel{background:var(--sel)}
.tscroll{overflow:auto;max-height:420px;border:1px solid var(--line);border-radius:8px}
.pos{color:var(--long)} .neg{color:var(--short)}
.detail{display:grid;grid-template-columns:1fr;gap:12px}
@media(min-width:900px){.detail{grid-template-columns:2fr 1fr}}
.kv{display:grid;grid-template-columns:auto 1fr;gap:4px 12px;font-size:13px}
.kv div:nth-child(odd){color:var(--muted)}
.events{font-size:12px;color:var(--muted);margin-top:8px;max-height:180px;overflow:auto}
.events div{padding:2px 0;border-bottom:1px dashed var(--grid)}
.note{font-size:12.5px;color:var(--muted);margin-top:8px}
.status{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:8px 16px;font-size:13px}
.status .k{color:var(--muted);font-size:11.5px}
.status .v{font-weight:600;font-variant-numeric:tabular-nums}
.badge{display:inline-block;padding:1px 6px;border-radius:6px;font-size:11px;border:1px solid var(--line);color:var(--muted);margin-left:4px}
</style>
</head>
<body>
<div class="wrap">
  <h1>__TITLE__</h1>
  <div class="sub" id="sub"></div>
  <div id="banner" class="panel" style="display:none;border-color:var(--arm);margin-top:10px">
    <b>PAPER TEST — nothing is traded.</b> The engine is re-run every few minutes on the day's candles as
    they arrive, starting FLAT at the open with the previous session as reference. No order is ever sent;
    this project contains no order code. The last, still-forming minute is left out.
  </div>
  <div id="status" class="panel" style="display:none"></div>
  <div class="runs" id="runs"></div>
  <div class="tiles" id="tiles"></div>

  <h2>Overview — every trade on the price path</h2>
  <div class="panel">
    <canvas id="ov" height="300"></canvas>
    <div class="legend">
      <span style="--c:var(--long)">▲ long entry</span><span style="--c:var(--short)">▼ short entry</span>
      <span style="--c:var(--ink)">✕ exit</span><span style="--c:var(--win)">shaded: winning trade</span>
      <span style="--c:var(--loss)">shaded: losing trade</span>
    </div>
    <div class="note">Hover for price and time; hover a marker for the trade; click a shaded span to open the trade below. The x-axis is trading time (nights and weekends removed).</div>
  </div>

  <h2>Equity — cumulative net P&amp;L, trade by trade</h2>
  <div class="panel"><canvas id="eq" height="180"></canvas>
    <div class="legend"><span style="--c:var(--accent)">net P&amp;L after costs</span><span style="--c:var(--loss)">drawdown from peak</span></div>
  </div>

  <h2>Trade ledger</h2>
  <div class="panel"><div class="tscroll"><table id="tbl"><thead></thead><tbody></tbody></table></div>
    <div class="note">Points = futures points earned per unit (partials included). Click a column to sort, a row to open the trade.</div></div>

  <h2>One trade, step by step</h2>
  <div class="panel detail">
    <div>
      <canvas id="dt" height="340"></canvas>
      <div class="legend">
        <span style="--c:var(--entry)">entry price</span><span style="--c:var(--stop)">early stop (entry ∓ S)</span>
        <span style="--c:var(--arm)">arm level (entry ± A)</span><span style="--c:var(--partial)">partial level (entry ± P)</span>
        <span style="--c:var(--trail)">trailing stop (entry ± (MFE − T)), once armed</span>
        <span style="--c:var(--arm)">amber band: gap regime (trail suspended)</span>
        <span style="--c:var(--stop)">red band: breaker, no stop</span>
      </div>
      <div class="note" id="howto"></div>
    </div>
    <div>
      <div class="kv" id="kv"></div>
      <div class="events" id="ev"></div>
    </div>
  </div>
  <div class="note" id="foot"></div>
</div>
<div class="tip" id="tip"></div>
<script>
const DATA = __DATA__;
const P = DATA.params;
let T = null, N = 0, OV = [];      // current run's ticks: t (epoch s, IST-as-UTC), s (spot), f (futures)
let run = 0, selected = null, sortKey = "entry_datetime", sortDir = 1;
function useDataset(path) {
  T = DATA.datasets[path]; N = T.t.length; OV = [];
  const sec = T.t.map(t => t % 60), expanded = sec.some(s => s === 45);
  for (let i = 0; i < N; i++) if (!expanded || sec[i] === 45) OV.push(i);
}

// ---------- helpers ----------
const css = v => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
const pad = n => String(n).padStart(2, "0");
function fmtT(e, withDate = true) {
  if (e == null) return "—";
  const d = new Date(e * 1000);
  const dd = `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())}`;
  const tt = `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}`;
  return withDate ? `${dd} ${tt}` : tt;
}
const fmtD = e => fmtT(e).slice(0, 10);
const money = v => v == null ? "—" : (v < 0 ? "−" : "") + "₹" + Math.abs(Math.round(v)).toLocaleString("en-IN");
const num = (v, d = 2) => v == null ? "—" : Number(v).toLocaleString("en-IN", { minimumFractionDigits: d, maximumFractionDigits: d });
function idxAt(e) {              // first tick index with t >= e
  let lo = 0, hi = N - 1;
  while (lo < hi) { const m = (lo + hi) >> 1; if (T.t[m] < e) lo = m + 1; else hi = m; }
  return lo;
}
// overview series (OV): one point per minute -- the :45 close tick when bars are expanded
const ovPos = i => {             // tick index -> overview position (nearest)
  let lo = 0, hi = OV.length - 1;
  while (lo < hi) { const m = (lo + hi) >> 1; if (OV[m] < i) lo = m + 1; else hi = m; }
  return lo;
};
function setupCanvas(c) {
  // remember the CSS height once: c.height below overwrites the attribute with the
  // DPR-scaled value, so reading it back on the next render would compound
  if (!c.dataset.h) c.dataset.h = c.getAttribute("height");
  const dpr = window.devicePixelRatio || 1, w = c.clientWidth, h = c.dataset.h | 0;
  c.width = w * dpr; c.height = h * dpr; c.style.height = h + "px";
  const g = c.getContext("2d"); g.setTransform(dpr, 0, 0, dpr, 0, 0);
  return { g, w, h };
}
function scale(vals, pad = 0.06) {
  let lo = Infinity, hi = -Infinity;
  for (const v of vals) { if (v == null) continue; if (v < lo) lo = v; if (v > hi) hi = v; }
  if (!isFinite(lo)) { lo = 0; hi = 1; }
  const r = (hi - lo) || 1; return [lo - r * pad, hi + r * pad];
}
function axes(g, L, R, Tp, B, ylo, yhi, xlabels) {
  g.strokeStyle = css("--grid"); g.lineWidth = 1; g.fillStyle = css("--muted"); g.font = "11px system-ui";
  const ticks = 5;
  for (let k = 0; k <= ticks; k++) {
    const v = ylo + (yhi - ylo) * k / ticks, y = B - (B - Tp) * k / ticks;
    g.beginPath(); g.moveTo(L, y); g.lineTo(R, y); g.stroke();
    g.textAlign = "right"; g.textBaseline = "middle"; g.fillText(num(v, v > 1000 ? 0 : 0), L - 6, y);
  }
  g.textAlign = "center"; g.textBaseline = "top";
  for (const [x, s] of xlabels) { g.beginPath(); g.moveTo(x, Tp); g.lineTo(x, B); g.stroke(); g.fillText(s, x, B + 4); }
}
const tip = document.getElementById("tip");
function showTip(html, x, y) { tip.innerHTML = html; tip.style.display = "block";
  const r = tip.getBoundingClientRect(); tip.style.left = Math.min(x + 14, innerWidth - r.width - 8) + "px"; tip.style.top = Math.min(y + 14, innerHeight - r.height - 8) + "px"; }
function hideTip() { tip.style.display = "none"; }

// ---------- header / tiles ----------
function renderHeader() {
  const R = DATA.runs[run];
  document.getElementById("sub").textContent =
    `${DATA.runs[run].data} · ${DATA.period[0].slice(0, 10)} → ${DATA.period[1].slice(0, 10)} · ${DATA.sessions} sessions · ${N.toLocaleString()} ticks · generated ${DATA.generated}`;
  const rb = document.getElementById("runs"); rb.innerHTML = "";
  DATA.runs.forEach((r, i) => { const b = document.createElement("button"); b.textContent = r.label;
    const c = r.config || {}; b.title = `extreme_tracking=${c.extreme_tracking} gap_retirement_mfe_mode=${c.gap_retirement_mfe_mode}`;
    b.className = i === run ? "on" : ""; b.onclick = () => { run = i; selected = null; renderAll(); }; rb.appendChild(b); });
  const m = R.metrics, d = m.diagnostics || {}, has = m.completed_trades > 0;
  const tiles = [
    ["Net P&L", money(m.total_net_pnl), m.total_net_pnl], ["Gross P&L", money(m.total_gross_pnl), m.total_gross_pnl],
    ["Costs", money(m.total_transaction_costs)], ["Trades", m.completed_trades ?? 0],
    ["Win rate", has ? (m.win_rate * 100).toFixed(1) + "%" : "—"], ["Profit factor", has ? num(m.profit_factor, 2) : "—"],
    ["Expectancy / trade", has ? money(m.expectancy_per_trade) : "—", m.expectancy_per_trade],
    ["Max drawdown", has ? money(m.maximum_drawdown) : "—"], ["Avg hold", has ? (m.average_holding_time_seconds / 3600).toFixed(1) + " h" : "—"],
    ["Trail exits W / L", `${d.trail_exits_winning ?? "—"} / ${d.trail_exits_losing ?? "—"}`],
    ["Breaker episodes", d.breaker_episodes ?? "—"], ["Adverse-gap exits", d.adverse_gap_exits ?? "—"],
  ];
  const tb = document.getElementById("tiles"); tb.innerHTML = "";
  for (const [k, v, sign] of tiles) { const e = document.createElement("div"); e.className = "tile";
    e.innerHTML = `<div class="k">${k}</div><div class="v ${sign == null ? "" : sign >= 0 ? "pos" : "neg"}">${v}</div>`; tb.appendChild(e); }
  const c = R.config || {};
  document.getElementById("foot").textContent =
    `Run: ${R.dir} · extreme_tracking=${c.extreme_tracking ?? "?"} (R2) · gap_retirement_mfe_mode=${c.gap_retirement_mfe_mode ?? "?"} (R6) · D=${P.D} S=${P.S} A=${P.A} P=${P.P} T=${P.T} q=${P.q} · qty ${(c.lot_size || 65) * (c.num_lots || 6)} · cost ${c.transaction_cost_per_side ?? 0.00015} per side. Simulated on historical data; no orders were placed.`;
}

// ---------- overview chart ----------
let ovState = null;
function renderOverview() {
  const c = document.getElementById("ov"), { g, w, h } = setupCanvas(c);
  const L = 62, R = w - 12, Tp = 12, B = h - 26, n = OV.length;
  const ys = OV.map(i => T.s[i]); const [ylo, yhi] = scale(ys);
  const X = p => L + (R - L) * p / Math.max(n - 1, 1), Y = v => B - (B - Tp) * (v - ylo) / (yhi - ylo);
  g.clearRect(0, 0, w, h);
  // x labels: first tick of a session, every k sessions
  const days = []; let last = "";
  OV.forEach((i, p) => { const d = fmtD(T.t[i]); if (d !== last) { days.push([p, d]); last = d; } });
  const every = Math.max(1, Math.ceil(days.length / Math.max(3, Math.floor((R - L) / 90))));
  axes(g, L, R, Tp, B, ylo, yhi, days.filter((_, k) => k % every === 0).map(([p, d]) => [X(p), d.slice(5)]));
  // trade spans
  const trades = DATA.runs[run].trades;
  for (const tr of trades) {
    const a = ovPos(idxAt(tr.entry_datetime)), b = ovPos(idxAt(tr.exit_datetime));
    g.fillStyle = css(tr.net_pnl >= 0 ? "--win" : "--loss");
    g.fillRect(X(a), Tp, Math.max(2, X(b) - X(a)), B - Tp);
    if (selected != null && tr.trade_id === selected) { g.strokeStyle = css("--accent"); g.lineWidth = 1.5; g.strokeRect(X(a), Tp, Math.max(2, X(b) - X(a)), B - Tp); }
  }
  // price
  g.strokeStyle = css("--ink"); g.lineWidth = 1; g.beginPath();
  OV.forEach((i, p) => { const x = X(p), y = Y(T.s[i]); p ? g.lineTo(x, y) : g.moveTo(x, y); }); g.stroke();
  // markers
  const marks = [];
  for (const tr of trades) {
    const pe = ovPos(idxAt(tr.entry_datetime)), px = ovPos(idxAt(tr.exit_datetime));
    const xe = X(pe), ye = Y(tr.entry_spot), xx = X(px), yx = Y(tr.exit_spot);
    g.fillStyle = css(tr.direction === "LONG" ? "--long" : "--short"); g.beginPath();
    if (tr.direction === "LONG") { g.moveTo(xe, ye - 9); g.lineTo(xe - 5, ye - 1); g.lineTo(xe + 5, ye - 1); }
    else { g.moveTo(xe, ye + 9); g.lineTo(xe - 5, ye + 1); g.lineTo(xe + 5, ye + 1); }
    g.closePath(); g.fill();
    g.strokeStyle = css(tr.net_pnl >= 0 ? "--long" : "--short"); g.lineWidth = 1.6; g.beginPath();
    g.moveTo(xx - 4, yx - 4); g.lineTo(xx + 4, yx + 4); g.moveTo(xx + 4, yx - 4); g.lineTo(xx - 4, yx + 4); g.stroke();
    marks.push({ x: xe, y: ye, tr, kind: "entry" }, { x: xx, y: yx, tr, kind: "exit" });
  }
  const f = DATA.runs[run].track && DATA.runs[run].track.final;
  if (f && f.pos && f.entry_t) {
    const pe = ovPos(idxAt(f.entry_t)), xe = X(pe), ye = Y(f.entry);
    g.fillStyle = css("--sel"); g.fillRect(xe, Tp, X(n - 1) - xe, B - Tp);
    g.fillStyle = css(f.pos > 0 ? "--long" : "--short"); g.beginPath();
    if (f.pos > 0) { g.moveTo(xe, ye - 9); g.lineTo(xe - 5, ye - 1); g.lineTo(xe + 5, ye - 1); }
    else { g.moveTo(xe, ye + 9); g.lineTo(xe - 5, ye + 1); g.lineTo(xe + 5, ye + 1); }
    g.closePath(); g.fill();
    const lab = `OPEN ${f.pos > 0 ? "LONG" : "SHORT"} @ ${num(f.entry)}`; g.font = "11px system-ui"; g.textBaseline = "top";
    const fits = xe + 8 + g.measureText(lab).width < R; g.textAlign = fits ? "left" : "right"; g.fillText(lab, fits ? xe + 8 : xe - 8, Tp + 4);
  }
  ovState = { L, R, Tp, B, n, X, Y, marks, trades };
}
document.getElementById("ov").addEventListener("mousemove", ev => {
  if (!ovState) return; const r = ev.target.getBoundingClientRect(), x = ev.clientX - r.left, y = ev.clientY - r.top;
  const { L, R, n, marks } = ovState;
  let best = null, bd = 100;
  for (const m of marks) { const d = Math.hypot(m.x - x, m.y - y); if (d < bd) { bd = d; best = m; } }
  if (best && bd < 9) { const t = best.tr; showTip(`<b>#${t.trade_id} ${t.direction}</b> ${best.kind}<br>${fmtT(t.entry_datetime)} @ ${num(t.entry_spot)} → ${fmtT(t.exit_datetime)} @ ${num(t.exit_spot)}<br>${t.exit_reason} · <b class="${t.net_pnl >= 0 ? "pos" : "neg"}">${money(t.net_pnl)}</b> (${t.points} pts)`, ev.clientX, ev.clientY); return; }
  const p = Math.round((x - L) / (R - L) * (n - 1)); if (p < 0 || p >= n) { hideTip(); return; }
  const i = OV[p]; showTip(`${fmtT(T.t[i])}<br>spot <b>${num(T.s[i])}</b> · fut <b>${num(T.f[i])}</b>`, ev.clientX, ev.clientY);
});
document.getElementById("ov").addEventListener("mouseleave", hideTip);
document.getElementById("ov").addEventListener("click", ev => {
  if (!ovState) return; const r = ev.target.getBoundingClientRect(), x = ev.clientX - r.left;
  const { L, R, n, trades } = ovState; const p = (x - L) / (R - L) * (n - 1);
  let hit = null, span = Infinity;
  for (const tr of trades) { const a = ovPos(idxAt(tr.entry_datetime)), b = ovPos(idxAt(tr.exit_datetime));
    if (p >= a - 1 && p <= b + 1 && (b - a) < span) { hit = tr; span = b - a; } }
  if (hit) { selected = hit.trade_id; renderOverview(); renderTable(); renderDetail(); document.getElementById("dt").scrollIntoView({ behavior: "smooth", block: "center" }); }
});

// ---------- equity ----------
function renderEquity() {
  const c = document.getElementById("eq"), { g, w, h } = setupCanvas(c);
  const L = 62, R = w - 12, Tp = 10, B = h - 26, n = OV.length;
  const trades = DATA.runs[run].trades.filter(t => t.exit_reason !== "DATASET_END").sort((a, b) => a.exit_datetime - b.exit_datetime);
  const pts = [[0, 0]]; let cum = 0, peak = 0; const dd = [[0, 0]];
  for (const tr of trades) { cum += tr.net_pnl; peak = Math.max(peak, cum); pts.push([ovPos(idxAt(tr.exit_datetime)), cum]); dd.push([pts[pts.length - 1][0], cum - peak]); }
  pts.push([n - 1, cum]); dd.push([n - 1, cum - peak]);
  const [ylo, yhi] = scale(pts.map(p => p[1]).concat(dd.map(p => p[1])), 0.1);
  const X = p => L + (R - L) * p / Math.max(n - 1, 1), Y = v => B - (B - Tp) * (v - ylo) / (yhi - ylo);
  g.clearRect(0, 0, w, h);
  const days = []; let last = ""; OV.forEach((i, p) => { const d = fmtD(T.t[i]); if (d !== last) { days.push([p, d]); last = d; } });
  const every = Math.max(1, Math.ceil(days.length / Math.max(3, Math.floor((R - L) / 90))));
  axes(g, L, R, Tp, B, ylo, yhi, days.filter((_, k) => k % every === 0).map(([p, d]) => [X(p), d.slice(5)]));
  g.strokeStyle = css("--line"); g.beginPath(); g.moveTo(L, Y(0)); g.lineTo(R, Y(0)); g.stroke();
  // drawdown fill
  g.fillStyle = css("--loss"); g.beginPath(); g.moveTo(X(0), Y(0));
  for (let k = 1; k < dd.length; k++) { g.lineTo(X(dd[k][0]), Y(dd[k - 1][1])); g.lineTo(X(dd[k][0]), Y(dd[k][1])); }
  g.lineTo(X(n - 1), Y(0)); g.closePath(); g.fill();
  // equity step
  g.strokeStyle = css("--accent"); g.lineWidth = 1.8; g.beginPath(); g.moveTo(X(0), Y(0));
  for (let k = 1; k < pts.length; k++) { g.lineTo(X(pts[k][0]), Y(pts[k - 1][1])); g.lineTo(X(pts[k][0]), Y(pts[k][1])); }
  g.stroke();
  g.fillStyle = css("--muted"); g.font = "11px system-ui"; g.textAlign = "right"; g.textBaseline = "bottom";
  g.fillText(`final ${money(cum)} (completed trades; an open position at dataset end is excluded, as in the metrics)`, R - 4, Tp + 12);
}

// ---------- table ----------
const COLS = [
  ["trade_id", "#"], ["direction", "side"], ["entry_datetime", "entry", "t"], ["entry_spot", "entry spot", "n"],
  ["entry_reason", "entry reason"], ["partial_datetime", "partial", "t"], ["exit_datetime", "exit", "t"],
  ["exit_spot", "exit spot", "n"], ["exit_reason", "exit reason"], ["points", "points", "n"],
  ["net_pnl", "net ₹", "m"], ["maximum_favourable_excursion", "MFE", "n"], ["maximum_adverse_excursion", "MAE", "n"],
  ["holding_time", "hold h", "h"], ["sessions_spanned", "sessions"], ["breaker_triggered", "breaker"], ["gap_regime_flag", "gap"],
];
function renderTable() {
  const trades = [...DATA.runs[run].trades].sort((a, b) => { const x = a[sortKey], y = b[sortKey];
    return (x == null ? -Infinity : x) > (y == null ? -Infinity : y) ? sortDir : -sortDir; });
  const th = document.querySelector("#tbl thead"); th.innerHTML = "<tr>" + COLS.map(([k, l]) => `<th class="${k === "direction" || k.endsWith("reason") ? "l" : ""}">${l}${k === sortKey ? (sortDir > 0 ? " ▲" : " ▼") : ""}</th>`).join("") + "</tr>";
  th.querySelectorAll("th").forEach((e, i) => e.onclick = () => { const k = COLS[i][0]; if (sortKey === k) sortDir = -sortDir; else { sortKey = k; sortDir = 1; } renderTable(); });
  const tb = document.querySelector("#tbl tbody"); tb.innerHTML = "";
  for (const tr of trades) {
    const row = document.createElement("tr"); if (tr.trade_id === selected) row.className = "sel";
    row.innerHTML = COLS.map(([k, _, f]) => { let v = tr[k]; let cls = "";
      if (f === "t") v = fmtT(v); else if (f === "n") v = num(v, k.includes("excursion") || k === "points" ? 1 : 2);
      else if (f === "m") { cls = v >= 0 ? "pos" : "neg"; v = money(v); } else if (f === "h") v = (v / 3600).toFixed(1);
      else if (typeof v === "boolean") v = v ? "yes" : "";
      if (k === "direction") cls = v === "LONG" ? "pos" : "neg";
      return `<td class="${cls} ${k === "direction" || k.endsWith("reason") ? "l" : ""}">${v ?? ""}</td>`; }).join("");
    row.onclick = () => { selected = tr.trade_id; renderOverview(); renderTable(); renderDetail(); };
    tb.appendChild(row);
  }
}

// ---------- trade detail ----------
function renderDetail() {
  const R = DATA.runs[run]; const trades = R.trades;
  if (!trades.length) { setupCanvas(document.getElementById("dt"));
    document.getElementById("kv").innerHTML = "<div>Trades</div><div>none completed yet</div>";
    document.getElementById("ev").innerHTML = ""; document.getElementById("howto").textContent =
      "No completed trade to show. The status box at the top shows the open position, if any, and the prices that would trigger the next entry."; return; }
  const tr = trades.find(t => t.trade_id === selected) || trades[0]; selected = tr.trade_id;
  const dir = tr.direction === "LONG" ? 1 : -1;
  const i0 = Math.max(0, idxAt(tr.entry_datetime) - 240), i1 = Math.min(N - 1, idxAt(tr.exit_datetime) + 120);
  const ie = idxAt(tr.entry_datetime), ix = idxAt(tr.exit_datetime);
  // strategy levels along the path. Preferred source: the engine's own recorded
  // state (R.track, one record per state change). Fallback: reconstruct from the rules.
  const E = tr.entry_spot, stop = E - dir * P.S, arm = E + dir * P.A, part = E + dir * P.P;
  const n_in = ix - ie + 1;
  const trail = new Array(n_in).fill(null), susp = new Array(n_in).fill(false),
        stopOn = new Array(n_in).fill(false), gapOn = new Array(n_in).fill(false),
        defOn = new Array(n_in).fill(false); let defPrice = null;
  const rows = R.track && R.track.rows; let recorded = false;
  if (rows && rows.length) {
    let r = 0;
    for (let k = 0; k < n_in; k++) {
      const te = T.t[ie + k]; while (r + 1 < rows.length && rows[r + 1][0] <= te) r++;
      const st = rows[r]; if (!st || st[0] > te || st[2] !== tr.trade_id) continue; recorded = true;
      stopOn[k] = !!st[5]; gapOn[k] = !!st[6]; defOn[k] = !!st[7]; if (st[8] != null) defPrice = st[8];
      if (st[4] >= P.A) { trail[k] = st[3] + dir * (st[4] - P.T); susp[k] = !!st[6]; }
    }
  }
  if (!recorded) {                       // rule-based reconstruction (no gap/R6 awareness)
    let best = E, armed = false;
    for (let k = 0; k < n_in; k++) { const px = T.s[ie + k]; if (dir * (px - best) > 0) best = px;
      if (!armed && dir * (best - E) >= P.A) armed = true; stopOn[k] = !armed; if (armed) trail[k] = best - dir * P.T; }
  }
  const armIdx = trail.findIndex(v => v != null);
  const c = document.getElementById("dt"), { g, w, h } = setupCanvas(c);
  const L = 62, Rr = w - 12, Tp = 12, B = h - 26, n = i1 - i0 + 1;
  const vals = []; for (let i = i0; i <= i1; i++) vals.push(T.s[i]); vals.push(stop, arm, part);
  const [ylo, yhi] = scale(vals, 0.08);
  const X = i => L + (Rr - L) * (i - i0) / Math.max(n - 1, 1), Y = v => B - (B - Tp) * (v - ylo) / (yhi - ylo);
  g.clearRect(0, 0, w, h);
  const labels = []; let lastD = ""; for (let i = i0; i <= i1; i += Math.max(1, Math.floor(n / 6))) { const d = fmtD(T.t[i]); labels.push([X(i), d !== lastD ? fmtT(T.t[i]) : fmtT(T.t[i], false)]); lastD = d; }
  axes(g, L, Rr, Tp, B, ylo, yhi, labels);
  // trade span
  g.fillStyle = css(tr.net_pnl >= 0 ? "--win" : "--loss"); g.fillRect(X(ie), Tp, X(ix) - X(ie), B - Tp);
  // levels
  const hline = (v, col, dash, from, to, label, side) => { g.strokeStyle = css(col); g.lineWidth = 1.2; g.setLineDash(dash); g.beginPath(); g.moveTo(X(from), Y(v)); g.lineTo(X(to), Y(v)); g.stroke(); g.setLineDash([]);
    g.fillStyle = css(col); g.font = "11px system-ui"; g.textAlign = "right"; g.textBaseline = side > 0 ? "bottom" : "top";
    const txt = `${label} ${num(v)}`, tw = g.measureText(txt).width; g.fillText(txt, Math.max(L + tw + 4, Math.min(X(to), Rr) - 4), Y(v) + (side > 0 ? -2 : 2)); };
  // entry label sits on the side opposite the entry-marker caption
  g.fillStyle = css("--entry"); g.font = "11px system-ui"; g.textAlign = "left"; g.textBaseline = dir > 0 ? "bottom" : "top";
  g.strokeStyle = css("--entry"); g.lineWidth = 1.2; g.beginPath(); g.moveTo(X(ie), Y(E)); g.lineTo(X(ix), Y(E)); g.stroke();
  g.fillText(`entry ${num(E)}`, X(ie) + 10, Y(E) + (dir > 0 ? -3 : 3));
  // regimes: gap regime (trail suspended) and breaker (no stop at all)
  const shade = (flags, col, label) => { let k = 0; while (k < n_in) { if (!flags[k]) { k++; continue; } let j = k; while (j + 1 < n_in && flags[j + 1]) j++;
      g.fillStyle = css(col); g.globalAlpha = 0.18; g.fillRect(X(ie + k), Tp, Math.max(2, X(ie + j) - X(ie + k)), B - Tp); g.globalAlpha = 1;
      if (X(ie + j) - X(ie + k) > 110) { g.fillStyle = css(col); g.font = "10px system-ui"; g.textAlign = "left"; g.textBaseline = "bottom"; g.fillText(label, X(ie + k) + 3, B - 3); } k = j + 1; } };
  shade(gapOn, "--arm", "gap regime: trail suspended");
  shade(defOn, "--stop", "breaker: no stop, waiting for deferred flip");
  // early stop: drawn only while the engine had it active
  { let k = 0; while (k < n_in) { if (!stopOn[k]) { k++; continue; } let j = k; while (j + 1 < n_in && stopOn[j + 1]) j++;
      hline(stop, "--stop", [6, 4], ie + k, ie + j, `early stop ${dir > 0 ? "−" : "+"}${P.S}`, -dir); k = j + 1; } }
  hline(arm, "--arm", [2, 3], ie, ix, `arm ${dir > 0 ? "+" : "−"}${P.A}`, dir);
  hline(part, "--partial", [2, 3], ie, ix, `partial ${dir > 0 ? "+" : "−"}${P.P}`, dir);
  if (defPrice != null) hline(defPrice, "--stop", [1, 3], ie, ix, `deferred flip ${num(defPrice)}`, -dir);
  if (armIdx >= 0) {
    const seg = (active) => { g.strokeStyle = css(active ? "--trail" : "--muted"); g.lineWidth = active ? 1.6 : 1.1; g.setLineDash(active ? [] : [3, 3]); g.beginPath(); let started = false;
      for (let k = 0; k < n_in; k++) { const on = trail[k] != null && (susp[k] !== active); if (!on) { started = false; continue; }
        const x = X(ie + k), y = Y(trail[k]); started ? g.lineTo(x, y) : g.moveTo(x, y); started = true; } g.stroke(); g.setLineDash([]); };
    seg(true); seg(false);
    const lastK = trail.length - 1 - [...trail].reverse().findIndex(v => v != null);
    g.fillStyle = css("--trail"); g.font = "11px system-ui"; g.textAlign = "right"; g.textBaseline = "bottom";
    g.fillText(`trailing stop (${recorded ? "engine state" : "reconstructed"})`, X(ix) - 4, Y(trail[lastK]) - 2);
  }
  // price path
  g.strokeStyle = css("--ink"); g.lineWidth = 1.3; g.beginPath();
  for (let i = i0; i <= i1; i++) { const x = X(i), y = Y(T.s[i]); i === i0 ? g.moveTo(x, y) : g.lineTo(x, y); } g.stroke();
  // markers: entry, partial, exit
  const mark = (i, v, col, shape, label) => { const x = X(i), y = Y(v); g.fillStyle = css(col); g.strokeStyle = css(col); g.lineWidth = 2; g.beginPath();
    if (shape === "tri") { g.moveTo(x, y - dir * 10); g.lineTo(x - 6, y - dir * 1); g.lineTo(x + 6, y - dir * 1); g.closePath(); g.fill(); }
    else if (shape === "dia") { g.moveTo(x, y - 7); g.lineTo(x + 7, y); g.lineTo(x, y + 7); g.lineTo(x - 7, y); g.closePath(); g.fill(); }
    else { g.moveTo(x - 6, y - 6); g.lineTo(x + 6, y + 6); g.moveTo(x + 6, y - 6); g.lineTo(x - 6, y + 6); g.stroke(); }
    g.font = "11px system-ui"; g.textAlign = "center"; g.textBaseline = dir > 0 ? "top" : "bottom"; const tw = g.measureText(label).width;
    g.fillText(label, Math.max(L + tw / 2, Math.min(Rr - tw / 2, x)), y + dir * 12); };
  mark(ie, tr.entry_spot, tr.direction === "LONG" ? "--long" : "--short", "tri", `${tr.direction} @ ${num(tr.entry_spot)}`);
  if (tr.partial_datetime) mark(idxAt(tr.partial_datetime), tr.partial_spot, "--partial", "dia", `partial ${tr.partial_qty} @ ${num(tr.partial_spot)}`);
  mark(ix, tr.exit_spot, tr.net_pnl >= 0 ? "--long" : "--short", "x", `${tr.exit_reason} @ ${num(tr.exit_spot)}`);
  // events inside the window
  const evs = R.events.filter(e => e.t >= T.t[i0] && e.t <= T.t[i1] && (e.trade_id == null || e.trade_id === tr.trade_id || /^(GAP_|BREAKER|DEFERRED|OVERNIGHT|FUTURES_ROLL|EXPIRY)/.test(e.e)));
  g.fillStyle = css("--muted"); g.font = "10px system-ui"; g.textAlign = "center"; g.textBaseline = "top";
  const seen = new Set();
  for (const e of evs) { if (["ENTRY_IMMEDIATE", "REENTRY_IMMEDIATE", "TRAIL_STOP", "EARLY_STOP", "PARTIAL"].includes(e.e)) continue; const x = X(idxAt(e.t)); const key = Math.round(x / 30); if (seen.has(key)) continue; seen.add(key);
    g.strokeStyle = css("--grid"); g.beginPath(); g.moveTo(x, Tp); g.lineTo(x, B); g.stroke();
    const label = e.e.replace(/_/g, " ").toLowerCase(), tw = g.measureText(label).width; g.textAlign = "center"; g.fillText(label, Math.max(L + tw / 2, Math.min(Rr - tw / 2, x)), Tp + 2); }
  // side panel
  const kv = [["Trade", `#${tr.trade_id} ${tr.direction}`], ["Entered", `${fmtT(tr.entry_datetime)} · ${tr.entry_reason}`],
    ["Entry spot / fut", `${num(tr.entry_spot)} / ${num(tr.entry_futures)}`], ["Quantity", tr.entry_qty],
    ["Partial", tr.partial_datetime ? `${fmtT(tr.partial_datetime)} · ${tr.partial_qty} @ spot ${num(tr.partial_spot)} / fut ${num(tr.partial_futures)}` : "none"],
    ["Exited", `${fmtT(tr.exit_datetime)} · ${tr.exit_reason}`], ["Exit spot / fut", `${num(tr.exit_spot)} / ${num(tr.exit_futures)} (qty ${tr.exit_qty})`],
    ["Gross / cost / net", `${money(tr.gross_pnl)} / ${money(tr.transaction_cost)} / <b class="${tr.net_pnl >= 0 ? "pos" : "neg"}">${money(tr.net_pnl)}</b>`],
    ["Points per unit", `${tr.points} (${(tr.points / tr.entry_spot * 100).toFixed(3)}% of index)`],
    ["Best / worst (MFE / MAE)", `+${num(tr.maximum_favourable_excursion, 1)} / ${num(tr.maximum_adverse_excursion, 1)} pts`],
    ["Held", `${(tr.holding_time / 3600).toFixed(1)} h · ${tr.sessions_spanned} session(s)${tr.overnight_flag ? " · overnight" : ""}`],
    ["Flags", `${tr.breaker_triggered ? "breaker " : ""}${tr.gap_regime_flag ? "gap-regime " : ""}${tr.early_stop_count ? "early-stops=" + tr.early_stop_count : ""}` || "—"]];
  document.getElementById("kv").innerHTML = kv.map(([k, v]) => `<div>${k}</div><div>${v}</div>`).join("");
  document.getElementById("ev").innerHTML = "<div><b>Engine events in this window</b></div>" + (evs.length ? evs.map(e => `<div>${fmtT(e.t)} · <b>${e.e}</b> ${e.info ? "· " + e.info : ""}</div>`).join("") : "<div>none</div>");
  const side = tr.direction === "LONG" ? "rises" : "falls", opp = tr.direction === "LONG" ? "falls" : "rises";
  document.getElementById("howto").innerHTML =
    `How to read it: the trade opened ${tr.direction} at <b>${num(E)}</b>. If price ${opp} ${P.S} points to the red line before it ever ${side} ${P.A} to the amber line, the early stop closes it and the engine reverses. Once the amber line is touched the red line is switched off for good. Touching the purple line closes half. From the moment the amber line is touched, the blue trailing stop follows the best price at a distance of ${P.T}; touching it closes the rest. Inside an amber band the engine is in the favourable-gap regime and the trailing stop is switched off (the dashed grey line shows where it would be); when the regime ends the engine rebases MFE (R6), which is why the blue line can jump. ${recorded ? "Levels are taken from the engine's recorded state, not re-derived." : "Levels are reconstructed from the rules (no recorded state)."}`;
}

function renderStatus() {
  const box = document.getElementById("status"), ban = document.getElementById("banner");
  if (!DATA.live) { box.style.display = "none"; ban.style.display = "none"; return; }
  ban.style.display = "block";
  const R = DATA.runs[run], f = R.track && R.track.final;
  if (!f) { box.style.display = "none"; return; }
  const side = f.pos > 0 ? "LONG" : f.pos < 0 ? "SHORT" : "FLAT";
  const openR = f.pos && f.last_spot != null && f.entry != null ? (f.last_spot - f.entry) * f.pos : null;
  const items = [
    ["As of (last complete minute)", fmtT(T.t[N - 1])], ["Spot / futures", `${num(T.s[N - 1])} / ${num(T.f[N - 1])}`],
    ["Position", side + (f.pos ? ` · qty ${f.qty} · since ${fmtT(f.entry_t)}` : "")],
  ];
  if (f.pos) items.push(["Entry spot / fut", `${num(f.entry)} / ${num(f.entry_fut)}`],
    ["Open P&L (spot pts)", `${openR >= 0 ? "+" : ""}${num(openR, 1)}`],
    ["Best / worst so far", `+${num(f.mfe, 1)} / ${num(f.mae, 1)}`],
    ["Early stop", f.early_stop ? `ON at ${num(f.entry - f.pos * P.S)}` : "off (armed)"],
    ["Partial", f.partial_done ? "done" : `pending at ${num(f.entry + f.pos * P.P)}`],
    ["Trailing stop", f.mfe >= P.A ? `${num(f.entry + f.pos * (f.mfe - P.T))}${f.gap_regime ? " (suspended: gap regime)" : ""}` : "not armed yet"],
    ["Breaker", f.deferred ? `ENGAGED · deferred flip at ${num(f.deferred_price)}` : `off · consecutive early stops ${f.consecutive_early_stops}`]);
  else items.push(["Next LONG entry if spot ≥", num(f.long_trigger)], ["Next SHORT entry if spot ≤", num(f.short_trigger)],
    ["Pending", f.pending_side ? `${f.pending_side > 0 ? "LONG" : "SHORT"} spot-triggered at ${num(f.pending_level)}, waiting for futures confirmation` : "none"]);
  box.style.display = "block";
  box.innerHTML = `<div class="status">${items.map(([k, v]) => `<div><div class="k">${k}</div><div class="v">${v}</div></div>`).join("")}</div>`;
}
function renderAll() { useDataset(DATA.runs[run].data); renderStatus(); renderHeader(); renderOverview(); renderEquity(); renderTable(); renderDetail(); }
window.addEventListener("resize", () => { renderOverview(); renderEquity(); renderDetail(); });
renderAll();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    sys.exit(main())
