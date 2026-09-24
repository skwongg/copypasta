"""The deployed live chain against a fake broker that speaks Robinhood's real shapes.

watcher payload -> sign_alerts -> fire_entries.py --mode live -> run_exits.py --mode live,
through the real CLIs, MCPClient, state store and lock. Only the MCP transport,
the OAuth token and the wall clock are substituted. The broker fake lives in
tests/fake_robinhood.py; its order rows carry no ref_id, like the real ones.
"""
import contextlib
import io
import json
import os
import secrets
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import entry_engine
import exits
import fire_entries
import kill
import mcp_client
import run_exits
import sign_alerts
from tests import fake_robinhood as frh
from trade_state import TradingState

REPO = Path(__file__).resolve().parents[1]
START = datetime(2026, 9, 24, 14, 7, 10, tzinfo=timezone.utc)   # Thu 07:07 PT


class LivePipelineTests(unittest.TestCase):
    def setUp(self):
        self.now = START
        test = self

        class Frozen(datetime):
            @classmethod
            def now(cls, tz=None):
                return test.now.astimezone(tz) if tz else test.now.replace(tzinfo=None)

        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name).resolve()
        self.key = secrets.token_bytes(32)
        key_file = root / "source_key"
        key_file.write_text(self.key.hex())
        key_file.chmod(0o600)
        env = mock.patch.dict(os.environ, {"COPYTRADER_STATE_DIR": str(root / "state"),
                                           "COPYTRADER_SOURCE_KEY_FILE": str(key_file),
                                           "COPYTRADER_CONFIG": str(REPO / "policy.json")})
        env.start()
        self.addCleanup(env.stop)
        self.rh = frh.FakeRobinhood(lambda: self.now)
        original_init = mcp_client.MCPClient.__init__

        def init(client, *args, **kwargs):
            kwargs.update(transport=self.rh, token_provider="test-token")
            original_init(client, *args, **kwargs)
        for patcher in (mock.patch.object(mcp_client.MCPClient, "__init__", init),
                        mock.patch.object(entry_engine, "datetime", Frozen),
                        mock.patch.object(exits, "datetime", Frozen)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.state = TradingState("live", frh.ACCOUNT)
        self.state.initialize()
        kill.arm(self.state)
        self.alert_id = 1970000000000000000

    # ---- helpers ------------------------------------------------------------
    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)

    def alert(self, text="$SPY 769 CALLS .33"):
        self.alert_id += 1
        return {"status": "alert", "alerts": [{
            "id": str(self.alert_id), "handle": "CassyTrades", "type": "entry",
            "text": f"Cassy\n@CassyTrades\n25s\n{text}\n\nlet's get it\n1.3K",
            "posted_at": (self.now - timedelta(seconds=25)).isoformat(),
            "url": f"https://x.com/CassyTrades/status/{self.alert_id}"}]}

    def fire(self, payload=None):
        signed, _ = sign_alerts.sign_alerts(payload or self.alert(), self.key)
        out = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(json.dumps(signed))), contextlib.redirect_stdout(out):
            rc = fire_entries.main(["--alerts-json", "/dev/stdin", "--mode", "live"])
        return rc, out.getvalue()

    def sweep(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = run_exits.main(["--mode", "live"])
        return rc, out.getvalue()

    def sells(self):
        return [o for o in self.rh.orders if o["legs"][0]["side"] == "sell"]

    def snapshot(self):
        return self.state.snapshot()

    # ---- entries --------------------------------------------------------------
    def test_alert_fires_budget_sized_limit_buy_at_ask(self):
        rc, out = self.fire()
        self.assertEqual(rc, 0, out)
        self.assertEqual(len(self.rh.placed), 1, out)
        order = self.rh.placed[0]
        self.assertEqual((order["legs"][0]["option_id"], order["legs"][0]["side"], order["quantity"],
                          order["type"], order["price"]), (frh.OPTION_ID, "buy", "15", "limit", "0.33"))
        self.assertEqual(self.snapshot()["positions"][0]["qty_remaining"], 15)

    def test_disarmed_refuses_before_any_broker_call(self):
        kill.disarm(self.state)
        with mock.patch.object(mcp_client.MCPClient, "call_tool", side_effect=AssertionError("broker call")):
            self.assertEqual(fire_entries.main(["--alerts-json", "/dev/null", "--mode", "live"]), 2)
            self.assertEqual(run_exits.main(["--mode", "live"]), 2)
        kill.arm(self.state)
        kill.halt(self.state)
        with mock.patch.object(mcp_client.MCPClient, "call_tool", side_effect=AssertionError("broker call")):
            self.assertEqual(fire_entries.main(["--alerts-json", "/dev/null", "--mode", "live"]), 2)

    def test_pending_entry_reconciles_by_broker_order_id(self):
        # Real place responses come back "confirmed"; real order rows never carry ref_id.
        self.rh.place_state = "confirmed"
        self.fire()
        self.advance(170)
        self.rh.fill(self.rh.orders[0]["id"])
        self.sweep()
        data = self.snapshot()
        self.assertIsNone(data["halt_reason"])
        self.assertEqual(data["positions"][0]["qty_remaining"], 15)

    def test_pending_entry_reconciles_when_place_response_omits_ref_id(self):
        self.rh.place_state, self.rh.place_includes_ref_id = "confirmed", False
        self.fire()
        self.advance(60)
        self.rh.fill(self.rh.orders[0]["id"])
        self.sweep()
        self.assertEqual(self.snapshot()["positions"][0]["qty_remaining"], 15)

    def test_unparseable_place_response_recovers_from_order_snapshot(self):
        with mock.patch.object(mcp_client.MCPClient, "normalize_order",
                               side_effect=mcp_client.MCPError("unparseable")):
            self.fire()
        self.assertEqual(self.snapshot()["halt_reason"], "unknown_order_outcome")
        self.advance(60)
        self.sweep()
        data = self.snapshot()
        self.assertIsNone(data["halt_reason"])
        self.assertEqual(data["positions"][0]["qty_remaining"], 15)

    def test_unknown_outcome_never_adopts_a_manual_order(self):
        with mock.patch.object(mcp_client.MCPClient, "normalize_order",
                               side_effect=mcp_client.MCPError("unparseable")):
            self.fire()
        self.rh.orders[0]["placed_agent"] = "user"
        self.advance(60)
        self.sweep()
        self.assertEqual(self.snapshot()["halt_reason"], "unknown_order_outcome")

    def test_unknown_outcome_never_adopts_an_older_identical_order(self):
        with mock.patch.object(mcp_client.MCPClient, "normalize_order",
                               side_effect=mcp_client.MCPError("unparseable")):
            self.fire()
        self.rh.orders[0]["created_at"] = "2026-09-23T14:07:10Z"   # yesterday's identical order
        self.advance(60)
        self.sweep()
        self.assertEqual(self.snapshot()["halt_reason"], "unknown_order_outcome")

    def test_stale_entry_is_canceled_and_unblocks_the_account(self):
        self.rh.place_state = "confirmed"
        self.fire()
        for _ in range(3):
            self.advance(60)
            self.sweep()
        self.assertEqual(self.rh.orders[0]["state"], "cancelled")
        data = self.snapshot()
        self.assertIsNone(data["halt_reason"])
        self.assertEqual(data["positions"], [])
        self.rh.place_state = "filled"
        self.fire()
        self.assertEqual(len(self.rh.placed), 2)

    def test_alert_waits_out_an_exit_sweep_holding_the_lock(self):
        held, release = threading.Event(), threading.Event()

        def sweep_holding_lock():
            with self.state._locked():
                held.set()
                release.wait(5)
        thread = threading.Thread(target=sweep_holding_lock)
        thread.start()
        held.wait()
        timer = threading.Timer(1.0, release.set)
        timer.start()
        try:
            rc, out = self.fire()
        finally:
            release.set()
            thread.join()
            timer.cancel()
        self.assertEqual(len(self.rh.placed), 1, out)

    # ---- exits ----------------------------------------------------------------
    def test_stop_loss_sells_everything_at_the_bid(self):
        self.fire()
        self.rh.bid, self.rh.ask = 0.12, 0.13
        self.advance(60)
        self.sweep()
        sale = self.rh.placed[-1]
        self.assertEqual((sale["legs"][0]["side"], sale["legs"][0]["position_effect"], sale["quantity"],
                          sale["price"]), ("sell", "close", "15", "0.12"))
        data = self.snapshot()
        self.assertEqual(data["positions"][0]["qty_remaining"], 0)
        self.assertEqual([e["realized_pnl"] for e in data["events"] if e["event"] == "exit_fill"], [-315.0])

    def test_take_profit_ladder(self):
        self.fire()
        self.rh.bid, self.rh.ask = 0.50, 0.51     # +51%: half
        self.advance(60)
        self.sweep()
        self.assertEqual(self.snapshot()["positions"][0]["qty_remaining"], 8)
        self.rh.bid, self.rh.ask = 1.32, 1.33     # 4x: +200% then +300% rungs
        self.advance(60)
        self.sweep()
        self.assertEqual([s["quantity"] for s in self.rh.placed[1:]], ["7", "4", "4"])
        self.assertEqual(self.snapshot()["positions"][0]["qty_remaining"], 0)

    def test_unfilled_stop_is_repriced_to_the_falling_bid(self):
        self.fire()
        self.rh.place_state = "confirmed"
        self.rh.bid, self.rh.ask = 0.12, 0.13
        self.advance(60)
        self.sweep()
        self.rh.bid, self.rh.ask = 0.03, 0.04
        self.advance(60)
        self.sweep()
        self.assertEqual([(o["price"], o["state"]) for o in self.sells()],
                         [("0.03000000", "confirmed"), ("0.12000000", "cancelled")])
        self.assertIsNone(self.snapshot()["halt_reason"])
        self.advance(60)
        self.sweep()        # bid unchanged: the resting order keeps its place
        self.assertEqual(len(self.sells()), 2)
        self.rh.fill(self.sells()[0]["id"])
        self.advance(60)
        self.sweep()
        self.assertEqual(self.snapshot()["positions"][0]["qty_remaining"], 0)

    def test_manual_close_is_recorded_and_trading_continues(self):
        self.fire()
        self.rh.manual_close_all()
        self.advance(60)
        self.sweep()
        data = self.snapshot()
        self.assertEqual(data["positions"][0]["status"], "closed")
        self.assertEqual([e["qty"] for e in data["events"] if e["event"] == "external_close"], [15])
        self.advance(60)
        self.fire()
        self.assertEqual(len(self.rh.placed), 2)

    def test_manual_trim_leaves_the_rest_managed(self):
        self.fire()
        self.rh.positions[frh.OPTION_ID] = 5
        self.rh.bid, self.rh.ask = 0.12, 0.13
        self.advance(60)
        self.sweep()
        self.assertEqual(self.rh.placed[-1]["quantity"], "5")
        self.assertEqual(self.snapshot()["positions"][0]["qty_remaining"], 0)

    def test_holdings_the_bot_never_bought_still_block(self):
        self.fire()
        self.rh.positions[frh.OPTION_ID] = 20
        self.rh.bid, self.rh.ask = 0.12, 0.13
        self.advance(60)
        rc, out = self.sweep()
        self.assertIn("needs review", out)
        self.assertEqual(len(self.rh.placed), 1)


if __name__ == "__main__":
    unittest.main()
