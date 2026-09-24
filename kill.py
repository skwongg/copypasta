"""Scoped operational controls. No import-time I/O or implied broker cancellation."""
import argparse
import os
import stat
from trade_state import TradingState, private_directory, _directory_fd


def can_fire(context=None, mode='dry_run', account=None):
    ctx = context or TradingState(mode, account)
    fd = None
    try:
        fd = _directory_fd(ctx.directory)
        directory = os.fstat(fd)
        if directory.st_uid != os.geteuid() or stat.S_IMODE(directory.st_mode) != 0o700:
            return False
        armed = os.stat('ARMED', dir_fd=fd, follow_symlinks=False)
        if not stat.S_ISREG(armed.st_mode) or armed.st_uid != os.geteuid() or stat.S_IMODE(armed.st_mode) != 0o600:
            return False
        try:
            os.stat('KILL', dir_fd=fd, follow_symlinks=False)
            return False
        except FileNotFoundError:
            return True
    except OSError:
        return False
    finally:
        if fd is not None:
            os.close(fd)


is_armed = can_fire


def live_accounts(root=None):
    """Account namespaces that have live state. Local only: no broker call."""
    base = TradingState(root=root).root / 'live'
    try:
        names = sorted(entry.name for entry in os.scandir(base)
                       if entry.is_dir(follow_symlinks=False) and not entry.name.startswith('.'))
    except OSError:
        return []
    return [name for name in names if (base / name / 'state.json').is_file()]


def any_live_armed(root=None):
    """Cheap pre-check the hooks rely on: is any live account armed and not halted?"""
    return any(can_fire(context=TradingState('live', name, root=root)) for name in live_accounts(root))


def arm(context):
    # Live arming is permitted only through the explicit activation review;
    # the ARMED marker is what the entry/exit hooks and the adapter gates check.
    with context.transaction():
        pass
    directory = _directory_fd(context.directory)
    try:
        fd = os.open('ARMED', os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        os.fchmod(fd, 0o600)
        os.close(fd)
    finally:
        os.close(directory)


def halt(context):
    private_directory(context.directory)
    fd = os.open(context.directory / 'KILL', os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    os.close(fd)


def disarm(context):
    (context.directory / 'ARMED').unlink(missing_ok=True)


def reset(context):
    # Never clears uncertain orders or automatically arms.
    (context.directory / 'KILL').unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    for action in ('arm', 'disarm', 'halt', 'reset', 'status'):
        group.add_argument('--' + action, action='store_true')
    parser.add_argument('--mode', choices=('dry_run', 'live'), default='dry_run')
    parser.add_argument('--account')
    args = parser.parse_args(argv)
    account = args.account
    if args.mode == 'live' and account is None:
        accounts = live_accounts()
        if len(accounts) != 1:
            parser.error('--account is required: found %d live accounts' % len(accounts))
        account = accounts[0]
    ctx = TradingState(args.mode, account)
    for action, fn in (('arm', arm), ('disarm', disarm), ('halt', halt), ('reset', reset)):
        if getattr(args, action):
            fn(ctx)
    print('armed' if can_fire(ctx) else 'halted/disarmed')
    if args.halt or args.disarm:
        print('New submissions disabled. Pending orders are not canceled; monitor holdings manually.')


if __name__ == '__main__':
    main()
