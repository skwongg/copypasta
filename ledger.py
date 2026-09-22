"""Append-only JSONL ledger for the Robinhood copy-trader.

Pure local logic: no network calls, no credentials, no orders.

Event types used by other modules (each record also carries "ts", a UTC
ISO-8601 timestamp added by append()):

  entry_attempt  - a copied entry order was attempted
                   (fields: trader, symbol, side, qty, limit_price, ...)
  entry_fill     - an entry order filled
                   (fields: trader, symbol, side, qty, fill_price, fees, ...)
  entry_blocked  - entry blocked by a policy check before ordering
                   (fields: reason, ...)
  entry_rejected - entry order rejected (e.g. kill switch, slippage)
                   (fields: reason, ...)
  exit_fill      - an exit order filled; carries realized_pnl (dollars,
                   before fees) and fees (dollars)
  kill_halt      - the kill switch was engaged while positions were open
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from config import MARKET_TZ

LEDGER_PATH = Path.home() / "workspace" / "copy-trader" / "ledger.jsonl"


def append(event: dict) -> dict:
    """Append an event to the ledger.

    Writes {"ts": <UTC iso>, **event} as one JSONL line, creating parent
    dirs as needed. Returns the full record that was written.
    """
    record = {"ts": datetime.now(timezone.utc).isoformat(), **event}
    LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LEDGER_PATH.open("a") as f:
        f.write(json.dumps(record) + "\n")
    return record


def read_all() -> list[dict]:
    """Return all ledger records as a list of dicts (empty if no ledger)."""
    if not LEDGER_PATH.exists():
        return []
    records = []
    with LEDGER_PATH.open() as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def _market_date(record: dict) -> str | None:
    ts = record.get("ts")
    if not ts:
        return None
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(MARKET_TZ).strftime("%Y-%m-%d")


def realized_pnl_on(date_str: str) -> float:
    """Net realized P&L (dollars) for one market date ("YYYY-MM-DD").

    Sums the `realized_pnl` fields of records with event=="exit_fill",
    subtracting each record's `fees` (defaults to 0 when absent).
    """
    total = 0.0
    for record in read_all():
        if record.get("event") != "exit_fill":
            continue
        if _market_date(record) != date_str:
            continue
        total += float(record.get("realized_pnl", 0.0)) - float(record.get("fees", 0.0))
    return total


def fills_today(date_str: str) -> list[dict]:
    """List of entry fill records (event=="entry_fill") for one market date."""
    return [
        record
        for record in read_all()
        if record.get("event") == "entry_fill" and _market_date(record) == date_str
    ]


def _self_test() -> None:
    global LEDGER_PATH
    old_path = LEDGER_PATH
    LEDGER_PATH = Path("/tmp/copy-trader-test-ledger.jsonl")
    try:
        if LEDGER_PATH.exists():
            LEDGER_PATH.unlink()

        # round-trip
        r1 = append({"event": "entry_fill", "trader": "CassyTrades", "symbol": "SPY",
                     "qty": 5, "fill_price": 2.10})
        r2 = append({"event": "exit_fill", "trader": "CassyTrades", "symbol": "SPY",
                     "qty": 5, "realized_pnl": 100.0, "fees": 2.0})
        r3 = append({"event": "exit_fill", "trader": "clintoptions", "symbol": "QQQ",
                     "qty": 3, "realized_pnl": -30.0, "fees": 1.0})
        r4 = append({"event": "entry_blocked", "reason": "daily loss cap"})
        assert "ts" in r1 and r1["event"] == "entry_fill"

        all_records = read_all()
        assert len(all_records) == 4
        assert all_records[1]["realized_pnl"] == 100.0

        # realized_pnl_on math uses the market date of the appended records
        today = _market_date(r2)
        pnl = realized_pnl_on(today)
        assert abs(pnl - 67.0) < 1e-9, pnl  # (100-2) + (-30-1) = 67
        assert realized_pnl_on("1999-01-01") == 0.0  # empty date
        assert read_all() == all_records  # second read identical

        # fills_today returns only entry fills for that date
        fills = fills_today(today)
        assert len(fills) == 1 and fills[0]["symbol"] == "SPY"
        assert fills_today("1999-01-01") == []
    finally:
        if LEDGER_PATH.exists():
            LEDGER_PATH.unlink()
        LEDGER_PATH = old_path

    print("ledger.py self-test: OK")


if __name__ == "__main__":
    _self_test()
