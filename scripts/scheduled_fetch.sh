#!/bin/bash
# Scheduled fetch -- pulls new bars from Angel One, rebuilds both OHLC variants,
# validates the canonical file. Installed as a launchd agent by
# scripts/install_schedule.sh; runs every weekday at 18:30 (after the session
# closes and the vendor's history for the day is settled).
#
# Why daily and not weekly: Angel One drops a contract from its API the day
# after expiry, so a weekly job would permanently lose the last 1-2 sessions of
# every expiring contract. A daily run costs ~5 API calls; everything else is
# cached under data/raw/angel/ and accumulates.
#
# This job never splits or backtests. The in-sample/holdout cut is a deliberate
# manual act (split_data.py); the holdout must not move under a schedule.
set -uo pipefail

PROJECT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"   # wherever the repo lives
PY="${PYTHON:-$PROJECT/.venv/bin/python}"
START="2023-09-20"        # fixed: history accumulates from here; do not let it slide with the date
LOG="$PROJECT/data/raw/scheduled_fetch.log"

cd "$PROJECT" || exit 1
mkdir -p data/raw
{
  echo "=== $(date '+%Y-%m-%d %H:%M:%S') start ==="
  # 1. online: new bars + new contracts, canonical OHLC (adverse-first) file
  "$PY" -W ignore fetch_data.py --start "$START" --nearest-available \
        --out data/nifty_3y.csv --report data/fetch_report.json \
        --log-file data/raw/fetch.log 2>&1 | grep -vE "\(cache\)|smartConnect"
  rc_fetch=${PIPESTATUS[0]}
  # 2. offline: the other intra-bar ordering, from the same cache
  "$PY" -W ignore fetch_data.py --start "$START" --nearest-available --offline \
        --ohlc-order favourable-first --out data/nifty_3y_favfirst.csv \
        --spot-out /dev/null --fut-out /dev/null \
        --report data/fetch_report_favfirst.json --log-file /dev/null 2>&1 | grep -E "wrote data|ERROR|Traceback"
  rc_fav=${PIPESTATUS[0]}
  # 3. validate the canonical file
  "$PY" validate_data.py data/nifty_3y.csv --json data/nifty_3y.validation.json 2>&1 | sed -n '/^Verdict/,$p'
  rc_val=${PIPESTATUS[0]}
  # 4. permanent paper page for today (from the bars just fetched; exit 3 = no session today)
  "$PY" -W ignore paper_trade.py --simulate --as-of 15:30 --no-deploy --archive 2>&1 | grep -E "wrote|completed trades|ERROR|no complete bars"
  rc_arc=${PIPESTATUS[0]}; [ "$rc_arc" = "3" ] && rc_arc=0
  echo "=== $(date '+%Y-%m-%d %H:%M:%S') done  fetch=$rc_fetch favfirst=$rc_fav validate=$rc_val archive=$rc_arc ==="
} >> "$LOG" 2>&1
exit $(( rc_fetch != 0 || rc_fav != 0 || rc_val != 0 || rc_arc != 0 ))
