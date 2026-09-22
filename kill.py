"""Kill switch for the Robinhood copy-trader.

Pure local logic: no network calls, no credentials, no orders.

State is two marker files:
  ARMED (~/workspace/copy-trader/ARMED) - the copy-trader is armed
  KILL  (~/workspace/copy-trader/KILL)  - the kill switch is engaged

Every order path (entry AND exit) must call can_fire() before placing any
order. By explicit decision, engaging KILL halts ALL order placement,
including automated exits: the copy-trader stops touching the account
entirely, and exiting any still-open positions becomes the user's manual
job in the Robinhood app.

CLI:
  python kill.py --halt    engage the kill switch ("COPY TRADER HALTED")
  python kill.py --reset   lift the kill switch
  python kill.py --arm     create the ARMED marker
  python kill.py --disarm  remove the ARMED marker
  python kill.py --status  print armed/halted state
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BASE = Path.home() / "workspace" / "copy-trader"
ARMED_PATH = BASE / "ARMED"
KILL_PATH = BASE / "KILL"
POSITIONS_PATH = BASE / "positions.json"


def is_armed() -> bool:
    """True iff ARMED exists AND KILL does NOT exist."""
    return ARMED_PATH.exists() and not KILL_PATH.exists()


def can_fire() -> bool:
    """Single choke point every order path must call before placing an order."""
    return is_armed()


def _open_positions() -> int:
    """Best-effort count of open positions from positions.json (0 if absent)."""
    if not POSITIONS_PATH.exists():
        return 0
    try:
        data = json.loads(POSITIONS_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return 0
    positions = data.get("positions", data) if isinstance(data, dict) else data
    if not isinstance(positions, list):
        return 0
    n = 0
    for p in positions:
        if not isinstance(p, dict):
            continue
        if p.get("status", "open") == "open" and float(p.get("quantity", p.get("qty", 0))) != 0:
            n += 1
    return n


def cmd_halt() -> None:
    KILL_PATH.parent.mkdir(parents=True, exist_ok=True)
    KILL_PATH.touch()
    print("COPY TRADER HALTED")
    n = _open_positions()
    if n:
        print(f"WARNING: {n} open position(s) now unprotected "
              "- exit manually in the Robinhood app.")


def cmd_reset() -> None:
    KILL_PATH.unlink(missing_ok=True)
    print("Kill switch reset.")


def cmd_arm() -> None:
    ARMED_PATH.parent.mkdir(parents=True, exist_ok=True)
    ARMED_PATH.touch()
    print("Copy trader ARMED.")


def cmd_disarm() -> None:
    ARMED_PATH.unlink(missing_ok=True)
    print("Copy trader disarmed.")


def cmd_status() -> None:
    if KILL_PATH.exists():
        print("HALTED (KILL file present)")
    elif ARMED_PATH.exists():
        print("Armed (ARMED file present, no KILL file)")
    else:
        print("Disarmed (no ARMED file)")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Copy-trader kill switch")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--halt", action="store_true")
    group.add_argument("--reset", action="store_true")
    group.add_argument("--arm", action="store_true")
    group.add_argument("--disarm", action="store_true")
    group.add_argument("--status", action="store_true")
    args = parser.parse_args(argv)
    if args.halt:
        cmd_halt()
    elif args.reset:
        cmd_reset()
    elif args.arm:
        cmd_arm()
    elif args.disarm:
        cmd_disarm()
    elif args.status:
        cmd_status()


def _self_test() -> None:
    import tempfile

    global ARMED_PATH, KILL_PATH, POSITIONS_PATH
    old_armed, old_kill, old_pos = ARMED_PATH, KILL_PATH, POSITIONS_PATH
    tmp = Path(tempfile.mkdtemp(prefix="copy-trader-kill-test-"))
    ARMED_PATH, KILL_PATH, POSITIONS_PATH = tmp / "ARMED", tmp / "KILL", tmp / "positions.json"
    try:
        # disarmed by default
        assert is_armed() is False
        assert can_fire() is False

        # arm -> firing allowed
        cmd_arm()
        assert ARMED_PATH.exists()
        assert is_armed() is True
        assert can_fire() is True

        # halt -> kill blocks can_fire
        cmd_halt()
        assert KILL_PATH.exists()
        assert is_armed() is False
        assert can_fire() is False

        # reset -> armed again
        cmd_reset()
        assert not KILL_PATH.exists()
        assert can_fire() is True

        # disarm -> cannot fire
        cmd_disarm()
        assert not ARMED_PATH.exists()
        assert can_fire() is False

        # halt warns about open positions
        cmd_arm()
        POSITIONS_PATH.write_text(json.dumps({"positions": [
            {"symbol": "SPY", "quantity": 5, "status": "open"},
            {"symbol": "QQQ", "quantity": 0, "status": "open"},
            {"symbol": "AAPL", "quantity": 2, "status": "closed"},
        ]}))
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            cmd_halt()
        out = buf.getvalue()
        assert "COPY TRADER HALTED" in out
        assert "WARNING: 1 open position(s) now unprotected" in out, out
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
        ARMED_PATH, KILL_PATH, POSITIONS_PATH = old_armed, old_kill, old_pos

    print("kill.py self-test: OK")


if __name__ == "__main__":
    if len(sys.argv) == 1:
        _self_test()
    else:
        main()
