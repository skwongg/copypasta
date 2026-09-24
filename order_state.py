"""Durable order intents and cumulative confirmed-fill accounting."""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import math
import re
import time
import kill
from mcp_client import MCPClient
from trade_state import StateError

UNRESOLVED = {'prepared', 'unknown', 'pending', 'partially_filled'}
TERMINAL = {'filled', 'rejected', 'canceled'}
LATCHES = {'sl': False, 'tp50': False, 'tp200': False, 'tp300': False}
TAKE_PROFIT_RUNGS = frozenset({'tp50', 'tp200', 'tp300'})

# Strict canonical UUID text: the broker order identity is never guessed.
_UUID_RE = re.compile(r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-'
                      r'[0-9a-fA-F]{4}-[0-9a-fA-F]{12}')


class OrderError(RuntimeError):
    pass


def _response_fingerprint(value, depth=0):
    """Structural shape of a broker response: keys and value types only.

    Recorded when a place response cannot be parsed, so the next
    unknown_order_outcome halt carries exactly what the parser needs to
    learn. Never includes values: no ids, prices, tokens, or PII.
    """
    if depth > 2:
        return '...'
    if isinstance(value, dict):
        return {str(k): _response_fingerprint(v, depth + 1)
                for k, v in list(value.items())[:25]}
    if isinstance(value, list):
        if not value:
            return 'list[0]'
        return ['list[%d]' % len(value), _response_fingerprint(value[0], depth + 1)]
    return type(value).__name__


def finite_positive(value):
    try:
        return type(value) in (int, float) and math.isfinite(value) and value > 0
    except OverflowError:
        return False


def event(data, kind, now, **fields):
    record = {'event': kind, 'ts': now.astimezone(timezone.utc).isoformat(), **fields}
    data['events'].append(record)
    return record


def intent_id(store, key):
    return hashlib.sha256(f'{store.mode}:{store.account}:{key}'.encode()).hexdigest()


