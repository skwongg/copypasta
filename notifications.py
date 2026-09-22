"""Deterministic terminal notifications. Never wakes an agent or invokes tools."""
import argparse
import math
import re
import sys
from trade_state import TradingState

CODES = {
    'not_armed_or_halted': 'Trader is disarmed or halted; monitor any existing broker orders manually.',
    'outside_market_hours': 'Outside supported market hours.',
    'missing_entry_price': 'Entry price is missing; manual review required.',
    'ambiguous_contract': 'Trade text or contract identity is ambiguous; manual review required.',
    'duplicate_alert': 'Duplicate alert ignored.',
    'chase_limit_exceeded': 'Quoted ask exceeds the entry slippage cap.',
    'trade_exceeds_budget': 'One contract exceeds the trade budget.',
    'exposure_limit': 'Open exposure cap reached.',
    'daily_loss_limit': 'Daily realized loss cap reached.',
    'preview_not_approved': 'Broker preview did not approve this order.',
    'orders_require_reconciliation': 'Outstanding orders need reconciliation; no new order submitted.',
    'order_or_broker_state_requires_review': 'Broker order/position state needs review; execution stopped.',
    'state_unavailable_or_corrupt': 'State is unavailable or corrupt; execution stopped.',
    'provider_or_configuration_error': 'Provider/configuration validation failed; execution stopped.',
    'order_filled': 'Confirmed fill.',
    'order_pending': 'Order pending; no completed fill claimed.',
    'order_partially_filled': 'Order partially filled; reconciliation required.',
    'order_rejected': 'Order rejected.',
    'order_canceled': 'Order canceled.',
}


def render(note):
    prefix = '[PAPER]' if note.get('mode') == 'dry_run' else '[LIVE]' if note.get('mode') == 'live' else '[INVALID]'
    message = CODES.get(note.get('code'), 'Input rejected or manual review required.')
    parts = [prefix, message]
    symbol = note.get('contract_symbol')
    if isinstance(symbol, str) and re.fullmatch(r'[A-Z]{1,6} *[0-9]{6}[CP][0-9]{8}', symbol) and len(symbol) == 21:
        parts.append(symbol)
    qty = note.get('qty')
    if type(qty) is int and 0 <= qty <= 100000:
        parts.append(f'Filled quantity: {qty}.')
    price = note.get('fill_price')
    if type(price) in (int, float) and math.isfinite(price) and 0 < price <= 100000:
        parts.append(f'Execution price: {price:.2f}.')
    return ' '.join(parts)


def drain(state, stream=None):
    stream = stream or sys.stdout
    with state.transaction() as tx:
        for event in tx.data['events']:
            if event.get('notified') or event['event'] not in {'entry_fill', 'exit_fill', 'order_status'}:
                continue
            code = 'order_filled' if event['event'].endswith('_fill') else 'order_' + event.get('status', 'unknown')
            stream.write(render({'mode': state.mode, 'code': code, 'contract_symbol': event.get('contract_symbol'),
                                 'qty': event.get('qty'), 'fill_price': event.get('price')}) + '\n')
            stream.flush()
            event['notified'] = True
            tx.save()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=('dry_run', 'live'), default='dry_run')
    parser.add_argument('--account')
    args = parser.parse_args(argv)
    drain(TradingState(args.mode, args.account))


if __name__ == '__main__':
    main()
