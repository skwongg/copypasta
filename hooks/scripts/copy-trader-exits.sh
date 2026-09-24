#!/usr/bin/env bash
# Copy-trader exit monitor hook.
# Runs run_exits.py --mode live every poll (60s) during market hours. Each
# sweep reconciles orders, reprices resting exit sells at the current bid,
# cancels stale entry buys and applies the stop-loss / take-profit ladder.
set -euo pipefail
source "$HATCH_HOOK_RUNTIME"

CT="$HOME/workspace/copy-trader"
export COPYTRADER_CONFIG="$CT/policy.json"

# Market hours: Mon-Fri 06:30-13:00 America/Los_Angeles.
# NOTE: use (( )) arithmetic — bash's [ builtin rejects base#number notation.
DOW="$(TZ=America/Los_Angeles date +%u)"
NOW="$(TZ=America/Los_Angeles date +%H%M)"
if [ "$DOW" -gt 5 ] || (( 10#$NOW < 10#0630 )) || (( 10#$NOW >= 10#1300 )); then
  silent "outside market hours"
  exit 0
fi

# Arm/kill state lives in the live state directory, not the checkout;
# run_exits.py checks it locally and exits 2 without a broker call when
# disarmed or halted.
python3 "$CT/run_exits.py" --mode live >>"$CT/exits.log" 2>&1 || {
  log "run_exits failed" "$(tail -c 500 "$CT/exits.log" 2>/dev/null)"
  silent "exit monitor error, staying silent"
}

# Sync any new copy-trader fills into Silas's dashboard ledger ("Me" tab).
# The sync is local-only; the dashboard redeploy needs credentials, so it
# goes through a worker wake.
SYNC_OUT="$(python3 "$HOME/workspace/trade-watch/sync_my_ledger.py" 2>&1)" || {
  log "ledger sync failed" "$SYNC_OUT"
  silent "exit monitor sweep done"
}

if echo "$SYNC_OUT" | grep -qE "appended [1-9][0-9]* ledger"; then
  wake "ledger synced" "{\"detail\": \"$SYNC_OUT\"}"
fi

silent "exit monitor sweep done"
