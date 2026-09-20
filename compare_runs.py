#!/usr/bin/env python3
"""
compare_runs.py -- side-by-side metrics for several backtest result folders,
plus the strategy expressed in percentage-of-index terms.

    python compare_runs.py ALWAYS=results/insample_always FLAT_ONLY=results/insample_flat_only
    python compare_runs.py ALWAYS=results/x --spot data/nifty_spot_1min.csv   # + D as % of index over time

Why the percentage view: D = 180 points is a fixed distance. At NIFTY 12,000 it
is 1.5 % of the index, at 24,300 it is 0.74 %. A multi-year backtest at a fixed
point distance therefore tests a tighter strategy at high index levels than at
low ones and blends the two. This script shows, per period and per index-level
bucket, what 180 points was as a percentage and how the trades did in both
units, so the distortion can be seen rather than guessed.

Per-trade points = gross_pnl / entry_qty (the futures points actually earned
per unit, partials included). Percent = points / entry_spot.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Dict, List, Tuple

import pandas as pd

D_POINTS = 180.0

HEADLINE = [
    ("completed_trades", "trades", "{:,.0f}"),
    ("total_gross_pnl", "gross P&L (Rs)", "{:,.0f}"),
    ("total_transaction_costs", "costs (Rs)", "{:,.0f}"),
    ("total_net_pnl", "net P&L (Rs)", "{:,.0f}"),
    ("win_rate", "win rate", "{:.1%}"),
    ("average_winner", "avg winner (Rs)", "{:,.0f}"),
    ("average_loser", "avg loser (Rs)", "{:,.0f}"),
    ("profit_factor", "profit factor", "{:.2f}"),
    ("expectancy_per_trade", "expectancy/trade (Rs)", "{:,.0f}"),
    ("maximum_drawdown", "max drawdown (Rs)", "{:,.0f}"),
    ("sharpe_ratio_daily_pnl", "sharpe (daily P&L)", "{:.2f}"),
    ("sortino_ratio_daily_pnl", "sortino (daily P&L)", "{:.2f}"),
    ("maximum_consecutive_losses", "max consec. losses", "{:,.0f}"),
    ("average_holding_time_seconds", "avg hold (hours)", "{:,.1f}"),
    ("overnight_pnl", "overnight P&L (Rs)", "{:,.0f}"),
    ("long_pnl", "long P&L (Rs)", "{:,.0f}"),
    ("short_pnl", "short P&L (Rs)", "{:,.0f}"),
    ("early_stop_pnl", "early-stop P&L (Rs)", "{:,.0f}"),
    ("trailing_stop_pnl", "trail-stop P&L (Rs)", "{:,.0f}"),
    ("partial_profit_contribution", "partial contrib. (Rs)", "{:,.0f}"),
    ("breaker_contribution", "breaker contrib. (Rs)", "{:,.0f}"),
    ("gap_regime_contribution", "gap-regime contrib. (Rs)", "{:,.0f}"),
]
DIAG = ["trail_exits_winning", "trail_exits_losing", "breaker_episodes", "breaker_worst_net",
        "breaker_unprotected_seconds_total", "breaker_held_overnight", "deferred_flips_executed",
        "adverse_gap_exits", "expiry_squareoffs", "trades_with_partial", "overnight_trades",
        "top5_net_concentration"]


def load_run(path: str) -> Tuple[dict, pd.DataFrame, dict]:
    with open(os.path.join(path, "metrics.json")) as fh:
        m = json.load(fh)
    t = pd.read_csv(os.path.join(path, "trades.csv"))
    cfg = {}
    cp = os.path.join(path, "config.json")
    if os.path.exists(cp):
        with open(cp) as fh:
            cfg = json.load(fh)
    if len(t):
        t = t[t["exit_reason"] != "DATASET_END"].copy()
        t["entry_datetime"] = pd.to_datetime(t["entry_datetime"])
        t["exit_datetime"] = pd.to_datetime(t["exit_datetime"])
        t["points"] = t["gross_pnl"] / t["entry_qty"]
        t["pct"] = t["points"] / t["entry_spot"]
        t["d_pct_at_entry"] = D_POINTS / t["entry_spot"]
    return m, t, cfg


def table(rows: List[Tuple[str, List[str]]], labels: List[str], first_col: int = 30,
          col: int = 16) -> str:
    out = [" " * first_col + "".join(f"{l:>{col}}" for l in labels)]
    for name, vals in rows:
        out.append(f"{name:<{first_col}}" + "".join(f"{v:>{col}}" for v in vals))
    return "\n".join(out)


def f(fmt: str, v) -> str:
    if v is None:
        return "-"
    try:
        return fmt.format(v)
    except (ValueError, TypeError):
        return str(v)


def headline(runs: Dict[str, tuple]) -> str:
    labels = list(runs)
    rows = []
    for key, name, fmt in HEADLINE:
        vals = []
        for lab in labels:
            m = runs[lab][0]
            v = m.get(key)
            if key == "average_holding_time_seconds" and v is not None:
                v = v / 3600
            vals.append(f(fmt, v))
        rows.append((name, vals))
    rows.append(("", [""] * len(labels)))
    reasons = sorted({r for lab in labels for r in runs[lab][0].get("counts_by_exit_reason", {})})
    for r in reasons:
        rows.append((f"exits: {r}", [
            f"{runs[l][0]['counts_by_exit_reason'].get(r, 0)} / "
            f"{runs[l][0]['net_by_exit_reason'].get(r, 0):,.0f}" for l in labels]))
    rows.append(("", [""] * len(labels)))
    for k in DIAG:
        vals = []
        for lab in labels:
            v = runs[lab][0].get("diagnostics", {}).get(k)
            if k == "breaker_unprotected_seconds_total" and v is not None:
                v = f"{v / 3600:,.1f} h"
            elif isinstance(v, float):
                v = f"{v:,.2f}"
            vals.append(str(v) if v is not None else "-")
        rows.append((f"diag: {k}", vals))
    return table(rows, labels)


def config_diff(runs: Dict[str, tuple]) -> str:
    labels = list(runs)
    keys = sorted({k for lab in labels for k in runs[lab][2]})
    rows = []
    for k in keys:
        vals = [json.dumps(runs[lab][2].get(k)) for lab in labels]
        if len(set(vals)) > 1:
            rows.append((k, vals))
    if not rows:
        return "  (identical configuration)"
    return table(rows, labels)


def pct_section(runs: Dict[str, tuple]) -> str:
    labels = list(runs)
    out = []
    rows = []
    for lab in labels:
        t = runs[lab][1]
        if t.empty:
            continue
        rows.append((lab, [
            f"{len(t)}", f"{t['points'].sum():,.0f}", f"{t['points'].mean():,.1f}",
            f"{t['pct'].sum():+.2%}", f"{t['pct'].mean():+.3%}",
            f"{t['entry_spot'].min():,.0f}-{t['entry_spot'].max():,.0f}",
            f"{t['d_pct_at_entry'].min():.2%}-{t['d_pct_at_entry'].max():.2%}"]))
    out.append(table(rows, ["trades", "sum pts", "avg pts", "sum %", "avg %", "index range",
                            "D as % idx"], first_col=14, col=15))
    # by index-level bucket and by calendar period, per run
    for lab in labels:
        t = runs[lab][1]
        if t.empty:
            continue
        out.append(f"\n  [{lab}] by index level at entry (D = 180 points):")
        edges = [0, 15000, 18000, 21000, 24000, 27000, 30000, 1e9]
        names = ["<15k", "15-18k", "18-21k", "21-24k", "24-27k", "27-30k", ">30k"]
        t["bucket"] = pd.cut(t["entry_spot"], edges, labels=names, right=False)
        g = t.groupby("bucket", observed=True)
        brows = []
        for b, gg in g:
            brows.append((str(b), [
                f"{len(gg)}", f"{D_POINTS / gg['entry_spot'].mean():.2%}",
                f"{(gg['net_pnl'] > 0).mean():.0%}", f"{gg['points'].mean():,.1f}",
                f"{gg['pct'].mean():+.3%}", f"{gg['net_pnl'].sum():,.0f}"]))
        out.append(table(brows, ["trades", "D as %", "win rate", "avg pts", "avg %", "net Rs"],
                         first_col=14, col=13))
        out.append(f"\n  [{lab}] by quarter (entry date):")
        t["q"] = t["entry_datetime"].dt.to_period("Q").astype(str)
        qrows = []
        for q, gg in t.groupby("q"):
            qrows.append((q, [
                f"{len(gg)}", f"{gg['entry_spot'].mean():,.0f}",
                f"{D_POINTS / gg['entry_spot'].mean():.2%}", f"{(gg['net_pnl'] > 0).mean():.0%}",
                f"{gg['points'].sum():,.0f}", f"{gg['pct'].sum():+.2%}", f"{gg['net_pnl'].sum():,.0f}"]))
        out.append(table(qrows, ["trades", "avg index", "D as %", "win rate", "sum pts", "sum %",
                                 "net Rs"], first_col=14, col=13))
    return "\n".join(out)


def spot_section(path: str) -> str:
    s = pd.read_csv(path)
    s["timestamp"] = pd.to_datetime(s["timestamp"])
    s["q"] = s["timestamp"].dt.to_period("Q").astype(str)
    g = s.groupby("q")["ltp"].agg(["min", "mean", "max"])
    rows = [(q, [f"{r['min']:,.0f}", f"{r['mean']:,.0f}", f"{r['max']:,.0f}",
                 f"{D_POINTS / r['mean']:.2%}", f"{D_POINTS / r['max']:.2%}-{D_POINTS / r['min']:.2%}"])
            for q, r in g.iterrows()]
    lo, hi = s["ltp"].min(), s["ltp"].max()
    head = (f"  index range {lo:,.0f} .. {hi:,.0f}: D = 180 is {D_POINTS / hi:.2%} of the index at "
            f"the top and {D_POINTS / lo:.2%} at the bottom "
            f"(ratio {(D_POINTS / lo) / (D_POINTS / hi):.2f}x)\n")
    return head + table(rows, ["min", "mean", "max", "D as % (mean)", "D as % range"],
                        first_col=10, col=16)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("runs", nargs="*", help="LABEL=results_dir")
    ap.add_argument("--spot", help="full spot series (timestamp,ltp) for the D-as-%-of-index table")
    args = ap.parse_args()

    runs: Dict[str, tuple] = {}
    for spec in args.runs:
        lab, _, path = spec.partition("=")
        if not path:
            lab, path = os.path.basename(spec.rstrip("/")), spec
        runs[lab] = load_run(path)

    if runs:
        print("=" * 78)
        print("SIDE BY SIDE  (exits: count / net Rs)")
        print("=" * 78)
        print(headline(runs))
        print()
        print("config differences")
        print(config_diff(runs))
        print()
        print("=" * 78)
        print("POINTS vs PERCENT OF INDEX  (points = gross / qty; % = points / entry spot)")
        print("=" * 78)
        print(pct_section(runs))
    if args.spot:
        print()
        print("=" * 78)
        print(f"D = {D_POINTS:.0f} AS A PERCENTAGE OF THE INDEX OVER TIME  ({args.spot})")
        print("=" * 78)
        print(spot_section(args.spot))
    return 0


if __name__ == "__main__":
    sys.exit(main())
