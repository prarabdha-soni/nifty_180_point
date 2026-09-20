#!/bin/bash
# Paper-trading poll: run paper_trade.py on today's candles and publish /paper.
# Fired every 5 minutes by launchd (com.nifty180.paper); exits at once outside
# market hours (Mon-Fri 09:17-15:45 IST) unless FORCE=1. Never places orders.
set -uo pipefail
PROJECT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG="$PROJECT/data/raw/paper_day.log"
cd "$PROJECT" || exit 1
mkdir -p data/raw
dow=$(date +%u); hm=$(date +%H%M)
if [ "${FORCE:-0}" != "1" ] && { [ "$dow" -gt 5 ] || [ "$hm" -lt 0917 ] || [ "$hm" -gt 1545 ]; }; then
  exit 0
fi
{
  echo "=== $(date '+%Y-%m-%d %H:%M:%S') poll ==="
  .venv/bin/python -W ignore paper_trade.py "$@" 2>&1 | grep -vE "\(cache\)|non-final|chunks cached|smartConnect"
  rc=${PIPESTATUS[0]}
  echo "=== exit $rc ==="
} >> "$LOG" 2>&1
exit "$rc"