def apply_response(data, key, response, now):
    """Validate before modifying. Acknowledgements never fabricate executions."""
    updated = deepcopy(data)
    order = updated['orders'][key]
    if not isinstance(response, dict) or response.get('isError'):
        raise OrderError('invalid_order_response')
    status, broker_id = response.get('status'), response.get('order_id')
    filled, avg = response.get('filled_qty'), response.get('avg_fill_price')
    if status not in UNRESOLVED | TERMINAL or status in {'prepared', 'unknown'}:
        raise OrderError('invalid_order_status')
    if not isinstance(broker_id, str) or not _UUID_RE.fullmatch(broker_id):
        raise OrderError('invalid_broker_order_id')
    if order.get('broker_order_id') not in (None, broker_id):
        raise OrderError('broker_order_identity_changed')
    for field in ('contract_symbol', 'side', 'quantity'):
        if response.get(field) != order[field]:
            raise OrderError('broker_order_intent_mismatch')
    # Broker order rows do not echo ref_id (verified on live rows 2026-09-24);
    # identity then rests on the broker order id and the leg's option_id.
    if response.get('ref_id') is not None and response['ref_id'] != order['ref_id']:
        raise OrderError('broker_order_intent_mismatch')
    if response.get('option_id') != order['option_id']:
        raise OrderError('broker_order_intent_mismatch')
    if type(filled) is not int or not order['filled_qty'] <= filled <= order['quantity']:
        raise OrderError('invalid_cumulative_fill')
    if (filled > 0 and not finite_positive(avg)) or (filled == 0 and avg not in (None, 0)):
        raise OrderError('invalid_execution_price')
    if status == 'filled' and filled != order['quantity']:
        raise OrderError('incomplete_filled_order')
    if status == 'partially_filled' and not 0 < filled < order['quantity']:
        raise OrderError('invalid_partial_order')
    if status == 'rejected' and filled:
        raise OrderError('rejected_order_has_fills')
    if order['status'] in TERMINAL and (status != order['status'] or filled != order['filled_qty']):
        raise OrderError('terminal_order_changed')
    delta = filled - order['filled_qty']
    previous_value = order['filled_qty'] * (order.get('avg_fill_price') or 0)
    current_value = filled * (avg or 0)
    if not delta and current_value != previous_value:
        raise OrderError('execution_correction_requires_review')
    if delta and current_value <= previous_value:
        raise OrderError('invalid_incremental_execution')
    if order['side'] == 'buy' and delta and (current_value - previous_value) / delta > order['limit_price'] + 1e-8:
        raise OrderError('execution_exceeded_limit')
    if order['side'] == 'sell' and delta and order.get('limit_price') is not None and (current_value - previous_value) / delta < order['limit_price'] - 1e-8:
        raise OrderError('execution_below_sell_limit')
    if delta:
        positions = updated['positions']
        position = next((p for p in positions if p['position_id'] == order['position_id']), None)
        if order['side'] == 'buy':
            if position is None:
                position = {'position_id': order['position_id'], 'option_id': order['option_id'],
                            'contract_symbol': order['contract_symbol'],
                            'underlying': order['underlying'], 'qty_initial': 0, 'qty_remaining': 0,
                            'fill_price': avg, 'latches': dict(LATCHES), 'status': 'open'}
                positions.append(position)
            position['qty_initial'] += delta
            position['qty_remaining'] += delta
            position['fill_price'] = avg
            position['status'] = 'open'
            event(updated, 'entry_fill', now, intent_id=key, contract_symbol=order['contract_symbol'],
                  qty=delta, price=(current_value - previous_value) / delta)
        else:
            if position is None or delta > position['qty_remaining']:
                raise OrderError('sale_exceeds_managed_position')
            realized = round(((current_value - previous_value) - position['fill_price'] * delta) * 100, 2)
            position['qty_remaining'] -= delta
            position['status'] = 'open' if position['qty_remaining'] else 'closed'
            event(updated, 'exit_fill', now, intent_id=key, contract_symbol=order['contract_symbol'],
                  qty=delta, price=(current_value - previous_value) / delta, realized_pnl=realized)
    order.update(status=status, broker_order_id=broker_id, filled_qty=filled, avg_fill_price=avg)
    if order['side'] == 'sell' and status == 'filled':
        position = next(p for p in updated['positions'] if p['position_id'] == order['position_id'])
        position['latches'][order['rung']] = True
    repriced = order.get('cancel_requested') and status == 'canceled'
    if order['side'] == 'sell' and repriced and filled and order['rung'] in {'tp50', 'tp200'}:
        # A partial take-profit counts as the rung; re-halving the remainder would oversell.
        position = next(p for p in updated['positions'] if p['position_id'] == order['position_id'])
        position['latches'][order['rung']] = True
    if order['side'] == 'sell' and status in {'rejected', 'canceled'} and not repriced:
        updated['halt_reason'] = 'exit_not_completed'
    event(updated, 'order_status', now, intent_id=key, status=status, filled_qty=filled)
    return updated


def is_resting_take_profit(order):
    """A take-profit sell working at the broker at its fixed rung price.

    It is expected to rest until the market comes back to it, so it does not
    freeze the account the way an unresolved entry or stop does.
    """
    return (order['side'] == 'sell' and order.get('rung') in TAKE_PROFIT_RUNGS
            and order['status'] in {'pending', 'partially_filled'} and bool(order.get('broker_order_id')))


def blocking_orders(data):
    """Unresolved orders that must be reconciled before any new decision."""
    return [o for o in data['orders'].values() if o['status'] in UNRESOLVED and not is_resting_take_profit(o)]


def _broker_row(rows, order, claimed):
    """The broker row for one of our orders.

    Known broker id first. Otherwise (the place response was lost or
    unparseable) an echoed ref_id, and failing that the single unclaimed
    agentic order for the same contract, side and quantity created after we
    submitted.
    """
    if order.get('broker_order_id'):
        return [r for r in rows if r.get('order_id') == order['broker_order_id']]
    matches = [r for r in rows if r.get('ref_id') is not None and r['ref_id'] == order['ref_id']]
    if matches:
        return matches
    return [r for r in rows
            if r.get('ref_id') is None and r.get('order_id') not in claimed
            and r.get('placed_agent') in (None, 'agentic')
            and (r.get('option_id'), r.get('side'), r.get('quantity'))
            == (order['option_id'], order['side'], order['quantity'])
            and _created_after(r.get('created_at'), order.get('submitted_at'))]


def _created_after(created_at, submitted_at, slack=60):
    """False only when both timestamps parse and the row predates the intent."""
    try:
        created = datetime.fromisoformat(created_at)
        submitted = datetime.fromisoformat(submitted_at)
        return (created - submitted).total_seconds() >= -slack
    except (TypeError, ValueError):
        return True


