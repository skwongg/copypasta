#!/usr/bin/env python3
"""Run reviewed tests with a temporary HOME and CPython audit guards.

No dependencies, credentials, or broker access are needed. These guards prevent
accidental I/O in this reviewed suite; they are not an OS sandbox for hostile
Python/native code. Spawned multiprocessing workers install the same guards.
"""
import os
from pathlib import Path
import sys

REPO = Path(__file__).resolve().parent


def install_guards():
    import contextvars
    original_home = Path(os.environ['COPYTRADER_TEST_ORIGINAL_HOME']).resolve()
    isolated_home = Path(os.environ['HOME']).resolve()
    runtime_roots = (Path(sys.base_prefix).resolve(), Path(sys.prefix).resolve())

    def inside(path, root):
        return path == root or root in path.parents

    checked_open = contextvars.ContextVar('checked_open', default=False)
    directory_paths = {}

    def check_path(raw):
        path = Path(os.fsdecode(raw)).absolute().resolve()
        if path.name in {'.tokens.json', '.oauth_client.json', 'tokens.json', 'oauth_client.json'} and not inside(path, isolated_home):
            raise PermissionError('test guard: credential file outside isolated HOME')
        if inside(path, original_home) and not any(inside(path, r) for r in (REPO, isolated_home, *runtime_roots)):
            raise PermissionError('test guard: real user home access is disabled')
        return path

    original_open = os.open
    def guarded_open(path, flags, mode=0o777, *, dir_fd=None):
        candidate = Path(os.fsdecode(path))
        if dir_fd is not None and not candidate.is_absolute():
            if dir_fd not in directory_paths:
                raise PermissionError('test guard: unknown directory descriptor')
            candidate = directory_paths[dir_fd] / candidate
        resolved = check_path(candidate)
        token = checked_open.set(True)
        try:
            descriptor = original_open(path, flags, mode, dir_fd=dir_fd)
        finally:
            checked_open.reset(token)
        directory_paths[descriptor] = resolved
        return descriptor

    def guard(event, args):
        if event in {'socket.connect', 'socket.bind', 'socket.getaddrinfo', 'socket.gethostbyname',
                     'socket.gethostbyaddr', 'socket.sendto', 'socket.sendmsg', 'subprocess.Popen', 'os.system',
                     'os.posix_spawn', 'os.posix_spawnp'}:
            raise PermissionError('test guard: network and external commands are disabled')
        if event == 'open' and not checked_open.get() and isinstance(args[0], (str, bytes, os.PathLike)):
            check_path(args[0])
    sys.addaudithook(guard)
    os.open = guarded_open
    sys.path.insert(0, str(REPO))


# Also runs when multiprocessing imports this script as __mp_main__.
if os.environ.get('COPYTRADER_TEST_WORKER') == '1':
    install_guards()


def worker():
    import unittest
    suite = unittest.defaultTestLoader.discover(str(REPO / 'tests'), top_level_dir=str(REPO))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


def main():
    if os.environ.get('COPYTRADER_TEST_WORKER') == '1':
        return worker()
    import subprocess
    import tempfile
    with tempfile.TemporaryDirectory(prefix='copypasta-tests-') as directory:
        root = Path(directory).resolve()
        isolated_home = root / 'home'
        isolated_home.mkdir(mode=0o700)
        temporary = isolated_home / 'tmp'
        temporary.mkdir(mode=0o700)
        env = {'PATH': os.defpath, 'HOME': str(isolated_home), 'TMPDIR': str(temporary),
               'COPYTRADER_STATE_DIR': str(isolated_home / 'state'),
               'COPYTRADER_CREDENTIAL_DIR': str(isolated_home / 'credentials'),
               'COPYTRADER_TEST_ORIGINAL_HOME': str(Path.home().resolve()),
               'COPYTRADER_TEST_WORKER': '1', 'PYTHONDONTWRITEBYTECODE': '1'}
        completed = subprocess.run([sys.executable, '-I', '-S', '-B', '-W', 'error::ResourceWarning',
                                    str(Path(__file__).resolve())], env=env, cwd=REPO, check=False)
        return completed.returncode


if __name__ == '__main__':
    raise SystemExit(main())
