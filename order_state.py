"""Durable order intents and cumulative confirmed-fill accounting."""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import math
import re
import kill
from mcp_client import MCPClient
from trade_state import StateError

UNRESOLVED = {'prepared', 'unknown', 'pending', 'partially_filled'}
TERMINAL = {'filled', 'rejected', 'canceled'}
LATCHES = {'sl': False, 'tp50': False, 'tp200': False, 'tp300': False}

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
    if response.get('ref_id') != order['ref_id']:
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
    if order['side'] == 'sell' and status in {'rejected', 'canceled'}:
        updated['halt_reason'] = 'exit_not_completed'
    event(updated, 'order_status', now, intent_id=key, status=status, filled_qty=filled)
    return updated


def reconcile(tx, mcp, now):
    pending = [o for o in tx.data['orders'].values() if o['status'] in UNRESOLVED]
    if not pending:
        return
    try:
        rows = mcp.get_orders()
        if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
            raise OrderError('invalid_order_snapshot')
        for order in pending:
            matches = [r for r in rows if r.get('ref_id') == order['ref_id']]
            if len(matches) != 1:
                raise OrderError('order_outcome_unresolved')
            tx.data = apply_response(tx.data, order['intent_id'], matches[0], now)
            tx.save()
        if tx.data['halt_reason'] == 'unknown_order_outcome' and not any(o['status'] in UNRESOLVED for o in tx.data['orders'].values()):
            tx.data['halt_reason'] = None
            tx.save()
    except Exception:
        tx.data['halt_reason'] = 'unknown_order_outcome'
        tx.save()
        raise OrderError('order_outcome_unresolved') from None


def verify_broker_positions(data, mcp):
    """Dedicated account: any unexplained position blocks mutations.

    Positions are keyed by broker instrument id, falling back to the OCC
    symbol when the snapshot row carries no id. Either side must identify
    the same contract.
    """
    if mcp.mode != 'live':
        return
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
    for p in data['positions']:
        if p['qty_remaining']:
            key = p.get('option_id') or p['contract_symbol']
            managed[key] = managed.get(key, 0) + p['qty_remaining']
    if actual != managed:
        raise OrderError('broker_positions_do_not_reconcile')
    # Unexpected live open orders also reserve funds/holdings outside our ledger.
    rows = mcp.get_orders(status='open')
    if not isinstance(rows, list) or rows:
        raise OrderError('broker_open_orders_require_review')


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
                 broker_order_id=None, ref_id=ref_id)
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
