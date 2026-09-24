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
    # after it never runs. fire_entries.py itself refuses (locally, before any
    # broker call) unless the live state is armed and not halted; arm/halt with
    # `python3 ~/workspace/copy-trader/kill.py --mode live --arm|--halt|--disarm|--status`.
    CT="$HOME/workspace/copy-trader"
    export COPYTRADER_CONFIG="$CT/policy.json"
    set +e
    FIRE_OUT="$(printf '%s' "$RESULT" | python3 "$CT/sign_alerts.py" 2>>"$CT/fire.log" \
      | python3 "$CT/fire_entries.py" --alerts-json /dev/stdin --mode live 2>>"$CT/fire.log")"
    FIRE_RC=$?
    set -e
    printf '%s\n' "$FIRE_OUT" >>"$CT/fire.log"
    # fire_entries prints only fixed result codes and validated contract symbols.
    CT_LINES="$(printf '%s\n' "$FIRE_OUT" | grep '^\[LIVE\]' || true)"
    if [ "$FIRE_RC" -ne 0 ]; then
      CT_LINES="did not trade (exit $FIRE_RC: disarmed, halted or broker error; see fire.log)"
    fi
    PAYLOAD="$(printf '%s' "$RESULT" | jq -c --arg ct "$CT_LINES" '. + {copytrader: $ct}' 2>/dev/null || printf '%s' "$RESULT")"
    wake "new trade posts from @CassyTrades/@clintoptions/@capricekayem/@spylieu" "$PAYLOAD"
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
