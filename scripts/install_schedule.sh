#!/bin/bash
# Install (or reinstall) the launchd agent that runs scripts/scheduled_fetch.sh.
#   scripts/install_schedule.sh            install / reload
#   scripts/install_schedule.sh --remove   uninstall
#   scripts/install_schedule.sh --run-now  install and fire one run immediately
set -eu
SCRIPTS="$(cd "$(dirname "$0")" && pwd)"
PROJECT="$(cd "$SCRIPTS/.." && pwd)"
SRC="$SCRIPTS/com.nifty180.fetch.plist"          # template with __PROJECT__ placeholders
DST="$HOME/Library/LaunchAgents/com.nifty180.fetch.plist"
LABEL="com.nifty180.fetch"
DOMAIN="gui/$(id -u)"
launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
if [ "${1:-}" = "--remove" ]; then rm -f "$DST"; echo "removed $LABEL"; exit 0; fi
mkdir -p "$HOME/Library/LaunchAgents"
sed "s#__PROJECT__#$PROJECT#g" "$SRC" > "$DST"
case "$PROJECT" in
  "$HOME/Downloads"*|"$HOME/Desktop"*|"$HOME/Documents"*)
    echo "NOTE: $PROJECT is inside a macOS-protected folder. launchd jobs are denied access there"
    echo "      unless /bin/bash has Full Disk Access (System Settings > Privacy & Security),"
    echo "      or the project is moved out (e.g. ~/nifty_180_point) and this installer re-run." ;;
esac
launchctl bootstrap "$DOMAIN" "$DST"
launchctl enable "$DOMAIN/$LABEL"
echo "installed $LABEL -> $(launchctl print "$DOMAIN/$LABEL" | grep -E 'state =' | head -1)"
if [ "${1:-}" = "--run-now" ]; then launchctl kickstart -k "$DOMAIN/$LABEL"; echo "kickstarted; see data/raw/scheduled_fetch.log"; fi
