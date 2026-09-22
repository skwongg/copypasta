"""Prove the runner's guards and side-effect-free imports without real secrets."""
import importlib
import os
from pathlib import Path
import socket
import sys
import unittest


class IsolationSecurityTests(unittest.TestCase):
    def test_runner_has_clean_environment_and_network_is_denied(self):
        self.assertEqual(os.environ.get('COPYTRADER_TEST_WORKER'), '1', 'run python3 run_tests.py')
        self.assertFalse(any(key in os.environ for key in ('GH_TOKEN', 'GITHUB_TOKEN', 'OPENAI_API_KEY', 'ANTHROPIC_API_KEY')))
        with self.assertRaises(PermissionError):
            socket.getaddrinfo('example.invalid', 443)
        with socket.socket() as client, self.assertRaises(PermissionError):
            client.connect(('127.0.0.1', 9))
        with self.assertRaises(PermissionError):
            sys.audit('socket.sendmsg', None, ('127.0.0.1', 9))

    def test_guard_refuses_real_home_and_checkout_credentials_before_io(self):
        home = Path(os.environ['COPYTRADER_TEST_ORIGINAL_HOME'])
        for path in (home / '.copypasta-audit-nonexistent', Path(__file__).parents[1] / '.tokens.json'):
            with self.subTest(path=path.name), self.assertRaises(PermissionError):
                path.read_bytes()

    def test_imports_do_not_read_write_or_delete_production_named_sentinels(self):
        home = Path.home()
        legacy = home / 'workspace/copy-trader'
        legacy.mkdir(parents=True, exist_ok=True)
        paths = [legacy / name for name in ('.tokens.json', '.oauth_client.json', 'notifications.jsonl', 'positions.json', 'ARMED')]
        for path in paths:
            path.write_text('SENTINEL-NOT-A-CREDENTIAL')
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in paths}
        importing = True
        protected_names = {p.name for p in paths}
        def forbid_sentinel_reads(event, args):
            if importing and event == 'open' and isinstance(args[0], (str, bytes, os.PathLike)) and Path(os.fsdecode(args[0])).name in protected_names:
                raise AssertionError('module import attempted credential or production-state I/O')
        sys.addaudithook(forbid_sentinel_reads)
        try:
            self.import_fresh_modules()
        finally:
            importing = False
        self.assertEqual(before, {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in paths})

    def import_fresh_modules(self):
        for name in ('config', 'admission', 'resolver', 'mcp_client', 'oauth_client', 'trade_state',
                     'kill', 'order_state', 'entry_engine', 'exits', 'ledger', 'notifications',
                     'paper_client', 'fire_entries', 'run_exits', 'tests.test_fire_entries', 'tests.test_dryrun'):
            # Fresh execution without changing classes used by already collected tests.
            module = importlib.import_module(name)
            alias = '_audit_import_' + name.replace('.', '_')
            spec = importlib.util.spec_from_file_location(alias, module.__file__)
            fresh = importlib.util.module_from_spec(spec)
            sys.modules[alias] = fresh
            try:
                spec.loader.exec_module(fresh)
            finally:
                sys.modules.pop(alias, None)
