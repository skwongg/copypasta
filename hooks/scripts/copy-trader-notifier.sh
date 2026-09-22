#!/usr/bin/env bash
# Standalone deterministic text only: no sourced runtime, agent wake, or tool access.
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$SCRIPT_DIR/../notifications.py" "$@"
