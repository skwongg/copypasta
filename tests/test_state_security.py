"""Offline filesystem and concurrency checks for the new isolated state store."""
import copy
import json
import multiprocessing
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from trade_state import StateBusy, StateError, TradingState


def competing_snapshot(root, queue):
    try:
        TradingState(root=root).snapshot()
        queue.put("unexpected success")
    except StateBusy:
        queue.put("busy")
    except Exception as error:
        queue.put(type(error).__name__)


class StateSecurityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve() / "state"
        self.state = TradingState(root=self.root)

    def tearDown(self):
        self.temp.cleanup()

    def position(self):
        return {"position_id": "pos1", "contract_symbol": "QQQ   260918C00734000",
                "underlying": "QQQ", "qty_initial": 3, "qty_remaining": 3,
                "fill_price": .56, "latches": {"sl": False, "tp50": False, "tp200": False, "tp300": False},
                "status": "open"}

    def order(self):
        return {"intent_id": "order1", "side": "buy", "quantity": 3,
                "filled_qty": 0, "status": "prepared", "metadata": {"symbol": "QQQ"}}

    def corrupt(self, data):
        self.state.snapshot()
        self.state.path.write_text(json.dumps(data))

    def test_construction_has_no_io_and_modes_accounts_are_strict(self):
        with mock.patch("os.open", side_effect=AssertionError("constructor IO")):
            state = TradingState(root=self.root)
        self.assertEqual(state.account, "paper")
        self.assertFalse(self.root.exists())
        for arguments in ({"mode": "simulation"}, {"mode": "live"}, {"account": "../live"},
                          {"account": "a/b"}, {"account": "."}, {"account": ""}, {"account": True}):
            with self.subTest(arguments=arguments), self.assertRaises(StateError):
                TradingState(root=self.root, **arguments)

    def test_lazy_paper_initialization_private_permissions_and_explicit_save(self):
        with self.state.transaction() as tx:
            tx.data["positions"].append(self.position())
            tx.data["orders"]["order1"] = self.order()
            tx.save()
        data = self.state.snapshot()
        self.assertEqual(data["positions"][0]["qty_remaining"], 3)
        for directory in (self.root, self.root / "dry_run", self.state.directory):
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.state.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((self.state.directory / ".lock").stat().st_mode), 0o600)
        data["positions"].clear()
        self.assertEqual(len(self.state.snapshot()["positions"]), 1)
        with self.state.transaction() as tx:
            tx.data["positions"].clear()
        self.assertEqual(len(self.state.snapshot()["positions"]), 1)
        with self.assertRaises(StateError):
            tx.save()

    def test_live_needs_explicit_initialization_and_never_overwrites(self):
        live = TradingState(mode="live", account="account_1", root=self.root)
        with self.assertRaises(StateError):
            live.snapshot()
        live.initialize()
        self.assertEqual(live.snapshot()["positions"], [])
        with self.assertRaises(StateError):
            live.initialize()
        second = TradingState(mode="live", account="account_2", root=self.root)
        with second.transaction(initialize=True) as tx:
            self.assertEqual(tx.data["account"], "account_2")

    def test_account_and_mode_namespaces_are_disjoint(self):
        with self.state.transaction() as tx:
            tx.data["positions"].append(self.position())
            tx.save()
        self.assertEqual(TradingState(account="other", root=self.root).snapshot()["positions"], [])
        live = TradingState(mode="live", account="paper", root=self.root)
        live.initialize()
        self.assertEqual(live.snapshot()["positions"], [])
        self.assertNotEqual(live.path, self.state.path)

    def test_nonblocking_lock_covers_entire_transaction_across_processes(self):
        context = multiprocessing.get_context("spawn")
        queue = context.Queue()
        with self.state.transaction():
            child = context.Process(target=competing_snapshot, args=(str(self.root), queue))
            child.start()
            self.assertEqual(queue.get(timeout=10), "busy")
            child.join(timeout=10)
            self.assertFalse(child.is_alive())
            self.assertEqual(child.exitcode, 0)
        self.assertEqual(self.state.snapshot()["mode"], "dry_run")
        queue.close()

    def test_corrupt_and_wrong_namespace_state_never_becomes_empty(self):
        good = self.state.snapshot()
        for replacement in ([], {}, {**good, "version": True}, {**good, "version": 2},
                            {**good, "mode": "live"}, {**good, "account": "someone_else"},
                            {**good, "unexpected": 1}, {**good, "events": ["bad"]},
                            {**good, "alerts": {"x": "bad"}}, {**good, "halt_reason": {}}):
            with self.subTest(replacement=replacement):
                self.state.path.write_text(json.dumps(replacement))
                with self.assertRaises(StateError):
                    self.state.snapshot()
        for raw in ("", "{", '{"version":1,"version":1}', '{"version":NaN}', "null"):
            with self.subTest(raw=raw):
                self.state.path.write_text(raw)
                with self.assertRaises(StateError):
                    self.state.snapshot()

    def test_invalid_positions_orders_and_nested_nonfinite_values_rejected_on_save(self):
        base = self.state.snapshot()
        changes = [
            {"positions": [{**self.position(), "fill_price": value}]} for value in (0, -1, True, float("nan"), float("inf"), 10 ** 400)
        ]
        changes += [{"positions": [{**self.position(), field: value}]} for field, value in (
            ("qty_initial", 0), ("qty_initial", 3.0), ("qty_remaining", -1), ("qty_remaining", True),
            ("qty_remaining", 4), ("qty_remaining", 0), ("status", "closed"),
            ("contract_symbol", "SPY   260918C00734000"), ("contract_symbol", "QQQ   260230C00734000"), ("latches", {}))]
        changes += [{"positions": [self.position(), self.position()]}, {"events": [{"nested": [float("nan")]}]}]
        changes += [{"orders": {"order1": {**self.order(), field: value}}} for field, value in (
            ("intent_id", "different"), ("side", "short"), ("quantity", True), ("filled_qty", -1),
            ("filled_qty", 4), ("status", "maybe"))]
        for patch in changes:
            with self.subTest(patch=patch), self.state.transaction() as tx:
                tx.data.update(copy.deepcopy(patch))
                with self.assertRaises(StateError):
                    tx.save()
            self.assertEqual(self.state.snapshot(), base)

    def test_rejects_symlinked_root_state_file_lock_and_namespace(self):
        real = self.root.parent / "real"
        real.mkdir()
        linked = self.root.parent / "linked"
        linked.symlink_to(real, target_is_directory=True)
        with self.assertRaises(StateError):
            TradingState(root=linked).snapshot()
        self.state.snapshot()
        backup = self.root.parent / "backup"
        self.state.path.rename(backup)
        self.state.path.symlink_to(backup)
        with self.assertRaises(StateError):
            self.state.snapshot()
        self.state.path.unlink()
        backup.rename(self.state.path)
        lock = self.state.directory / ".lock"
        lock.unlink()
        lock.symlink_to(self.state.path)
        with self.assertRaises(StateError):
            self.state.snapshot()
        (self.root / "live").symlink_to(self.root / "dry_run", target_is_directory=True)
        with self.assertRaises(StateError):
            TradingState(mode="live", account="paper", root=self.root).initialize()

    def test_rejects_insecure_permissions_and_hardlinked_state(self):
        self.state.snapshot()
        self.state.path.chmod(0o644)
        with self.assertRaises(StateError):
            self.state.snapshot()
        self.state.path.chmod(0o600)
        os.link(self.state.path, self.root.parent / "hardlink")
        with self.assertRaises(StateError):
            self.state.snapshot()

    def test_atomic_write_failure_preserves_previous_valid_state(self):
        before = self.state.snapshot()
        with self.state.transaction() as tx:
            tx.data["events"].append({"event": "must_not_persist"})
            with mock.patch("trade_state.os.replace", side_effect=OSError("simulated write failure")):
                with self.assertRaises(StateError):
                    tx.save()
        self.assertEqual(self.state.snapshot(), before)
        self.assertEqual(list(self.state.directory.glob(".state-*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
