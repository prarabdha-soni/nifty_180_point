#!/usr/bin/env python3
"""
make_proxy_data.py -- 3-year NIFTY spot proxy for V2 research (V2_RESEARCH.md).

    python make_proxy_data.py                    # TRAIN, VALIDATE, CALIBRATION
    python make_proxy_data.py --windows HOLDOUT-P   # only at the final step

Reads the cached 1-minute NIFTY 50 bars (data/raw/angel/, offline, no API
calls), sets futures = spot on every bar, expands each bar to four ticks under
both intra-bar orderings (fetch_data.expand_ohlc) and writes

    data/proxy_<WINDOW>.csv            adverse-first
    data/proxy_<WINDOW>_favfirst.csv   favourable-first

Each file starts with the session before the window (the engine's warm-up),
except CALIBRATION, which starts on 2026-06-01 to match the real-futures
in-sample run it is compared with. The files have NO futures_contract_expiry
column: no expiry square-off, no roll. (An empty expiry would read as NaT,
which compares unequal to itself and would fake a roll every session.)
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import fetch_data as fd  # noqa: E402

WINDOWS = {
    "TRAIN": ("2023-09-21", "2025-06-30"),
    "VALIDATE": ("2025-07-01", "2025-12-31"),
    "HOLDOUT-P": ("2026-01-01", "2026-05-29"),
    "CALIBRATION": ("2026-06-01", "2026-08-18"),
}
NEVER_BEFORE = dt.date(2026, 8, 19)     # original futures holdout starts here: never touched


def load_spot(cache_dir: str) -> pd.DataFrame:
    master = fd.ScripMaster.load(cache_dir, offline=True)
    spot_c = master.nifty_spot()
    cache = fd.ChunkCache(cache_dir)
    df = fd.fetch_series(None, cache, spot_c, dt.date(2023, 9, 1), NEVER_BEFORE - dt.timedelta(days=1),
                         25, True, dt.date.today())
    return fd.session_filter(df).reset_index(drop=True)


def build(spot: pd.DataFrame, window: str, out_dir: str) -> list:
    a, b = (dt.date.fromisoformat(x) for x in WINDOWS[window])
    if b >= NEVER_BEFORE:
        raise SystemExit(f"{window} reaches into the untouched futures holdout")
    days = sorted(set(spot["timestamp"].dt.date))
    before = [d for d in days if d < a]
    start = a if window == "CALIBRATION" else before[-1]      # warm-up session
    sub = spot[(spot["timestamp"].dt.date >= start) & (spot["timestamp"].dt.date <= b)].copy()
    m = pd.DataFrame({"timestamp": sub["timestamp"].values})
    m["trade_date"] = sub["timestamp"].dt.date.values
    for c in fd.OHLC:
        m[f"s_{c}"] = sub[c].values
        m[f"f_{c}"] = sub[c].values                            # futures = spot
    m["futures_contract_expiry"] = None
    m["contract"] = "PROXY"
    m["assignment"] = "PROXY"
    written = []
    for order, suffix in (("adverse-first", ""), ("favourable-first", "_favfirst")):
        ex = fd.expand_ohlc(m, order)
        out = ex[["timestamp", "trade_date", "spot_ltp", "futures_ltp"]].copy()
        out["timestamp"] = out["timestamp"].dt.strftime(fd.OUT_TS_FMT)
        out["trade_date"] = out["trade_date"].astype(str)
        path = os.path.join(out_dir, f"proxy_{window}{suffix}.csv")
        out.to_csv(path, index=False, float_format="%.2f")
        written.append(path)
    n_sess = sub["timestamp"].dt.date.nunique()
    print(f"{window:12} {start} .. {b}: {n_sess} sessions ({n_sess - 1} trading + warm-up), "
          f"{len(sub):,} bars -> {len(sub) * 4:,} ticks each: {', '.join(os.path.basename(p) for p in written)}")
    return written


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--windows", nargs="+", default=["TRAIN", "VALIDATE", "CALIBRATION"],
                    choices=list(WINDOWS))
    ap.add_argument("--cache-dir", default=os.path.join(HERE, "data", "raw", "angel"))
    ap.add_argument("--out-dir", default=os.path.join(HERE, "data"))
    args = ap.parse_args()
    import logging
    logging.basicConfig(level=logging.WARNING)
    spot = load_spot(args.cache_dir)
    print(f"spot cache: {len(spot):,} bars, {spot['timestamp'].iloc[0]} .. {spot['timestamp'].iloc[-1]}")
    for w in args.windows:
        build(spot, w, args.out_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
