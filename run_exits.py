"""Run the isolated paper exit monitor. Live rollout is separately gated."""
import argparse
import sys
from exits import ExitMonitor
from notifications import render
from paper_client import PaperClient
from trade_state import TradingState


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=('dry_run', 'live'), default='dry_run')
    parser.add_argument('--account')
    parser.add_argument('--market-json')
    args = parser.parse_args(argv)
    if args.mode == 'live':
        return live_main(args)
    if not args.market_json:
        parser.error('--market-json is required')
    try:
        state = TradingState('dry_run', args.account)
        monitor = ExitMonitor(PaperClient.from_file(args.market_json), state=state)
        for note in monitor.check():
            print(render(note))
        return 0
    except Exception:
        print('Input/configuration/state validation failed.', file=sys.stderr)
        return 1


def live_main(args):
    """Live exit-monitor path. Same gates as entries: adapter switch, ARMED
    marker, configured sources, market hours, kill-switch."""
    from mcp_client import MCPClient, LIVE_TRADING_ENABLED
    from config import load_policy
    import kill
    # Refuse before touching credentials, input, or the network.
    if not LIVE_TRADING_ENABLED:
        print('Live exits refused: broker adapter is not enabled.', file=sys.stderr)
        return 2
    policy = load_policy()
    if not all(policy.sources.values()):
        print('Live exits refused: policy.sources is not configured.', file=sys.stderr)
        return 2
    probe = MCPClient(mode='live', account_number='DISCOVERY')
    account = probe.discover_agentic_account()
    probe.close()
    if args.account and args.account != account:
        print('Live exits refused: --account does not match the agentic account.', file=sys.stderr)
        return 2
    state = TradingState('live', account)
    if not kill.can_fire(context=state):
        print('Live exits refused: not armed or halted.', file=sys.stderr)
        return 2
    mcp = MCPClient(mode='live', account_number=account, state_context=state)
    mcp.assert_mutation_allowed()  # fails before any network side effect
    monitor = ExitMonitor(mcp, 'live', state=state)
    for note in monitor.check():
        print(render(note))
    return 0


if __name__ == '__main__':
    sys.exit(main())
