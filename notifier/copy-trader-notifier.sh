#!/usr/bin/env bash
# copy-trader notifier hook.
# Drains ~/workspace/copy-trader/notifications.jsonl and wakes the worker
# agent with the queued post-trade notifications (fills, blocks, rejections,
# exit rung fills, halt warnings). This hook only RELAYS notifications; it
# needs no arming and places no orders.
set -euo pipefail
source "$HATCH_HOOK_RUNTIME"

QUEUE="$HOME/workspace/copy-trader/notifications.jsonl"

if [ -s "$QUEUE" ]; then
  PAYLOAD="$( jq -R -s '{notifications: [split("\n")[] | select(length > 0) | fromjson]}' <"$QUEUE" )"
  wake "copy-trader notifications" "$PAYLOAD"
  if [ "${HATCH_HOOK_DRY_RUN:-0}" != "1" ]; then
    : > "$QUEUE"
  fi
else
  silent "no copy-trader notifications"
fi
