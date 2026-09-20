#!/usr/bin/env python3
"""
split_data.py -- carve a merged engine CSV into in-sample and holdout by date.

    python split_data.py data/nifty_3y.csv --holdout-months 12

Holdout = the final N months (by trade_date, ending at the last session in the
file). In-sample = everything before. The holdout file is written read-only
(mode 0444) as a reminder that it is not to be run until the in-sample work is
signed off. Exits 3 if the in-sample side is empty.
"""

from __future__ import annotations

import argparse
import os
import sys

import pandas as pd


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--holdout-months", type=int, default=12)
    ap.add_argument("--insample-out", default=None)
    ap.add_argument("--holdout-out", default=None)
    args = ap.parse_args()

    base, ext = os.path.splitext(args.path)
    ins_path = args.insample_out or f"{base}_insample{ext}"
    hold_path = args.holdout_out or f"{base}_holdout{ext}"

    df = pd.read_csv(args.path)
    dates = pd.to_datetime(df["trade_date"])
    last = dates.max()
    holdout_start = (last - pd.DateOffset(months=args.holdout_months)) + pd.Timedelta(days=1)
    ins = df[dates < holdout_start]
    hold = df[dates >= holdout_start]

    print(f"input      {args.path}: {len(df):,} rows, {dates.nunique()} sessions, "
          f"{dates.min().date()} .. {last.date()}")
    print(f"holdout    final {args.holdout_months} months -> from {holdout_start.date()}")
    print(f"in-sample  {len(ins):,} rows, {pd.to_datetime(ins['trade_date']).nunique()} sessions"
          + (f", {pd.to_datetime(ins['trade_date']).min().date()} .. "
             f"{pd.to_datetime(ins['trade_date']).max().date()}" if len(ins) else ""))
    print(f"holdout    {len(hold):,} rows, {pd.to_datetime(hold['trade_date']).nunique()} sessions"
          + (f", {pd.to_datetime(hold['trade_date']).min().date()} .. "
             f"{pd.to_datetime(hold['trade_date']).max().date()}" if len(hold) else ""))

    if os.path.exists(hold_path):
        os.chmod(hold_path, 0o644)
    hold.to_csv(hold_path, index=False)
    os.chmod(hold_path, 0o444)
    print(f"wrote      {hold_path} (read-only) -- DO NOT RUN until in-sample is signed off")

    if ins.empty:
        print(f"\nIN-SAMPLE IS EMPTY: the file holds less than {args.holdout_months} months, "
              f"so the whole of it falls inside the holdout window. Nothing to backtest.")
        if os.path.exists(ins_path):
            os.remove(ins_path)
        return 3
    ins.to_csv(ins_path, index=False)
    print(f"wrote      {ins_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
