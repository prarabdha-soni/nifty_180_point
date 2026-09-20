#!/usr/bin/env python3
"""
NIFTY 180-Point Swing Strategy -- V1 backtest runner.

Merged layout (spec section 3's minimum record):
    python run_backtest.py --merged data/ticks.csv --out results/

    timestamp,trade_date,spot_ltp,futures_ltp,futures_contract_expiry
    2026-01-05 09:16:00,2026-01-05,24350.10,24371.25,2026-01-29

Split layout (two files, what most tick vendors ship):
    python run_backtest.py --spot data/spot.csv --futures data/fut.csv --out results/

    spot.csv      timestamp,ltp
    futures.csv   timestamp,ltp,expiry

Options:
    --config overrides.json     override any Config field
    --references refs.csv       supply previous-session levels explicitly
    --no-warmup                 trade the first session too (needs --references)

Outputs written to --out:
    trades.csv          the spec 15.1 trade ledger
    executions.csv      every simulated futures fill with its cost
    events.csv          engine event log (entries, stops, breaker, gaps, rolls)
    metrics.json        the spec 15.2 metrics plus diagnostics
    config.json         the exact configuration used, for reproducibility
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import replace

import pandas as pd

from strategy.config import Config
from strategy.data import MarketData
from strategy.backtester import Backtester
from strategy.metrics import compute_metrics, format_metrics


def load_config(path: str | None) -> Config:
    cfg = Config()
    if path:
        with open(path) as fh:
            overrides = json.load(fh)
        unknown = set(overrides) - set(cfg.__dataclass_fields__)
        if unknown:
            raise SystemExit(f"unknown config fields: {sorted(unknown)}")
        if "tie_break_order" in overrides:
            overrides["tie_break_order"] = tuple(overrides["tie_break_order"])
        cfg = replace(cfg, **overrides)
    cfg.validate()
    return cfg


def main() -> int:
    ap = argparse.ArgumentParser(description="NIFTY 180-Point Swing Strategy V1 backtest")
    ap.add_argument("--merged")
    ap.add_argument("--spot")
    ap.add_argument("--futures")
    ap.add_argument("--references")
    ap.add_argument("--config")
    ap.add_argument("--out", default="results")
    ap.add_argument("--no-warmup", action="store_true")
    args = ap.parse_args()

    if not args.merged and not (args.spot and args.futures):
        ap.error("provide --merged, or both --spot and --futures")

    cfg = load_config(args.config)
    if args.no_warmup:
        cfg = replace(cfg, warmup_first_session=False)

    print("Loading market data...")
    if args.merged:
        market = MarketData.from_merged_csv(args.merged, cfg)
    else:
        market = MarketData.from_split_csv(args.spot, args.futures, cfg)
    if args.references:
        market.apply_reference_override(args.references)

    sessions = len(market.references)
    print(f"  {len(market.events):,} events across {sessions} sessions")
    print(f"  {min(market.references)} to {max(market.references)}")

    print("Running V1 engine...")
    bt = Backtester(cfg)
    result = bt.run(market)

    os.makedirs(args.out, exist_ok=True)

    trades = pd.DataFrame([t.as_dict() for t in result.trades])
    trades.to_csv(os.path.join(args.out, "trades.csv"), index=False)

    execs = pd.DataFrame([vars(e) for e in result.executions])
    execs.to_csv(os.path.join(args.out, "executions.csv"), index=False)

    events = pd.DataFrame(result.events_log)
    events.to_csv(os.path.join(args.out, "events.csv"), index=False)

    metrics = compute_metrics(result.trades, cfg, result.executions)
    with open(os.path.join(args.out, "metrics.json"), "w") as fh:
        json.dump(metrics, fh, indent=2, default=str)
    with open(os.path.join(args.out, "config.json"), "w") as fh:
        fh.write(cfg.to_json())

    print()
    print(format_metrics(metrics))
    print()

    r = result.reconciliation
    tag = "PASS" if r["all_ok"] else "FAIL"
    print(f"Reconciliation (spec 18): {tag}")
    for k in ("cost_reconciles", "net_reconciles", "quantity_reconciles"):
        print(f"  {k:<26} {r[k]}")

    if result.warnings:
        print("\nWarnings:")
        for w in result.warnings:
            print(f"  - {w}")

    print(f"\nWritten to {args.out}/")
    return 0 if r["all_ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
