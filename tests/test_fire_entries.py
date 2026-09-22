#!/usr/bin/env python3
"""Self-test for fire_entries.py (dry_run only; no real orders, no live client).

Stubs entry_engine and MCPClient via sys.modules so the real entry engine and
network client are never touched.
"""
import json
import os
import sys
import types

CT_DIR = os.path.expanduser("~/workspace/copy-trader")
sys.path.insert(0, CT_DIR)
os.chdir(CT_DIR)

import fire_entries  # noqa: E402

# --- fake MCPClient (records construction, never touches the network) ------
constructed = []


class FakeMCPClient:
    def __init__(self, token_provider=None, mode="dry_run"):
        assert mode == "dry_run", f"test must not use mode={mode}"
        constructed.append({"mode": mode, "has_provider": token_provider is not None})


fire_entries.MCPClient = FakeMCPClient

# --- stub entry_engine -------------------------------------------------------
calls = []


class FakeResult:
    def __init__(self, action, notification):
        self.action = action
        self.notification = notification


def fake_process_entry(alert, mcp, mode="dry_run"):
    calls.append((alert["id"], mode))
    if alert["id"] == "boom":
        raise ValueError("resolver could not match contract")
    return FakeResult(
        action="fired",
        notification={
            "kind": "fill",
            "text": f"Filled test contract for @{alert['handle']}",
            "contract_symbol": "SPY260922C00671000",
            "qty": 1,
            "fill_price": 0.56,
            "order_id": "fake-123",
        },
    )


stub = types.ModuleType("entry_engine")
stub.process_entry = fake_process_entry
sys.modules["entry_engine"] = stub

# --- fake alerts: 2 entries + 1 exit ------------------------------------------
ALERTS = {
    "status": "alert",
    "alerts": [
        {"id": "a1", "handle": "clintoptions", "text": "$QQQ 734 CALLS .56",
         "posted_at": "2026-09-21T09:00:00Z", "url": "https://x.com/x/1",
         "type": "entry"},
        {"id": "a2", "handle": "CassyTrades", "text": "$SPY 771C 1.20",
         "posted_at": "2026-09-21T09:05:00Z", "url": "https://x.com/x/2",
         "type": "entry"},
        {"id": "a3", "handle": "clintoptions", "text": "out half +100%",
         "posted_at": "2026-09-21T09:10:00Z", "url": "https://x.com/x/3",
         "type": "exit"},
    ],
}

ALERTS_PATH = "/tmp/fire_entries_selftest_alerts.json"
with open(ALERTS_PATH, "w") as fh:
    json.dump(ALERTS, fh)

QUEUE = os.path.join(CT_DIR, "notifications.jsonl")
if os.path.exists(QUEUE):
    os.remove(QUEUE)

# --- run via /dev/stdin path too --------------------------------------------
with open(ALERTS_PATH) as fh:
    saved_stdin = sys.stdin
    class _S:
        def read(self):
            return fh.read()
    sys.stdin = _S()
    rc = fire_entries.main(["--alerts-json", "/dev/stdin", "--mode", "dry_run"])
    sys.stdin = saved_stdin
assert rc == 0, f"main returned {rc}"

# --- assertions ----------------------------------------------------------------
with open(QUEUE) as fh:
    lines = [ln for ln in fh.read().split("\n") if ln.strip()]
assert len(lines) == 2, f"expected exactly 2 queued notifications, got {len(lines)}: {lines}"
notifs = [json.loads(ln) for ln in lines]
assert all(n["kind"] == "fill" for n in notifs), notifs
assert all("contract_symbol" in n for n in notifs), notifs

entry_ids = [c[0] for c in calls]
assert entry_ids == ["a1", "a2"], f"exits must be ignored, calls were {entry_ids}"
assert all(mode == "dry_run" for _, mode in calls)
assert len(constructed) == 1 and constructed[0]["mode"] == "dry_run"

# --- exception path: one entry raises ----------------------------------------
os.remove(QUEUE)
calls.clear()
sys.modules["entry_engine"].process_entry = lambda a, m, mode="dry_run": fake_process_entry(a, m, mode=mode) if a["id"] != "a1" else (_ for _ in ()).throw(RuntimeError("kaboom"))

def boom_entry(alert, mcp, mode="dry_run"):
    if alert["id"] == "boom":
        raise RuntimeError("kaboom")
    return fake_process_entry(alert, mcp, mode=mode)

sys.modules["entry_engine"].process_entry = boom_entry
ALERTS["alerts"][0]["id"] = "boom"
with open(ALERTS_PATH, "w") as fh:
    json.dump(ALERTS, fh)
rc = fire_entries.main(["--alerts-json", ALERTS_PATH, "--mode", "dry_run"])
assert rc == 0
with open(QUEUE) as fh:
    lines = [ln for ln in fh.read().split("\n") if ln.strip()]
assert len(lines) == 2, lines
kinds = [json.loads(ln)["kind"] for ln in lines]
assert kinds == ["rejected", "fill"], kinds
assert "kaboom" in json.loads(lines[0])["text"]

# --- live mode without OAuth must refuse -------------------------------------
fire_entries._oauth_available = False
rc = fire_entries.main(["--alerts-json", ALERTS_PATH, "--mode", "live"])
assert rc != 0, "live mode without OAuth must exit non-zero"
fire_entries._oauth_available = True

# --- cleanup -------------------------------------------------------------------
os.remove(QUEUE)
os.remove(ALERTS_PATH)
print("SELF-TEST PASSED: 2 entries queued, 1 exit ignored, exceptions caught, live-without-oauth refused")
