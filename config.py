"""Shared configuration for the Robinhood copy-trader.

Pure local logic: no network calls, no credentials, no orders.

Loads global settings + per-trader assumptions from
~/workspace/trade-watch/trader_config.json and exposes market-calendar
helpers mirrored from ~/workspace/trade-watch/watch.py.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

_CONFIG_PATH = Path.home() / "workspace" / "trade-watch" / "trader_config.json"

with _CONFIG_PATH.open() as _f:
    _RAW = json.load(_f)

# ---------------------------------------------------------------------------
# Global settings (from trader_config.json)
# ---------------------------------------------------------------------------

MARKET_TZ = ZoneInfo(_RAW["_market_timezone"])
MAX_ENTRY_SLIPPAGE = float(_RAW["_max_entry_slippage"])  # 0.10

# ---------------------------------------------------------------------------
# Hard-coded policy constants (2026-09-21 decision with Silas)
# ---------------------------------------------------------------------------

# Target dollar size for each single copied trade entry.
TRADE_NOTIONAL_TARGET = 500.0  # per 2026-09-21 policy decision
# Maximum total dollar exposure across all open positions.
MAX_OPEN_EXPOSURE = 5000.0  # per 2026-09-21 policy decision
# Stop copying new trades once realized daily P&L hits this loss (dollars).
DAILY_LOSS_CAP = 2500.0  # per 2026-09-21 policy decision

# ---------------------------------------------------------------------------
# Market calendar (NYSE holidays, copied from watch.py's MARKET_HOLIDAYS)
# ---------------------------------------------------------------------------

MARKET_HOLIDAYS = {
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25",
    "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
    "2027-01-01", "2027-01-18", "2027-02-15", "2027-04-02", "2027-05-31",
    "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24",
}


def in_market_hours(now: datetime | None = None) -> bool:
    """True iff now is Mon-Fri, 6:30 AM-1:00 PM PT, and not a market holiday."""
    now = now or datetime.now(MARKET_TZ)
    if now.tzinfo is None:
        now = now.replace(tzinfo=MARKET_TZ)
    else:
        now = now.astimezone(MARKET_TZ)
    if now.strftime("%Y-%m-%d") in MARKET_HOLIDAYS:
        return False
    if now.weekday() >= 5:  # Sat/Sun
        return False
    mins = now.hour * 60 + now.minute
    return 6 * 60 + 30 <= mins < 13 * 60


def trading_day(ts_iso: str) -> date | None:
    """Validate an ISO-8601 timestamp against the market calendar.

    Parses the timestamp (with offset), converts to MARKET_TZ, and returns
    the market date if it is a weekday and not a holiday, else None.
    Used for same-day validation when inferring 0DTE expiries.
    """
    ts = datetime.fromisoformat(ts_iso)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=MARKET_TZ)
    local = ts.astimezone(MARKET_TZ)
    d = local.date()
    if d.weekday() >= 5:
        return None
    if d.strftime("%Y-%m-%d") in MARKET_HOLIDAYS:
        return None
    return d


def default_expiry_for(handle: str) -> str | None:
    """Default option expiry inference for a trader handle.

    Returns e.g. "0DTE" for CassyTrades (verified 2026-09-21: she never
    discloses expiry, so an undisclosed expiry means 0DTE), or None when no
    inference should be made (clintoptions, capricekayem, unknown handles).
    """
    key = handle.lstrip("@").lower()
    for name, entry in _RAW.items():
        if name.startswith("_") or not isinstance(entry, dict):
            continue
        if name.lower() == key:
            return entry.get("default_expiry")
    return None


# ---------------------------------------------------------------------------
# Summary dict for other modules to import
# ---------------------------------------------------------------------------

POLICY = {
    "market_timezone": str(MARKET_TZ),
    "max_entry_slippage": MAX_ENTRY_SLIPPAGE,
    "trade_notional_target": TRADE_NOTIONAL_TARGET,
    "max_open_exposure": MAX_OPEN_EXPOSURE,
    "daily_loss_cap": DAILY_LOSS_CAP,
    "market_holidays": sorted(MARKET_HOLIDAYS),
}


def _self_test() -> None:
    pt = MARKET_TZ

    # in_market_hours boundaries
    assert in_market_hours(datetime(2026, 9, 21, 6, 29, tzinfo=pt)) is False  # before open
    assert in_market_hours(datetime(2026, 9, 21, 6, 30, tzinfo=pt)) is True   # open edge
    assert in_market_hours(datetime(2026, 9, 21, 12, 59, tzinfo=pt)) is True  # close edge
    assert in_market_hours(datetime(2026, 9, 21, 13, 0, tzinfo=pt)) is False  # after close
    assert in_market_hours(datetime(2026, 9, 19, 10, 0, tzinfo=pt)) is False   # Saturday
    assert in_market_hours(datetime(2026, 9, 7, 10, 0, tzinfo=pt)) is False   # Labor Day holiday

    # trading_day: rejects Saturday and holiday, accepts a normal weekday
    assert trading_day("2026-09-19T10:00:00-07:00") is None  # Saturday
    assert trading_day("2026-09-07T10:00:00-07:00") is None  # Labor Day
    assert trading_day("2026-09-21T10:00:00-07:00") == date(2026, 9, 21)

    # default_expiry_for
    assert default_expiry_for("CassyTrades") == "0DTE"
    assert default_expiry_for("@CassyTrades") == "0DTE"
    assert default_expiry_for("clintoptions") is None
    assert default_expiry_for("capricekayem") is None
    assert default_expiry_for("nobody") is None

    # constants loaded from JSON
    assert MAX_ENTRY_SLIPPAGE == 0.10
    assert str(MARKET_TZ) == "America/Los_Angeles"

    print("config.py self-test: OK")


if __name__ == "__main__":
    _self_test()
