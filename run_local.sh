#!/bin/bash
# Local runner for the sales-meeting sync — invoked by the launchd agent
# com.bluon.sales-meeting-sync (~/Library/LaunchAgents).
#
# This is the PRIMARY trigger. The GitHub Action is the backstop for days this
# machine is asleep. GitHub's scheduler was delivering only ~4 of the requested
# ~32 daily fires and delaying the first by 60–120 min, so the entry landed
# around lunch instead of shortly after the 10am ET call. launchd fires on time.
#
# Tokens are read from ~/.config at runtime — never stored in the plist or here.
# Safe to run repeatedly: sync.py is idempotent and status-gated.

set -uo pipefail

REPO="$HOME/Desktop/bluon-sales-meeting-sync"
LOG="$HOME/.bluon-sales-sync/sync.log"
mkdir -p "$(dirname "$LOG")"

NOTION_PERSONAL_TOKEN="$(cat "$HOME/.config/notion/api_key" 2>/dev/null | tr -d '[:space:]')"
NOTION_BLUON_TOKEN="$(cat "$HOME/.config/notion/bluon_api_key" 2>/dev/null | tr -d '[:space:]')"
export NOTION_PERSONAL_TOKEN NOTION_BLUON_TOKEN

if [ -z "$NOTION_PERSONAL_TOKEN" ] || [ -z "$NOTION_BLUON_TOKEN" ]; then
  echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] ERROR: missing Notion token in ~/.config/notion/" >> "$LOG"
  exit 1
fi

echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] --- local run ---" >> "$LOG"
/usr/bin/python3 "$REPO/sync.py" "$@" >> "$LOG" 2>&1
echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] exit=$?" >> "$LOG"

# Keep the log from growing without bound.
tail -n 2000 "$LOG" > "$LOG.tmp" 2>/dev/null && mv "$LOG.tmp" "$LOG"
