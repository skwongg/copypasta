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
        print('Live execution disabled pending broker adapter and deployment review.', file=sys.stderr)
        return 2
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


if __name__ == '__main__':
    sys.exit(main())
