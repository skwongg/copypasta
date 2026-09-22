"""Read-only views of events committed with scoped trading state."""
from datetime import datetime
import config
from trade_state import TradingState


def read_all(*, state=None):
    return (state or TradingState()).snapshot()['events']


def realized_pnl_on(date_str, *, state=None):
    return sum(e.get('realized_pnl', 0) for e in read_all(state=state)
               if e['event'] == 'exit_fill'
               and datetime.fromisoformat(e['ts']).astimezone(config.MARKET_TZ).date().isoformat() == date_str)


def fills_today(date_str, *, state=None):
    return [e for e in read_all(state=state) if e['event'] == 'entry_fill'
            and datetime.fromisoformat(e['ts']).astimezone(config.MARKET_TZ).date().isoformat() == date_str]
