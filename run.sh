#!/bin/bash
# launchd entry point for the sales-meeting sync.
#
# WHY THIS LIVES OUTSIDE ~/Desktop (Aug 3 2026):
# The first version of this pointed launchd at ~/Desktop/bluon-sales-meeting-sync.
# It never ran once — every fire died with:
#     /bin/bash: .../run_local.sh: Operation not permitted
# macOS TCC protects ~/Desktop, ~/Documents and ~/Downloads. An interactive
# shell inherits Terminal's grant so running the script by hand works fine,
# which is exactly what made it look healthy. launchd has no such grant, so it
# could not even read the file. Hence this runtime copy under a dot-directory
# in $HOME, which TCC does not gate — the same reason com.bluon.lp-tracking
# works from ~/.bluon-tracking.
#
# ~/Desktop/bluon-sales-meeting-sync stays the editing copy. This one pulls
# from GitHub before each run, so committing and pushing there is what ships.

set -uo pipefail

RUNTIME="$HOME/.bluon-sales-sync"
REPO="$RUNTIME/repo"
LOG="$RUNTIME/sync.log"

echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] --- run ---" >> "$LOG"

# Pull latest, but never let a network or auth hiccup stop the sync — running
# slightly stale code beats not syncing the standup at all.
if ! git -C "$REPO" pull --quiet --ff-only >> "$LOG" 2>&1; then
  echo "  (git pull failed — running with the code already on disk)" >> "$LOG"
fi

NOTION_PERSONAL_TOKEN="$(cat "$HOME/.config/notion/api_key" 2>/dev/null | tr -d '[:space:]')"
NOTION_BLUON_TOKEN="$(cat "$HOME/.config/notion/bluon_api_key" 2>/dev/null | tr -d '[:space:]')"
export NOTION_PERSONAL_TOKEN NOTION_BLUON_TOKEN

if [ -z "$NOTION_PERSONAL_TOKEN" ] || [ -z "$NOTION_BLUON_TOKEN" ]; then
  echo "  ERROR: missing Notion token in ~/.config/notion/" >> "$LOG"
  exit 1
fi

# --all-calls: mirror every internal team call that day, not just the
# 10am standup (Aug 3 2026 - the DB is meant to be the full picture).
/usr/bin/python3 "$REPO/sync.py" --all-calls "$@" >> "$LOG" 2>&1
echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] exit=$?" >> "$LOG"

tail -n 2000 "$LOG" > "$LOG.tmp" 2>/dev/null && mv "$LOG.tmp" "$LOG"
