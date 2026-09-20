#!/usr/bin/env python3
"""
Generate a synthetic multi-session NIFTY spot/futures tick file in the merged
layout, so the pipeline can be exercised end to end before real data arrives.

    python make_sample_data.py --days 40 --out data/sample_ticks.csv

THIS IS NOT MARKET DATA. It is a seeded random walk with an overnight gap
process and a futures basis. Metrics computed on it say nothing about the
strategy's edge -- it exists only to prove the engine runs, the ledger
reconciles, and the CSV contract is right.
"""

from __future__ import annotations

import argparse
import datetime as dt
import random

import pandas as pd


def generate(
    days: int = 40,
    start_date: dt.date = dt.date(2026, 1, 5),
    start_price: float = 24300.0,
    bar_seconds: int = 60,
    seed: int = 7,
    drift_per_bar: float = 0.0,
    vol_per_bar: float = 7.0,
    overnight_vol: float = 60.0,
    basis: float = 22.0,
) -> pd.DataFrame:
    rng = random.Random(seed)
    rows = []
    price = start_price
    d = start_date
    sessions = 0

    # monthly expiry = last Thursday of the month, approximated for the sample
    def month_expiry(day: dt.date) -> dt.date:
        nxt = (day.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
        last = nxt - dt.timedelta(days=1)
        while last.weekday() != 3:
            last -= dt.timedelta(days=1)
        return last

    while sessions < days:
        if d.weekday() >= 5:
            d += dt.timedelta(days=1)
            continue

        if sessions > 0:
            price += rng.gauss(0, overnight_vol)

        expiry = month_expiry(d)
        if d > expiry:
            nxt = (d.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
            expiry = month_expiry(nxt)

        t = dt.datetime.combine(d, dt.time(9, 15, 0))
        end = dt.datetime.combine(d, dt.time(15, 30, 0))
        drift_today = rng.gauss(0, 0.35)          # some days trend, some chop

        while t <= end:
            price += rng.gauss(drift_per_bar + drift_today, vol_per_bar)
            fut = price + basis + rng.gauss(0, 1.2)
            rows.append(
                {
                    "timestamp": t.strftime("%Y-%m-%d %H:%M:%S"),
                    "trade_date": d.isoformat(),
                    "spot_ltp": round(price, 2),
                    "futures_ltp": round(fut, 2),
                    "futures_contract_expiry": expiry.isoformat(),
                }
            )
            t += dt.timedelta(seconds=bar_seconds)

        sessions += 1
        d += dt.timedelta(days=1)

    return pd.DataFrame(rows)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=40)
    ap.add_argument("--bar-seconds", type=int, default=60)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", default="data/sample_ticks.csv")
    a = ap.parse_args()

    df = generate(days=a.days, bar_seconds=a.bar_seconds, seed=a.seed)
    import os
    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    df.to_csv(a.out, index=False)
    print(f"{len(df):,} rows, {df['trade_date'].nunique()} sessions -> {a.out}")
    print(f"spot range {df['spot_ltp'].min():.0f} to {df['spot_ltp'].max():.0f}")


if __name__ == "__main__":
    main()
