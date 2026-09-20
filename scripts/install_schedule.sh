#!/bin/bash
# Install (or reinstall) the launchd agents defined by scripts/*.plist:
#   com.nifty180.fetch  weekday 18:30 data fetch        (scheduled_fetch.sh)
#   com.nifty180.paper  every 10 min, market hours only (paper_day.sh)
#   scripts/install_schedule.sh            install / reload all
#   scripts/install_schedule.sh --remove   uninstall all
#   scripts/install_schedule.sh --run-now  install and fire the fetch job once
set -eu
SCRIPTS="$(cd "$(dirname "$0")" && pwd)"
PROJECT="$(cd "$SCRIPTS/.." && pwd)"
DOMAIN="gui/$(id -u)"
NODEBIN="$(dirname "$(command -v vercel 2>/dev/null || echo /usr/local/bin/vercel)")"
mkdir -p "$HOME/Library/LaunchAgents"
case "$PROJECT" in
  "$HOME/Downloads"*|"$HOME/Desktop"*|"$HOME/Documents"*)
    echo "NOTE: $PROJECT is inside a macOS-protected folder; launchd jobs are denied access there." ;;
esac
for SRC in "$SCRIPTS"/*.plist; do
  LABEL="$(basename "$SRC" .plist)"
  DST="$HOME/Library/LaunchAgents/$LABEL.plist"
  launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
  if [ "${1:-}" = "--remove" ]; then rm -f "$DST"; echo "removed $LABEL"; continue; fi
  sed -e "s#__PROJECT__#$PROJECT#g" -e "s#__NODEBIN__#$NODEBIN#g" "$SRC" > "$DST"
  launchctl bootstrap "$DOMAIN" "$DST"
  launchctl enable "$DOMAIN/$LABEL"
  echo "installed $LABEL"
done
if [ "${1:-}" = "--run-now" ]; then launchctl kickstart -k "$DOMAIN/com.nifty180.fetch"; echo "kickstarted fetch; see data/raw/scheduled_fetch.log"; fi
