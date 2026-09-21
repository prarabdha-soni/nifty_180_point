#!/bin/bash
# Tick recorder wrapper -- runs tick_recorder.py until 15:31 IST. Fired by
# launchd (com.nifty180.ticks) at 09:10 Mon-Fri; exits at once on weekends or
# after 15:31 unless FORCE=1. Read-only market-data stream; no order code.
set -uo pipefail
PROJECT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${PYTHON:-$PROJECT/.venv/bin/python}"
LOG="$PROJECT/data/raw/record_ticks.log"
cd "$PROJECT" || exit 1
mkdir -p data/raw
dow=$(date +%u); hm=$(date +%H%M)
if [ "${FORCE:-0}" != "1" ] && { [ "$dow" -gt 5 ] || [ "$hm" -gt 1531 ]; }; then exit 0; fi
{
  echo "=== $(date '+%Y-%m-%d %H:%M:%S') start ==="
  "$PY" -W ignore tick_recorder.py "$@" 2>&1 | grep -vE "smartConnect|smartWebSocket|websocket_client"
  echo "=== $(date '+%Y-%m-%d %H:%M:%S') exit ${PIPESTATUS[0]} ==="
} >> "$LOG" 2>&1
