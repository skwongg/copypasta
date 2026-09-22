"""Confirmed-fill exit ladder. All operations hold the account's process lock."""
from datetime import datetime, timezone, timedelta
from decimal import Decimal, ROUND_DOWN
import time

import config
import kill
from entry_engine import quote_price
from order_state import UNRESOLVED, OrderError, intent_id, reconcile, submit, verify_broker_positions
from trade_state import TradingState, StateError

RUNGS = (('tp50', 1.5), ('tp200', 3.0), ('tp300', 4.0))


class ConditionalOrdersUnsupported(RuntimeError):
    pass


def place_conditional_exits(mcp, position):
    raise ConditionalOrdersUnsupported('conditional order schemas and semantics are unverified; disabled')


def open_positions(state=None):
    return [p for p in (state or TradingState()).snapshot()['positions'] if p['qty_remaining'] > 0]


class ExitMonitor:
    def __init__(self, mcp, mode='dry_run', *, state=None):
        if mode not in {'dry_run', 'live'} or mcp.mode != mode:
            raise ValueError('client mode mismatch')
        self.mcp, self.mode = mcp, mode
        self.state = state or TradingState(mode, getattr(mcp, 'account_number', None))
        if self.state.mode != mode or (mode == 'live' and self.state.account != mcp.account_number):
            raise ValueError('state/client account mismatch')

    def note(self, kind, code, **fields):
        return {'kind': kind, 'code': code, 'mode': self.mode, **fields}

    def check(self, *, now=None):
        now = now or datetime.now(timezone.utc)
        started = time.monotonic()
        if not kill.can_fire(context=self.state):
            return [self.note('blocked', 'not_armed_or_halted')]
        if not config.in_market_hours(now):
            return []
        notifications = []
        try:
            if self.mode == 'live':
                self.mcp.assert_mutation_allowed()
            with self.state.transaction() as tx:
                reconcile(tx, self.mcp, now)
                if tx.data['halt_reason'] or any(o['status'] in UNRESOLVED for o in tx.data['orders'].values()):
                    return [self.note('blocked', 'orders_require_reconciliation')]
                verify_broker_positions(tx.data, self.mcp)
                for position_id in [p['position_id'] for p in tx.data['positions'] if p['qty_remaining']]:
                    if not kill.can_fire(context=self.state):
                        notifications.append(self.note('blocked', 'not_armed_or_halted'))
                        break
                    position = next(p for p in tx.data['positions'] if p['position_id'] == position_id)
                    symbol = position['contract_symbol']
                    # Executable bid, not stale last-trade or an optimistic midpoint.
                    quote = self.mcp.get_option_quote(symbol)
                    quote_now = now + timedelta(seconds=max(0, time.monotonic() - started))
                    px = quote_price(quote, 'bid', quote_now, symbol)
                    ratio = px / position['fill_price']
                    triggered = [('sl', 0)] if ratio <= .40 else [(r, t) for r, t in RUNGS if ratio >= t]
                    for rung, _ in triggered:
                        position = next(p for p in tx.data['positions'] if p['position_id'] == position_id)
                        if position['latches'][rung] or not position['qty_remaining']:
                            continue
                        qty = position['qty_remaining'] if rung in {'sl', 'tp300'} else position['qty_remaining'] // 2
                        if qty == 0:
                            position['latches'][rung] = True
                            tx.save()
                            continue
                        if not kill.can_fire(context=self.state):
                            notifications.append(self.note('blocked', 'not_armed_or_halted'))
                            return notifications
                        key = intent_id(self.state, f'exit:{position_id}:{rung}')
                        limit = float(Decimal(str(px)).quantize(Decimal('.01'), rounding=ROUND_DOWN))
                        if limit <= 0:
                            raise OrderError('invalid_exit_limit')
                        preview = self.mcp.review_option_order(symbol, 'sell', qty, 'limit', limit)
                        if not isinstance(preview, dict) or preview.get('approved') is not True or preview.get('isError'):
                            notifications.append(self.note('rejected', 'preview_not_approved'))
                            return notifications
                        def validate_submission():
                            current = now + timedelta(seconds=max(0, time.monotonic() - started))
                            if not config.in_market_hours(current):
                                raise OrderError('market_closed_before_submission')
                            quote_price(quote, 'bid', current, symbol)
                        order = submit(tx, self.state, self.mcp, {
                            'intent_id': key, 'position_id': position_id, 'contract_symbol': symbol,
                            'underlying': position['underlying'], 'side': 'sell', 'quantity': qty,
                            'order_type': 'limit', 'limit_price': limit, 'rung': rung,
                        }, now, paper_price=px, validate=validate_submission)
                        action = ('fired' if order['status'] == 'filled' else 'pending' if order['status'] in UNRESOLVED
                                  else 'partially_filled' if order['filled_qty'] else 'rejected')
                        notifications.append(self.note(action,
                                                       'order_' + order['status'], contract_symbol=symbol,
                                                       qty=order['filled_qty'], fill_price=order['avg_fill_price']))
                        if order['status'] != 'filled':
                            return notifications
        except StateError:
            notifications.append(self.note('blocked', 'state_unavailable_or_corrupt'))
        except OrderError:
            notifications.append(self.note('blocked', 'order_or_broker_state_requires_review'))
        except Exception:
            notifications.append(self.note('rejected', 'provider_or_configuration_error'))
        return notifications
