#!/usr/bin/env python3
"""Process explicitly supplied alerts; paper market data is local-only."""
import argparse
import json
import sys
from config import load_policy
from entry_engine import process_entry
from notifications import render
from paper_client import PaperClient
from trade_state import TradingState


def _read_alerts(path):
    if path in ('-', '/dev/stdin'):
        raw = sys.stdin.read(1024 * 1024 + 1)
    else:
        with open(path, encoding='utf-8') as f:
            raw = f.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise ValueError('alert batch too large')
    data = json.loads(raw)
    if not isinstance(data, dict) or not isinstance(data.get('alerts'), list) or len(data['alerts']) > 100:
        raise ValueError('invalid alert batch')
    return data['alerts']


def fire(alerts, mode='dry_run', *, mcp, state=None, policy=None, now=None):
    state = state or TradingState(mode, getattr(mcp, 'account_number', None))
    summary = {'processed': 0, 'fired': 0, 'blocked': 0, 'rejected': 0, 'pending': 0, 'partially_filled': 0, 'ignored': 0}
    for alert in alerts:
        if not isinstance(alert, dict) or alert.get('type') != 'entry':
            summary['ignored'] += 1
            continue
        outcome = process_entry(alert, mcp, mode, state=state, policy=policy, now=now)
        summary['processed'] += 1
        summary[outcome.action if outcome.action in summary else 'rejected'] += 1
        print(render(outcome.notification))
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--alerts-json', required=True)
    parser.add_argument('--mode', choices=('dry_run', 'live'), default='dry_run')
    parser.add_argument('--account')
    parser.add_argument('--market-json', help='explicit offline fixture for paper simulation')
    args = parser.parse_args(argv)
    if args.mode == 'live':
        return live_main(args)
    if not args.market_json:
        parser.error('--market-json is required for credential-free paper execution')
    try:
        mcp = PaperClient.from_file(args.market_json)
        state = TradingState('dry_run', args.account)
        summary = fire(_read_alerts(args.alerts_json), mcp=mcp, state=state, policy=load_policy())
        print(json.dumps(summary))
        return 0
    except Exception:
        print('Input/configuration/state validation failed. No live access attempted.', file=sys.stderr)
        return 1


def live_main(args):
    """Live entry path. Every gate below must pass or nothing fires.

    Activation requires ALL of: LIVE_TRADING_ENABLED in mcp_client,
    the ARMED marker, a configured source allowlist, market hours, and
    the kill-switch armed. process_entry enforces the rest.
    """
    from mcp_client import MCPClient, LIVE_TRADING_ENABLED
    import kill
    # Refuse before touching credentials, input, or the network.
    if not LIVE_TRADING_ENABLED:
        print('Live entry refused: broker adapter is not enabled.', file=sys.stderr)
        return 2
    policy = load_policy()
    if not all(policy.sources.values()):
        print('Live entry refused: policy.sources is not configured.', file=sys.stderr)
        return 2
    probe = MCPClient(mode='live', account_number='DISCOVERY')
    account = probe.discover_agentic_account()
    probe.close()
    if args.account and args.account != account:
        print('Live entry refused: --account does not match the agentic account.', file=sys.stderr)
        return 2
    state = TradingState('live', account)
    if not kill.can_fire(context=state):
        print('Live entry refused: not armed or halted.', file=sys.stderr)
        return 2
    mcp = MCPClient(mode='live', account_number=account, state_context=state)
    mcp.assert_mutation_allowed()  # fails before any network side effect
    summary = fire(_read_alerts(args.alerts_json), 'live', mcp=mcp, state=state,
                   policy=policy)
    print(json.dumps(summary))
    return 0


if __name__ == '__main__':
    sys.exit(main())
