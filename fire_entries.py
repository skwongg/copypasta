#!/usr/bin/env python3
"""Fire copy-trader entries for new trade-post alerts.

CLI:
    python fire_entries.py --alerts-json PATH --mode live|dry_run

PATH is a JSON file shaped like {"alerts": [...]} as printed by
~/workspace/trade-watch/watch.py; it may also be /dev/stdin.

For each alert with type == "entry", calls
`process_entry(alert, mcp, mode)` from the sibling entry_engine.py and
appends the resulting `result.notification` dict as one JSON line to
~/workspace/copy-trader/notifications.jsonl (the queue drained by the
copy-trader-notifier hook).

Non-entry alerts (exits, updates) are ignored: exits stay manual per plan.

Exit codes:
    0  all alerts processed (per-alert failures are recorded in the queue)
    1  CLI/usage error (bad args, unreadable JSON, live mode without OAuth)
"""
from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
import traceback

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
NOTIFICATIONS_PATH = os.path.join(BASE_DIR, "notifications.jsonl")

# -- oauth wiring ---------------------------------------------------------
# oauth_client.py is built in parallel; guard the import so a missing
# OAuth setup produces a clear error instead of an ImportError traceback.
try:
    import oauth_client  # noqa: F401
    _oauth_available = True
except Exception:  # pragma: no cover - module may not exist yet
    oauth_client = None
    _oauth_available = False

from mcp_client import MCPClient


def _resolve_entry_engine():
    """Import entry_engine lazily; fail with a clear message if absent."""
    try:
        return importlib.import_module("entry_engine")
    except Exception as exc:
        raise RuntimeError(
            "entry_engine.py is not available in ~/workspace/copy-trader/; "
            "the entry engine module must be built before entries can fire."
        ) from exc


def _read_alerts(path):
    if path == "/dev/stdin" or path == "-":
        raw = sys.stdin.read()
    else:
        with open(path, "r", encoding="utf-8") as fh:
            raw = fh.read()
    data = json.loads(raw)
    alerts = data.get("alerts")
    if not isinstance(alerts, list):
        raise ValueError("alerts JSON must contain an 'alerts' list")
    return alerts


def _queue_notification(notification):
    with open(NOTIFICATIONS_PATH, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(notification, separators=(",", ":")) + "\n")


def fire(alerts, mode):
    """Process entry alerts; returns a summary dict."""
    entry_engine = _resolve_entry_engine()
    summary = {"processed": 0, "fired": 0, "blocked": 0, "rejected": 0,
               "ignored": 0}

    if mode == "live" and not _oauth_available:
        raise RuntimeError(
            "OAuth not completed; cannot fire live. "
            "Run `python oauth_client.py auth-url` and complete the OAuth "
            "flow before using --mode live.")

    token_provider = oauth_client.get_valid_token if _oauth_available else None
    mcp = MCPClient(token_provider=token_provider, mode=mode)

    for alert in alerts:
        if not isinstance(alert, dict):
            summary["ignored"] += 1
            continue
        if alert.get("type") != "entry":
            summary["ignored"] += 1  # exits/updates stay manual
            continue

        summary["processed"] += 1
        try:
            result = entry_engine.process_entry(alert, mcp, mode=mode)
            action = getattr(result, "action", None)
            if action == "fired":
                summary["fired"] += 1
            elif action == "blocked":
                summary["blocked"] += 1
            else:
                summary["rejected"] += 1
            notification = getattr(result, "notification", None)
            if notification is None:
                notification = {
                    "kind": "ambiguous",
                    "text": (f"Copy-trader: entry engine returned no "
                             f"notification for {alert.get('handle')} post "
                             f"{alert.get('id')}."),
                    "reason": "missing_notification",
                }
        except Exception as exc:
            summary["rejected"] += 1
            notification = {
                "kind": "rejected",
                "text": (f"Entry engine error on {alert.get('handle')} post: "
                         f"{type(exc).__name__}: {exc}"),
            }
        _queue_notification(notification)

    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--alerts-json", required=True,
                        help='path to {"alerts": [...]} JSON (or /dev/stdin)')
    parser.add_argument("--mode", choices=("live", "dry_run"),
                        default="dry_run")
    args = parser.parse_args(argv)

    try:
        alerts = _read_alerts(args.alerts_json)
    except Exception as exc:
        print(json.dumps({"error": f"failed to read alerts JSON: {exc}"}))
        return 1

    try:
        summary = fire(alerts, args.mode)
    except RuntimeError as exc:
        print(json.dumps({"error": str(exc)}))
        return 1
    except Exception as exc:  # unexpected; queue still keeps per-alert items
        print(json.dumps({
            "error": f"unexpected failure: {type(exc).__name__}: {exc}",
            "traceback_tail": traceback.format_exc(limit=3),
        }))
        return 1

    print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