def reconcile(tx, mcp, now):
    pending = [o for o in tx.data['orders'].values() if o['status'] in UNRESOLVED]
    if not pending:
        return
    try:
        rows = mcp.get_orders()
        if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
            raise OrderError('invalid_order_snapshot')
        claimed = {o.get('broker_order_id') for o in tx.data['orders'].values()} - {None}
        for order in pending:
            matches = _broker_row(rows, order, claimed)
            if len(matches) != 1:
                raise OrderError('order_outcome_unresolved')
            tx.data = apply_response(tx.data, order['intent_id'], matches[0], now)
            claimed.add(matches[0]['order_id'])
            tx.save()
        if tx.data['halt_reason'] == 'unknown_order_outcome' and not any(o['status'] in UNRESOLVED for o in tx.data['orders'].values()):
            tx.data['halt_reason'] = None
            tx.save()
    except Exception:
        tx.data['halt_reason'] = 'unknown_order_outcome'
        tx.save()
        raise OrderError('order_outcome_unresolved') from None


def verify_broker_positions(data, mcp, now=None):
    """Dedicated account: the broker is the truth for our holdings.

    Contracts the broker no longer holds were closed outside the bot (by
    hand in the app); the managed quantity shrinks to match and an
    external_close event records it. Holdings the bot never bought, or more
    than it bought, still block mutations. Returns True when data changed.

    Positions are keyed by broker instrument id, falling back to the OCC
    symbol when the snapshot row carries no id. Either side must identify
    the same contract.
    """
    if mcp.mode != 'live':
        return False
    rows = mcp.get_positions()
    if not isinstance(rows, list):
        raise OrderError('invalid_broker_position_snapshot')
    actual, managed = {}, {}
    for row in rows:
        if not isinstance(row, dict) or type(row.get('quantity')) is not int or row['quantity'] < 0:
            raise OrderError('invalid_broker_position_snapshot')
        key = row.get('option_id') or row.get('contract_symbol')
        if not isinstance(key, str) or not key:
            raise OrderError('invalid_broker_position_snapshot')
        if row['quantity']:
            if key in actual:
                raise OrderError('duplicate_broker_position')
            actual[key] = row['quantity']
    changed = False
    for p in data['positions']:
        if not p['qty_remaining']:
            continue
        key = p.get('option_id') or p['contract_symbol']
        held = actual.get(key, 0) - managed.get(key, 0)
        gone = p['qty_remaining'] - max(0, min(held, p['qty_remaining']))
        if gone:
            p['qty_remaining'] -= gone
            p['status'] = 'open' if p['qty_remaining'] else 'closed'
            event(data, 'external_close', now or datetime.now(timezone.utc), position_id=p['position_id'],
                  contract_symbol=p['contract_symbol'], qty=gone)
            changed = True
        if p['qty_remaining']:
            managed[key] = managed.get(key, 0) + p['qty_remaining']
    if actual != managed:
        raise OrderError('broker_positions_do_not_reconcile')
    # Unexpected live open orders also reserve funds/holdings outside our ledger.
    ours = {o.get('broker_order_id') for o in data['orders'].values() if o['status'] in UNRESOLVED} - {None}
    rows = mcp.get_orders(status='open')
    if not isinstance(rows, list) or any(not isinstance(r, dict) or r.get('order_id') not in ours for r in rows):
        raise OrderError('broker_open_orders_require_review')
    return changed


def cancel_orders(tx, mcp, now, keys, *, polls=4, pause=1.0, sleep=None):
    """Request cancellation of our working orders and poll until they settle.

    Cancellation is asynchronous at the broker, so each order is polled a few
    times. Returns the keys still unresolved; the next sweep's reconcile picks
    those up. A canceled sale we asked for never halts the account.
    """
    sleep = sleep or time.sleep
    for key in keys:
        order = tx.data['orders'][key]
        if not order.get('cancel_requested'):
            order['cancel_requested'] = True
            event(tx.data, 'cancel_requested', now, intent_id=key, side=order['side'])
            tx.save()
            try:
                mcp.cancel_order(order['broker_order_id'])
            except Exception:
                pass  # e.g. it filled first; the order snapshot below decides
        for attempt in range(polls):
            reconcile(tx, mcp, now)
            if tx.data['orders'][key]['status'] not in UNRESOLVED:
                break
            if attempt + 1 < polls:
                sleep(pause)
    return [key for key in keys if tx.data['orders'][key]['status'] in UNRESOLVED]


