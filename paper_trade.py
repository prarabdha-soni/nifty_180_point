#!/usr/bin/env python3
"""
paper_trade.py -- run the V1 engine on TODAY's candles as they arrive and
publish the result as a paper-trading page. Nothing is traded: the only API
call is getCandleData (read-only); this project contains no order code.

    python paper_trade.py                       # today, live candles, deploy /paper
    python paper_trade.py --no-deploy           # same, local only
    python paper_trade.py --date 2026-09-18 --simulate --as-of 11:30   # dry run from cache

Each run is a full re-run from a clean state:
  * reference session = the previous trading day (its bars give the
    previous-session high/low/close the strategy needs); it is a warm-up,
    no trading in it
  * trading session  = --date, from 09:15 to the last COMPLETE minute
    (the still-forming minute is dropped, so a trade never appears on a bar
    whose close is not known yet)
  * the engine starts FLAT at the open. A position the strategy would have
    been carrying from earlier days is not reconstructed -- that would mean
    running the holdout period, which is off limits.
  * two runs: the V1 default (extreme_tracking=ALWAYS) and FLAT_ONLY (R2),
    both with close_at_dataset_end=False so an open position stays open in
    the ledger instead of being closed as DATASET_END.

Outputs: results/paper/<date>/{day.csv, always/, flat_only/}, deploy/paper.html
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import fetch_data as fd  # noqa: E402

PY = os.path.join(HERE, ".venv", "bin", "python")


def previous_trading_day(cache: fd.ChunkCache, spot: fd.Contract, date: dt.date,
                         offline: bool, client) -> dt.date:
    """Latest date before `date` that has spot bars (cache first, then API)."""
    df = fd.fetch_series(client, cache, spot, date - dt.timedelta(days=10), date - dt.timedelta(days=1),
                         25, offline or client is None, dt.date.today())
    days = sorted({t.date() for t in df["timestamp"]}) if len(df) else []
    if not days:
        raise fd.FetchError("no bars found in the 10 days before the session -- cannot build references")
    return days[-1]


def day_bars(client, cache: fd.ChunkCache, contract: fd.Contract, day: dt.date,
             offline: bool) -> pd.DataFrame:
    """One session's 1-min bars: from the cache when offline, else straight from the API."""
    if offline:
        df = fd.fetch_series(None, cache, contract, day, day, 25, True, dt.date.today())
    else:
        start = dt.datetime.combine(day, dt.time(9, 15))
        end = min(dt.datetime.combine(day, dt.time(15, 30)), dt.datetime.now().replace(second=0, microsecond=0))
        if end <= start:                       # session has not started yet (API rejects future ranges)
            return fd.candles_to_frame([])
        data = client.candles(contract.exchange, contract.token, start, end)
        df = fd.candles_to_frame(data)
    if len(df):
        df = df[df["timestamp"].dt.date == day].reset_index(drop=True)
    return df


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--date", type=dt.date.fromisoformat, default=dt.date.today())
    ap.add_argument("--as-of", default=None, help="HH:MM -- ignore bars at/after this time")
    ap.add_argument("--simulate", action="store_true", help="offline: bars from the chunk cache only")
    ap.add_argument("--no-deploy", action="store_true")
    ap.add_argument("--cache-dir", default=os.path.join(HERE, "data", "raw", "angel"))
    ap.add_argument("--ohlc-order", choices=("adverse-first", "favourable-first"), default="adverse-first")
    args = ap.parse_args()

    fd.setup_logging(os.path.join(HERE, "data", "raw", "paper.log"))
    log = fd.LOG
    now = dt.datetime.now()
    date = args.date
    if args.as_of:
        h, m = (int(x) for x in args.as_of.split(":"))
        cutoff = dt.datetime.combine(date, dt.time(h, m))
    else:
        cutoff = now.replace(second=0, microsecond=0)          # drop the forming minute
    log.info("paper session %s  as-of %s  (%s)", date, cutoff.strftime("%H:%M"),
             "SIMULATE from cache" if args.simulate else "live candles")

    client = None
    if not args.simulate:
        client = fd.AngelClient(fd.Credentials.from_env(os.path.join(HERE, ".env")),
                                session_cache=os.path.join(HERE, "data", "raw", "angel_session.json"))
        client.login()
    master = fd.ScripMaster.load(args.cache_dir, offline=args.simulate)
    cache = fd.ChunkCache(args.cache_dir)
    spot_c = master.nifty_spot()
    live = [c for c in master.nifty_futures() if c.expiry >= date]
    recovered = [c for c in cache.contracts_on_disk("NFO") if c.expiry >= date
                 and (c.token, c.expiry) not in {(x.token, x.expiry) for x in live}]
    contracts = sorted(live + recovered, key=lambda c: c.expiry)
    if not contracts:
        raise fd.FetchError("no futures contract with expiry >= session date")

    ref_day = previous_trading_day(cache, spot_c, date, args.simulate, client)
    log.info("reference session %s, trading session %s", ref_day, date)

    # bars for both days, spot + every candidate contract (near-month chosen below)
    spot = pd.concat([day_bars(client, cache, spot_c, ref_day, args.simulate),
                      day_bars(client, cache, spot_c, date, args.simulate)], ignore_index=True)
    fut_by = {}
    for c in contracts[:3]:
        parts = [day_bars(client, cache, c, ref_day, args.simulate),
                 day_bars(client, cache, c, date, args.simulate)]
        fut_by[c] = pd.concat([p for p in parts if len(p)], ignore_index=True) if any(len(p) for p in parts) \
            else fd.candles_to_frame([])
    spot = spot[spot["timestamp"] < cutoff].reset_index(drop=True)
    for c in fut_by:
        fut_by[c] = fut_by[c][fut_by[c]["timestamp"] < cutoff].reset_index(drop=True)
    n_today = int((spot["timestamp"].dt.date == date).sum())
    log.info("spot bars: %d reference, %d today", len(spot) - n_today, n_today)
    if n_today == 0:
        log.warning("no complete bars for %s yet -- nothing to run", date)
        return 3

    fut, stitch = fd.assign_contracts(fut_by, contracts, nearest_available=False, near_month_from=None)
    if not len(fut):
        raise fd.FetchError("no near-month futures bars for these days")
    merged, rep = fd.align(spot, fut, "close")
    merged = fd.expand_ohlc(merged, args.ohlc_order)
    out_dir = os.path.join(HERE, "results", "paper", date.isoformat())
    os.makedirs(out_dir, exist_ok=True)
    day_csv = os.path.join(out_dir, "day.csv")
    fd.write_merged(merged, day_csv)
    contract_used = sorted(set(fut["contract"]))
    log.info("aligned %d minutes -> %s (contract %s)", rep["aligned_minutes"], day_csv, contract_used)

    # engine runs, via run_backtest.py so outputs are byte-identical to a normal run
    runs = {"always": os.path.join(HERE, "overrides", "paper_always.json"),
            "flat_only": os.path.join(HERE, "overrides", "paper_flat_only.json")}
    for name, cfg in runs.items():
        rd = os.path.join(out_dir, name)
        os.makedirs(rd, exist_ok=True)
        with open(os.path.join(rd, "run.log"), "w") as fh:
            rc = subprocess.call([PY, os.path.join(HERE, "run_backtest.py"), "--merged", day_csv,
                                  "--config", cfg, "--out", rd], stdout=fh, stderr=subprocess.STDOUT)
        if rc != 0:
            raise fd.FetchError(f"engine run {name} failed (see {rd}/run.log)")
        m = json.load(open(os.path.join(rd, "metrics.json")))
        log.info("[%s] completed trades %d, net %s", name, m.get("completed_trades", m.get("trades", 0)),
                 f"{m.get('total_net_pnl', 0):,.0f}")

    title = (f"PAPER TEST — {date} · as of {cutoff.strftime('%H:%M')}"
             + (" · DRY RUN from cached bars" if args.simulate else ""))
    page = os.path.join(HERE, "deploy", "paper.html")
    rel = os.path.relpath(day_csv, HERE)
    rc = subprocess.call([PY, os.path.join(HERE, "make_report.py"), "--data", rel, "--live",
                          "--title", title, f"ALWAYS={os.path.relpath(os.path.join(out_dir, 'always'), HERE)}",
                          f"FLAT_ONLY={os.path.relpath(os.path.join(out_dir, 'flat_only'), HERE)}", "--out", page],
                         cwd=HERE)
    if rc != 0:
        raise fd.FetchError("make_report failed")
    with open(os.path.join(out_dir, "status.json"), "w") as fh:
        json.dump({"date": date.isoformat(), "as_of": cutoff.isoformat(timespec="minutes"),
                   "bars_today": n_today, "contract": contract_used, "simulate": args.simulate,
                   "generated": now.isoformat(timespec="seconds")}, fh, indent=2)

    if not args.no_deploy:
        token = ""
        with open(os.path.join(HERE, ".env")) as fh:
            for line in fh:
                if line.startswith("VERCEL_TOKEN="):
                    token = line.split("=", 1)[1].strip()
        if not token:
            raise fd.FetchError("VERCEL_TOKEN missing from .env")
        out = subprocess.run(["vercel", "deploy", "--prod", "--yes", "--token", token],
                             cwd=os.path.join(HERE, "deploy"), capture_output=True, text=True)
        if out.returncode != 0:
            raise fd.FetchError(f"vercel deploy failed: {out.stderr[-400:]}")
        log.info("deployed -> https://nifty-180-report.vercel.app/paper")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except fd.FetchError as e:
        fd.LOG.error("%s", e)
        sys.exit(1)
