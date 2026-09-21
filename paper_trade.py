#!/usr/bin/env python3
"""
paper_trade.py -- run the V1 engine on TODAY's candles as they arrive and
publish the result as a paper-trading page. Nothing is traded: the only API
call is getCandleData (read-only); this project contains no order code.

    python paper_trade.py                       # today, live candles, deploy /paper
    python paper_trade.py --no-deploy           # same, local only
    python paper_trade.py --date 2026-09-18 --simulate --as-of 11:30   # dry run from cache

Each run is a full, continuous re-run from --start (default 2026-09-18, the
first paper day) through --date:
  * the trading day before --start is the warm-up (reference levels only)
  * every session from --start to yesterday comes from the bar cache
    (data/raw/angel/, committed daily by the fetch job); today's bars come
    from the API, up to the last COMPLETE minute (the forming minute is
    dropped, so a trade never appears on a bar whose close is not known)
  * positions carry overnight exactly as the strategy intends -- Monday opens
    with whatever Friday left open, and the gap rules apply at 09:16.
    The run does NOT reach back before --start: reconstructing the state the
    strategy would have had from the holdout period is off limits.
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

PY = sys.executable


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--date", type=dt.date.fromisoformat, default=dt.date.today())
    ap.add_argument("--start", type=dt.date.fromisoformat, default=dt.date(2026, 9, 18),
                    help="first paper trading day; the run is continuous from here (default 2026-09-18)")
    ap.add_argument("--as-of", default=None, help="HH:MM -- ignore bars at/after this time")
    ap.add_argument("--simulate", action="store_true", help="offline: bars from the chunk cache only")
    ap.add_argument("--no-deploy", action="store_true")
    ap.add_argument("--archive", action="store_true",
                    help="also write deploy/paper/<date>.html (permanent page for the day)")
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

    start = min(args.start, date)
    today = dt.date.today()
    # one spot request covering the run plus a lead-in, so the reference day is
    # derived from the same series (a second identical request within a second
    # trips Angel One's burst filter, then it refuses for minutes)
    spot_all = fd.fetch_series(client, cache, spot_c, start - dt.timedelta(days=10), date, 25,
                               args.simulate, today)
    days = sorted({t.date() for t in spot_all["timestamp"]}) if len(spot_all) else []
    before = [d for d in days if d < start]
    if not before:
        raise fd.FetchError("no bars found in the 10 days before the paper start -- cannot build references")
    ref_day = before[-1]
    spot = spot_all[spot_all["timestamp"].dt.date >= ref_day].reset_index(drop=True)
    log.info("warm-up %s, paper run %s .. %s (continuous, positions carried overnight)", ref_day, start, date)

    fut_by = {}
    for c in contracts[:3]:
        c_start = max(ref_day, c.expiry - dt.timedelta(days=120))
        if c_start > date:
            continue
        fut_by[c] = fd.fetch_series(client, cache, c, c_start, date, 25, args.simulate, today)
    spot = spot[spot["timestamp"] < cutoff].reset_index(drop=True)
    for c in fut_by:
        fut_by[c] = fut_by[c][fut_by[c]["timestamp"] < cutoff].reset_index(drop=True)
    n_today = int((spot["timestamp"].dt.date == date).sum())
    n_days = spot["timestamp"].dt.date.nunique()
    log.info("spot bars: %d sessions, %d bars today", n_days, n_today)
    if n_today == 0 and date == today:
        log.warning("no complete bars for %s yet -- nothing to run", date)
        return 3

    fut, stitch = fd.assign_contracts(fut_by, contracts, nearest_available=False, near_month_from=None)
    if not len(fut):
        raise fd.FetchError("no near-month futures bars for these days")
    merged, rep = fd.align(spot, fut, "close")
    out_dir = os.path.join(HERE, "results", "paper", date.isoformat())
    os.makedirs(out_dir, exist_ok=True)
    day_csv = os.path.join(out_dir, "day.csv")
    fd.write_merged(fd.expand_ohlc(merged, args.ohlc_order), day_csv)
    # the other intra-minute ordering, so the page shows the bar-treatment band
    other = "favourable-first" if args.ohlc_order == "adverse-first" else "adverse-first"
    day_csv_other = os.path.join(out_dir, "day_other.csv")
    fd.write_merged(fd.expand_ohlc(merged, other), day_csv_other)
    contract_used = sorted(set(fut["contract"]))
    log.info("aligned %d minutes -> %s (contract %s)", rep["aligned_minutes"], day_csv, contract_used)

    # engine runs, via run_backtest.py so outputs are byte-identical to a normal run
    runs = {"always": (os.path.join(HERE, "overrides", "paper_always.json"), day_csv),
            "flat_only": (os.path.join(HERE, "overrides", "paper_flat_only.json"), day_csv),
            "always_other": (os.path.join(HERE, "overrides", "paper_always.json"), day_csv_other)}
    for name, (cfg, csv) in runs.items():
        rd = os.path.join(out_dir, name)
        os.makedirs(rd, exist_ok=True)
        with open(os.path.join(rd, "run.log"), "w") as fh:
            rc = subprocess.call([PY, os.path.join(HERE, "run_backtest.py"), "--merged", csv,
                                  "--config", cfg, "--out", rd], stdout=fh, stderr=subprocess.STDOUT)
        if rc != 0:
            raise fd.FetchError(f"engine run {name} failed (see {rd}/run.log)")
        m = json.load(open(os.path.join(rd, "metrics.json")))
        log.info("[%s] completed trades %d, net %s", name, m.get("completed_trades", m.get("trades", 0)),
                 f"{m.get('total_net_pnl', 0):,.0f}")

    title = (f"PAPER TEST — since {start} · as of {date} {cutoff.strftime('%H:%M')}"
             + (" · DRY RUN from cached bars" if args.simulate else ""))
    page = os.path.join(HERE, "deploy", "paper.html")
    arch_dir = os.path.join(HERE, "deploy", "paper")
    os.makedirs(arch_dir, exist_ok=True)
    rel = os.path.relpath(day_csv, HERE)
    targets = [page] + ([os.path.join(arch_dir, f"{date.isoformat()}.html")] if args.archive else [])
    for out in targets:
        rc = subprocess.call([PY, os.path.join(HERE, "make_report.py"), "--data", rel, "--live",
                              "--title", title, "--archive-dir", arch_dir,
                              f"ALWAYS={os.path.relpath(os.path.join(out_dir, 'always'), HERE)}",
                              f"FLAT_ONLY={os.path.relpath(os.path.join(out_dir, 'flat_only'), HERE)}",
                              f"ALWAYS ({other})={os.path.relpath(os.path.join(out_dir, 'always_other'), HERE)}"
                              f"@{os.path.relpath(day_csv_other, HERE)}",
                              "--out", out], cwd=HERE)
        if rc != 0:
            raise fd.FetchError("make_report failed")
    with open(os.path.join(out_dir, "status.json"), "w") as fh:
        json.dump({"date": date.isoformat(), "start": start.isoformat(), "as_of": cutoff.isoformat(timespec="minutes"),
                   "bars_today": n_today, "sessions": n_days, "contract": contract_used, "simulate": args.simulate,
                   "generated": now.isoformat(timespec="seconds")}, fh, indent=2)

    if not args.no_deploy:
        token = ""
        with open(os.path.join(HERE, ".env")) as fh:
            for line in fh:
                if line.startswith("VERCEL_TOKEN="):
                    token = line.split("=", 1)[1].strip()
        if not token:
            raise fd.FetchError("VERCEL_TOKEN missing from .env")
        ddir = os.path.join(HERE, "deploy")
        # a Vercel deploy replaces every file, so the main report must ride along.
        # On a fresh checkout (CI) it is not on disk: take the live copy, and refuse
        # to deploy if that fails rather than publish a site without it.
        idx = os.path.join(ddir, "index.html")
        if not os.path.exists(idx):
            got = subprocess.run(["curl", "-sf", "-o", idx, "https://nifty-180-report.vercel.app/"],
                                 capture_output=True)
            if got.returncode != 0 or not os.path.exists(idx) or os.path.getsize(idx) < 100_000:
                raise fd.FetchError("could not fetch the live index.html to keep it in the deploy")
        if not os.path.exists(os.path.join(ddir, ".vercel", "project.json")):
            lk = subprocess.run(["vercel", "link", "--yes", "--project", "nifty-180-report",
                                 "--token", token], cwd=ddir, capture_output=True, text=True)
            if lk.returncode != 0:
                raise fd.FetchError(f"vercel link failed: {lk.stderr[-300:]}")
        out = subprocess.run(["vercel", "deploy", "--prod", "--yes", "--token", token],
                             cwd=ddir, capture_output=True, text=True)
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
