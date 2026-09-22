#!/usr/bin/env bash
# Poll @CassyTrades and @clintoptions for new options trade posts.
# Delegates fetching/classification to the Python watcher; wakes the worker
# agent only when fresh trade entries/exits are found.
set -euo pipefail
source "$HATCH_HOOK_RUNTIME"

WATCH="$HOME/workspace/trade-watch/venv/bin/python $HOME/workspace/trade-watch/watch.py"

# Never mutate seen-state during dry runs.
if [ "${HATCH_HOOK_DRY_RUN:-0}" = "1" ]; then
  WATCH="$WATCH --no-save"
fi

RESULT="$( $WATCH 2>/tmp/trade-watch-err.log )" || {
  log "watch.py failed" "$(head -c 500 /tmp/trade-watch-err.log)"
  silent "watcher error, staying silent"
}

STATUS="$( printf '%s' "$RESULT" | jq -r '.status // "error"' )"

case "$STATUS" in
  alert)
    # Copy-trader: sign the alert JSON (authenticated producer path) and pipe
    # it into the entry engine BEFORE wake — wake does not return, so anything
    # after it never runs. Only fires while ~/workspace/copy-trader/ARMED
    # exists (disarmed = text-only alerts). Deployment review complete 2026-09-22;
    # fire_entries.py --mode live is enabled with all gates active.
    if [ -f "$HOME/workspace/copy-trader/ARMED" ]; then
      export COPYTRADER_CONFIG="$HOME/workspace/copy-trader/policy.json"
      printf '%s' "$RESULT" | python3 "$HOME/workspace/copy-trader/sign_alerts.py" \
        | "$HOME/workspace/copy-trader/fire_entries.py" --alerts-json /dev/stdin --mode live >>"$HOME/workspace/copy-trader/fire.log" 2>&1 || true
    fi
    wake "new trade posts from @CassyTrades/@clintoptions/@capricekayem" "$RESULT"
    ;;
  ok)
    silent "no new trade posts"
    ;;
  off_hours)
    silent "outside market hours"
    ;;
  *)
    DETAIL="$( printf '%s' "$RESULT" | jq -r '.detail // "unknown"' )"
    log "watcher inconclusive" "$DETAIL"
    silent "watcher inconclusive: $DETAIL"
    ;;
esac
