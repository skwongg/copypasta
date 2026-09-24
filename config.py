"""Reviewed defaults. No filesystem or network side effects at import."""
from dataclasses import dataclass, field
from datetime import datetime
import json
import math
import os
import re
from pathlib import Path
from zoneinfo import ZoneInfo

MARKET_TZ = ZoneInfo('America/Los_Angeles')
TRADE_NOTIONAL_TARGET = 500.0
MAX_OPEN_EXPOSURE = 5000.0
DAILY_LOSS_CAP = 2500.0
MAX_ENTRY_SLIPPAGE = 0.10
MAX_QUOTE_AGE_SECONDS = 30
# Broker quote timestamps may run slightly ahead of our clock; tolerate a few
# seconds of future-dating so fresh quotes are not rejected as invalid.
# 2026-09-23: ~1s broker clock skew blocked every live quote check.
MAX_QUOTE_SKEW_SECONDS = 5
# Resting limit orders. An exit sell still open after this many seconds is
# canceled and re-placed at the current bid on the next sweep (~60s cadence);
# an entry buy still open after ENTRY_ORDER_TIMEOUT_SECONDS is canceled.
EXIT_REPRICE_SECONDS = 45
ENTRY_ORDER_TIMEOUT_SECONDS = 120
# How long a live CLI waits for the other one (entry vs exit sweep) to
# release the account's state lock before giving up.
STATE_LOCK_WAIT_SECONDS = 30
TRADERS = {'cassytrades': '0DTE', 'clintoptions': None, 'capricekayem': None, 'spylieu': None}
MARKET_HOLIDAYS = {
    '2026-01-01', '2026-01-19', '2026-02-16', '2026-04-03', '2026-05-25', '2026-06-19',
    '2026-07-03', '2026-09-07', '2026-11-26', '2026-12-25', '2027-01-01', '2027-01-18',
    '2027-02-15', '2027-03-26', '2027-05-31', '2027-06-18', '2027-07-05', '2027-09-06',
    '2027-11-25', '2027-12-24',
}
EARLY_CLOSES = {'2026-11-27', '2026-12-24', '2027-11-26'}


def default_expiry_for(handle):
    return TRADERS.get((handle or '').lstrip('@').lower())


def in_market_hours(now=None):
    now = now or datetime.now(MARKET_TZ)
    if now.tzinfo is None:
        return False
    now = now.astimezone(MARKET_TZ)
    if now.year not in {2026, 2027}:
        return False
    day = now.date().isoformat()
    if now.weekday() >= 5 or day in MARKET_HOLIDAYS:
        return False
    close = 10 * 60 if day in EARLY_CLOSES else 13 * 60
    return 6 * 60 + 30 <= now.hour * 60 + now.minute < close


@dataclass(frozen=True)
class Policy:
    sources: dict = field(default_factory=lambda: dict.fromkeys(TRADERS))
    source_key: bytes | None = None
    max_age_seconds: int = 300
    trade_target: float = TRADE_NOTIONAL_TARGET
    max_exposure: float = MAX_OPEN_EXPOSURE
    daily_loss_cap: float = DAILY_LOSS_CAP
    max_slippage: float = MAX_ENTRY_SLIPPAGE

    def __post_init__(self):
        if not isinstance(self.sources, dict) or not self.sources or any(
            key not in TRADERS or (value is not None and (not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', value)))
            for key, value in self.sources.items()
        ):
            raise ValueError('invalid source allowlist')
        ids = [v for v in self.sources.values() if v is not None]
        if len(ids) != len(set(ids)):
            raise ValueError('source IDs must be unique')
        if type(self.max_age_seconds) is not int or not 1 <= self.max_age_seconds <= 300:
            raise ValueError('invalid alert age bound')
        for value in (self.trade_target, self.max_exposure, self.daily_loss_cap):
            if type(value) not in (float, int) or not math.isfinite(value) or value <= 0:
                raise ValueError('invalid risk bound')
        if type(self.max_slippage) not in (float, int) or not math.isfinite(self.max_slippage) or not 0 <= self.max_slippage <= MAX_ENTRY_SLIPPAGE:
            raise ValueError('slippage must not exceed reviewed limit')


def _load_source_key():
    """Authenticated producer key. Lives outside the repo, 0600. Missing in
    live mode fails closed at admission ('verified_source_channel_required')."""
    path = os.environ.get("COPYTRADER_SOURCE_KEY_FILE",
                          os.path.expanduser("~/.local/share/copypasta/source_key"))
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read().strip()
        key = bytes.fromhex(raw)
    except (OSError, ValueError):
        return None
    return key if len(key) >= 32 else None


def load_policy():
    path = os.environ.get('COPYTRADER_CONFIG')
    source_key = _load_source_key()
    if not path:
        return Policy(source_key=source_key)
    data = json.loads(Path(path).read_text())
    if not isinstance(data, dict) or set(data) - {'sources', 'max_age_seconds', 'trade_target', 'max_exposure', 'daily_loss_cap', 'max_slippage'}:
        raise ValueError('unsupported policy keys')
    return Policy(source_key=source_key, **data)