def cancel_stale_orders(tx, mcp, now, *, sell_after, buy_after, **polling):
    """Cancel resting stop-loss sells the market moved away from, and stale entries.

    A stop-loss sell older than ``sell_after`` seconds whose limit is above the
    current bid is canceled so the exit sweep re-places it at the bid. A buy
    older than ``buy_after`` is canceled for good: the alert's price is gone,
    and a resting entry would freeze every exit. Take-profits are never
    repriced; they hold at their fixed rung price.
    """
    stale = []
    for order in tx.data['orders'].values():
        if order['status'] not in {'pending', 'partially_filled'} or not order.get('broker_order_id'):
            continue
        if order['side'] == 'sell' and order.get('rung') != 'sl':
            continue
        try:
            age = (now - datetime.fromisoformat(order['submitted_at'])).total_seconds()
        except (KeyError, TypeError, ValueError):
            age = float('inf')
        if age < (sell_after if order['side'] == 'sell' else buy_after):
            continue
        if order['side'] == 'sell' and not order.get('cancel_requested'):
            try:
                bid = mcp.get_option_quote(order['option_id'])['bid']
                if finite_positive(bid) and bid >= order['limit_price']:
                    continue  # still at the bid; keep its place in the queue
            except Exception:
                pass
        stale.append(order['intent_id'])
    cancel_orders(tx, mcp, now, stale, **polling)
    return stale


def submit(tx, store, mcp, order, now, *, paper_price, validate=None):
    key = order['intent_id']
    if key in tx.data['orders']:
        raise OrderError('duplicate_order_intent')
    if not kill.can_fire(context=store):
        raise OrderError('halted_before_submission')
    if mcp.mode == 'live':
        mcp.assert_mutation_allowed()
    if validate is not None:
        validate()
    # Deterministic idempotency key: retries of this logical order reuse it.
    ref_id = MCPClient.make_ref_id(key)
    order = dict(order, status='prepared', filled_qty=0, avg_fill_price=None,
                 broker_order_id=None, ref_id=ref_id,
                 submitted_at=now.astimezone(timezone.utc).isoformat())
    tx.data['orders'][key] = order
    tx.save()  # intent exists before an external side effect or process crash
    response = None
    try:
        if validate is not None:
            validate()
        if not kill.can_fire(context=store):
            raise OrderError('halted_before_submission')
    except Exception:
        order['status'] = 'rejected'
        tx.save()
        raise OrderError('submission_validation_failed') from None
    try:
        if mcp.mode == 'dry_run':
            response = {
                'order_id': MCPClient.make_ref_id('paper-order:' + key),
                'ref_id': ref_id, 'option_id': order['option_id'],
                'contract_symbol': order['contract_symbol'], 'side': order['side'],
                'quantity': order['quantity'], 'status': 'filled',
                'filled_qty': order['quantity'], 'avg_fill_price': paper_price,
            }
        else:
            response = mcp.place_option_order(
                option_id=order['option_id'], side=order['side'],
                position_effect=order['position_effect'], qty=order['quantity'],
                order_type=order['order_type'], limit_price=order['limit_price'],
                ref_id=order['ref_id'],
            )
        tx.data = apply_response(tx.data, key, response, now)
        tx.save()
        return tx.data['orders'][key]
    except Exception as exc:
        if tx.data['orders'][key]['status'] != 'rejected':
            tx.data['orders'][key]['status'] = 'unknown'
            tx.data['halt_reason'] = 'unknown_order_outcome'
            # Prefer the raw broker shape (attached by place/cancel when the
            # parser rejects it); fall back to the normalized response.
            raw = getattr(exc, 'raw_response', None)
            if raw is None:
                raw = response
            if raw is not None:
                # Capture the shape the parser could not read (keys/types only,
                # never values) so the halt is diagnosable, not a mystery.
                event(tx.data, 'unparseable_order_response', now, intent_id=key,
                      fingerprint=_response_fingerprint(raw))
            tx.save()
        raise OrderError('order_outcome_unresolved') from None
