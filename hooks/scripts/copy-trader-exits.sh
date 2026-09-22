#!/usr/bin/env bash
# Copy-trader exit monitor hook.
# Runs run_exits.py --mode live about every 2 minutes during market hours,
# but ONLY when there is something to manage (ARMED entries, or open
# positions on file) and the kill switch is not engaged. Exit fills and rung
# hits are queued to notifications.jsonl and relayed to the Trade Alerts
# side chat by the copy-trader-notifier hook (60s poll).
set -euo pipefail
source "$HATCH_HOOK_RUNTIME"

CT="$HOME/workspace/copy-trader"
export COPYTRADER_CONFIG="$CT/policy.json"

# Kill switch engaged? Never touch orders.
if [ -f "$CT/KILL" ]; then
  silent "copy-trader killed, exit monitor paused"
  exit 0
fi

# Market hours: Mon-Fri 06:30-13:00 America/Los_Angeles.
DOW="$(TZ=America/Los_Angeles date +%u)"
NOW="$(TZ=America/Los_Angeles date +%H%M)"
if [ "$DOW" -gt 5 ] || [ "10#$NOW" -lt "10#0630" ] || [ "10#$NOW" -ge "10#1300" ]; then
  silent "outside market hours"
  exit 0
fi

# Anything to manage? Armed entries, or open positions still on file.
HAS_POS=0
if [ -s "$CT/positions.json" ]; then
  N="$(jq 'if type=="array" then length else (to_entries|length) end' "$CT/positions.json" 2>/dev/null || echo 0)"
  case "${N:-0}" in ''|*[!0-9]*) N=0;; esac
  [ "$N" -gt 0 ] && HAS_POS=1
fi
if [ ! -f "$CT/ARMED" ] && [ "$HAS_POS" -eq 0 ]; then
  silent "copy-trader idle: disarmed and no open positions"
  exit 0
fi

python3 "$CT/run_exits.py" --mode live >>"$CT/exits.log" 2>&1 || {
  log "run_exits failed" "$(tail -c 500 "$CT/exits.log" 2>/dev/null)"
  silent "exit monitor error, staying silent"
}

silent "exit monitor sweep done"
