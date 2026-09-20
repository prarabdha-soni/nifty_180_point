#!/bin/bash
# Regenerate the backtest report and deploy it to Vercel as a static page.
#   scripts/deploy_report.sh              regenerate + deploy to production
#   scripts/deploy_report.sh --no-build   deploy the existing results/report.html
# Needs VERCEL_TOKEN in .env (only that key is read; Angel One creds never
# leave the machine). The page is public to anyone with the URL.
set -euo pipefail
PROJECT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT"
TOKEN="$(awk -F= '/^VERCEL_TOKEN=/{print substr($0, index($0,"=")+1)}' .env | tr -d '\r')"
[ -n "$TOKEN" ] || { echo "VERCEL_TOKEN missing from .env" >&2; exit 1; }

if [ "${1:-}" != "--no-build" ]; then
  .venv/bin/python make_report.py --data data/nifty_3y_insample.csv \
    ALWAYS=results/insample_always FLAT_ONLY=results/insample_flat_only \
    R6_CARRY=results/insample_r6_carry \
    "ALWAYS (favourable-first)=results/insample_always_favfirst@data/nifty_3y_favfirst_insample.csv" \
    --out results/report.html
fi
cp results/report.html deploy/index.html
cd deploy
# first run creates/links the project; later runs reuse deploy/.vercel (gitignored)
if [ ! -f .vercel/project.json ]; then
  vercel link --yes --project nifty-180-report --token "$TOKEN" >/dev/null
fi
URL="$(vercel deploy --prod --yes --token "$TOKEN" 2>/dev/null | tail -1)"
echo "deployed: $URL"
echo "production alias: https://nifty-180-report.vercel.app"
