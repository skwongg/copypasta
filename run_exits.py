"""Thin CLI runner for the exit monitor.

Usage:
    python3 run_exits.py --mode dry_run   # default: safe, no real orders
    python3 run_exits.py --mode live      # operator-explicit only

Builds an MCPClient, runs ExitMonitor.check(), appends the returned
notifications (one JSON object per line) to
~/workspace/copy-trader/notifications.jsonl, and prints a summary JSON.

The OAuth token provider comes from the sibling oauth_client module
(get_valid_token). If oauth_client is not built yet the import is guarded
and the runner exits with a clear error asking to complete OAuth setup.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

BASE = Path.home() / "workspace" / "copy-trader"
NOTIFICATIONS_PATH = BASE / "notifications.jsonl"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the copy-trader exit monitor")
    parser.add_argument("--mode", choices=("dry_run", "live"), default="dry_run",
                        help="dry_run (default, safe) or live (explicit only)")
    args = parser.parse_args(argv)

    # -- OAuth token provider (guarded: module may not exist yet) -----------
    try:
        from oauth_client import get_valid_token
    except ImportError:
        print("ERROR: oauth_client is not built yet — complete the OAuth setup "
              "first (build oauth_client with get_valid_token), then retry.",
              file=sys.stderr)
        return 2

    from mcp_client import MCPClient
    from exits import ExitMonitor, open_positions

    mcp = MCPClient(token_provider=get_valid_token, mode=args.mode)
    monitor = ExitMonitor(mcp, mode=args.mode)
    notifications = monitor.check()

    # -- append notifications, one JSON per line ----------------------------
    if notifications:
        NOTIFICATIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with NOTIFICATIONS_PATH.open("a") as f:
            for n in notifications:
                f.write(json.dumps(
                    {"ts": datetime.now(timezone.utc).isoformat(), **n}) + "\n")

    # -- summary -------------------------------------------------------------
    by_kind: dict[str, int] = {}
    for n in notifications:
        kind = n.get("kind", "unknown")
        by_kind[kind] = by_kind.get(kind, 0) + 1
    summary = {
        "mode": args.mode,
        "ts": datetime.now(timezone.utc).isoformat(),
        "notifications": len(notifications),
        "by_kind": by_kind,
        "open_positions": len(open_positions()),
    }
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
