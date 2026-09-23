"""Validated entry decisions using isolated, serialized, durable state."""
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from decimal import Decimal, ROUND_DOWN
import math
import time

from admission import admit, AdmissionError
import config
import kill
import resolver
from order_state import UNRESOLVED, OrderError, event, finite_positive, intent_id, reconcile, submit, verify_broker_positions
from trade_state import TradingState, StateError


@dataclass
class EntryResult:
    action: str
    reason: str
    contract: object = None
    qty: int = 0
    limit: float | None = None
    order_id: str | None = None
    fill_price: float | None = None
    notification: dict = field(default_factory=dict)


def result(action, reason, mode, **fields):
    # Only bounded codes and validated trading fields enter notifications.
    notification = {'kind': action, 'code': reason, 'mode': mode}
    for key in ('qty', 'limit', 'fill_price'):
        if fields.get(key) is not None:
            notification[key] = fields[key]
    contract = fields.get('contract')
    if contract is not None:
        notification['contract_symbol'] = contract.contract_symbol
    return EntryResult(action, reason, notification=notification, **fields)


def quote_price(quote, field, now, symbol=None):
    if (not isinstance(quote, dict) or not finite_positive(quote.get(field))
            or (symbol is not None and quote.get('contract_symbol') != symbol)):
        raise OrderError('invalid_quote')
    try:
        timestamp = datetime.fromisoformat(quote['as_of'])
        if timestamp.tzinfo is None:
            raise ValueError()
        age = (now.astimezone(timezone.utc) - timestamp.astimezone(timezone.utc)).total_seconds()
        if not -config.MAX_QUOTE_SKEW_SECONDS <= age <= config.MAX_QUOTE_AGE_SECONDS:
            raise ValueError()
    except (KeyError, TypeError, ValueError):
        raise OrderError('stale_or_invalid_quote') from None
    return float(quote[field])


def process_entry(alert, mcp, mode='dry_run', *, state=None, policy=None, now=None):
    now = now or datetime.now(timezone.utc)
    started = time.monotonic()
    original_alert = alert
    policy = policy or config.Policy()
    if mode not in {'dry_run', 'live'} or mcp.mode != mode:
        raise ValueError('client mode mismatch')
    state = state or TradingState(mode, getattr(mcp, 'account_number', None))
    if state.mode != mode or (mode == 'live' and state.account != mcp.account_number):
        raise ValueError('state/client account mismatch')
    try:
        alert = admit(alert, mode, policy, now)
    except AdmissionError as exc:
        return result('rejected', str(exc), mode)
    if not kill.can_fire(context=state):
        return result('blocked', 'not_armed_or_halted', mode)
    if not config.in_market_hours(now):
        return result('blocked', 'outside_market_hours', mode)
    try:
        if mode == 'live':
            mcp.assert_mutation_allowed()
        with state.transaction() as tx:
            reconcile(tx, mcp, now)
            if tx.data['halt_reason'] or any(o['status'] in UNRESOLVED for o in tx.data['orders'].values()):
                return result('blocked', 'orders_require_reconciliation', mode)
            alert_key = f'{alert["handle"]}:{alert["id"]}'
            if alert_key in tx.data['alerts']:
                return result('blocked', 'duplicate_alert', mode)
            tx.data['alerts'][alert_key] = {'posted_at': alert['posted_at'], 'source_id': alert['source_id']}
            event(tx.data, 'alert_reserved', now, alert_key=alert_key)
            tx.save()
            contract = resolver.resolve(alert['text'], alert['handle'], alert['posted_at'], mcp)
            if isinstance(contract, resolver.Ambiguous):
                return result('ambiguous', 'ambiguous_contract', mode)
            if contract.premium is None:
                return result('needs_manual', 'missing_entry_price', mode, contract=contract)
            if not finite_positive(contract.premium):
                return result('rejected', 'invalid_entry_price', mode)
            quote = mcp.get_option_quote(contract.option_id)
            quote_now = now + timedelta(seconds=max(0, time.monotonic() - started))
            ask = quote_price(quote, 'ask', quote_now, contract.contract_symbol)
            cap = Decimal(str(contract.premium)) * (Decimal('1') + Decimal(str(policy.max_slippage)))
            if Decimal(str(ask)) > cap:
                return result('blocked', 'chase_limit_exceeded', mode, contract=contract)
            limit = min(cap, Decimal(str(ask))).quantize(Decimal('.01'), rounding=ROUND_DOWN)
            if limit <= 0:
                return result('blocked', 'invalid_limit', mode)
            qty = int(Decimal(str(policy.trade_target)) // (limit * 100))
            if qty < 1:
                return result('blocked', 'trade_exceeds_budget', mode)
            exposure = sum(p['qty_remaining'] * p['fill_price'] * 100 for p in tx.data['positions'])
            if exposure + qty * float(limit) * 100 > policy.max_exposure:
                return result('blocked', 'exposure_limit', mode)
            today = now.astimezone(config.MARKET_TZ).date()
            realized = sum(e.get('realized_pnl', 0) for e in tx.data['events']
                           if e['event'] == 'exit_fill' and datetime.fromisoformat(e['ts']).astimezone(config.MARKET_TZ).date() == today)
            if realized <= -policy.daily_loss_cap:
                return result('blocked', 'daily_loss_limit', mode)
            verify_broker_positions(tx.data, mcp)
            preview = mcp.review_option_order(option_id=contract.option_id, side='buy',
                                                 position_effect='open', qty=qty,
                                                 order_type='limit', limit_price=float(limit),
                                                 underlying=contract.underlying)
            if not isinstance(preview, dict) or preview.get('approved') is not True:
                return result('rejected', 'preview_not_approved', mode)
            def validate_submission():
                current = now + timedelta(seconds=max(0, time.monotonic() - started))
                if not config.in_market_hours(current):
                    raise OrderError('market_closed_before_submission')
                admit(original_alert, mode, policy, current)
                quote_price(quote, 'ask', current, contract.contract_symbol)
            key = intent_id(state, 'entry:' + alert_key)
            order = submit(tx, state, mcp, {
                'intent_id': key, 'position_id': key, 'alert_key': alert_key,
                'option_id': contract.option_id, 'contract_symbol': contract.contract_symbol,
                'underlying': contract.underlying,
                'side': 'buy', 'position_effect': 'open', 'quantity': qty, 'order_type': 'limit',
                'limit_price': float(limit), 'trader_premium': contract.premium,
            }, now, paper_price=float(limit), validate=validate_submission)
            action = ('fired' if order['status'] == 'filled' else 'pending' if order['status'] in UNRESOLVED
                      else 'partially_filled' if order['filled_qty'] else 'rejected')
            return result(action, 'order_' + order['status'], mode, contract=contract, qty=order['filled_qty'],
                          limit=float(limit), order_id=order['broker_order_id'], fill_price=order['avg_fill_price'])
    except StateError:
        return result('blocked', 'state_unavailable_or_corrupt', mode)
    except OrderError:
        return result('blocked', 'order_or_broker_state_requires_review', mode)
    except Exception:
        # Provider text, credentials, post text and exception bodies never reach the notifier.
        return result('rejected', 'provider_or_configuration_error', mode)
