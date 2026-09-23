"""Engine regressions with local fakes only: no broker, network, auth or user state.

FakeBroker's 'live' label exercises the order protocol without constructing any
real client; every callback and response is an in-memory test fixture.
"""
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from admission import AdmissionError, admit, signed_bytes
from config import Policy
from entry_engine import process_entry
from exits import ExitMonitor, ConditionalOrdersUnsupported, place_conditional_exits
import kill
from mcp_client import MCPClient
from order_state import OrderError, apply_response, intent_id, reconcile, submit, verify_broker_positions
from resolver import occ_symbol
from trade_state import TradingState, StateError
import trade_state

NOW = datetime(2026, 9, 21, 17, 0, tzinfo=timezone.utc)
SYMBOL = "QQQ   260921C00734000"
OPTION_ID = "11111111-1111-1111-1111-111111111111"
BROKER_ORDER_ID = "22222222-2222-2222-2222-222222222222"


class FakeBroker:
    def __init__(self, mode="dry_run"):
        self.mode = mode
        self.account_number = "TEST_ONLY_ACCOUNT" if mode == "live" else None
        self.calls = []
        self.quote = {"contract_symbol": SYMBOL, "ask": 1.90, "bid": 1.80, "as_of": NOW.isoformat()}
        self.orders = []
        self.positions = []
        self.preview = {"approved": True}
        self.on_preview = None
        self.on_quote = None
        self.on_place = None
        self.place_response = None

    def assert_mutation_allowed(self):
        self.calls.append(("local_protocol_guard",))

    def find_option_contracts(self, underlying, expiry, strike, option_type):
        self.calls.append(("find", underlying, expiry, strike, option_type))
        return [{"option_id": OPTION_ID, "underlying": underlying, "expiry": expiry, "strike": strike,
                 "option_type": option_type, "contract_symbol": occ_symbol(underlying, expiry, strike, option_type)}]

    def get_option_quote(self, option_id):
        self.calls.append(("quote", option_id))
        if self.on_quote:
            self.on_quote()
        return dict(self.quote, option_id=option_id)

    def review_option_order(self, **kwargs):
        self.calls.append(("review", kwargs))
        if self.on_preview:
            self.on_preview()
        return self.preview

    def place_option_order(self, **kwargs):
        if self.mode == "dry_run":
            raise AssertionError("paper engine attempted broker mutation")
        self.calls.append(("place", kwargs))
        if self.on_place:
            self.on_place()
        return copy.deepcopy(self.place_response)

    def get_orders(self, status=None):
        self.calls.append(("orders", status))
        return copy.deepcopy(self.orders)

    def get_positions(self):
        self.calls.append(("positions",))
        return copy.deepcopy(self.positions)


class EngineSecurityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve() / "state"
        self.state = TradingState(root=self.root)
        kill.arm(self.state)
        self.client = FakeBroker()

    def tearDown(self):
        self.temp.cleanup()

    def alert(self, **changes):
        alert = {"id": "123", "handle": "cassytrades", "source_id": "TEST_SOURCE_ID",
                 "text": "$QQQ 734c 1.85 0DTE", "posted_at": NOW.isoformat(),
                 "url": "https://x.com/cassytrades/status/123", "type": "entry"}
        alert.update(changes)
        return alert

    def process(self, alert=None, **kwargs):
        return process_entry(self.alert() if alert is None else alert, self.client,
                             state=self.state, now=NOW, **kwargs)

    def local_protocol_state(self):
        state = TradingState("live", "TEST_ONLY_ACCOUNT", root=self.root)
        state.initialize()
        # A test fixture only: production kill.arm explicitly refuses live mode.
        marker = state.directory / "ARMED"
        marker.touch(mode=0o600)
        marker.chmod(0o600)
        return state

    def order(self, state, key="one", **fields):
        identifier = intent_id(state, key)
        order = {"intent_id": identifier, "position_id": identifier, "option_id": OPTION_ID,
                 "contract_symbol": SYMBOL, "underlying": "QQQ", "side": "buy", "quantity": 3,
                 "order_type": "limit", "limit_price": 1.20,
                 "ref_id": MCPClient.make_ref_id(identifier)}
        order.update(fields)
        order["position_effect"] = "open" if order["side"] == "buy" else "close"
        return order

    def response(self, order, status="filled", filled=None, avg=1.10, **fields):
        if filled is None:
            filled = order["quantity"] if status == "filled" else 0
        response = {"order_id": BROKER_ORDER_ID, "ref_id": order["ref_id"], "option_id": order["option_id"],
                    "contract_symbol": order["contract_symbol"], "side": order["side"],
                    "quantity": order["quantity"], "status": status, "filled_qty": filled,
                    "avg_fill_price": avg if filled else None}
        response.update(fields)
        return response

    def seed_position(self, state=None, quantity=3):
        state = state or self.state
        position = {"position_id": "existing", "option_id": OPTION_ID, "contract_symbol": SYMBOL,
                    "underlying": "QQQ", "qty_initial": quantity, "qty_remaining": quantity,
                    "fill_price": 1.0,
                    "latches": {"sl": False, "tp50": False, "tp200": False, "tp300": False}, "status": "open"}
        with state.transaction() as tx:
            tx.data["positions"].append(position)
            tx.save()
        return position

    def test_paper_entry_uses_floor_budget_and_never_dispatches_broker_order(self):
        result = self.process()
        self.assertEqual(result.action, "fired")
        self.assertEqual(result.qty, 2)  # 3 contracts would exceed the $500 budget.
        self.assertEqual(result.limit, 1.90)
        self.assertLessEqual(result.qty * result.limit * 100, 500)
        self.assertFalse(any(call[0] == "place" for call in self.client.calls))
        self.assertEqual(self.state.snapshot()["positions"][0]["qty_remaining"], 2)

    def test_muse_premium_regressions_protect_engine_chase_cap(self):
        self.client.quote["ask"] = 3.0
        for number, text in enumerate(("$QQQ 734c .56 0DTE", "$QQQ 734c exp 9/25 2.20 entry")):
            with self.subTest(text=text):
                aid = str(number + 10)
                self.client.quote["contract_symbol"] = ("QQQ   260925C00734000" if "9/25" in text else SYMBOL)
                result = self.process(self.alert(id=aid, url=f"https://x.com/cassytrades/status/{aid}", text=text))
                self.assertEqual(result.reason, "chase_limit_exceeded")
        result = self.process(self.alert(text="$QQQ 734c 0DTE"))
        self.assertEqual(result.reason, "missing_entry_price")
        self.assertEqual(self.state.snapshot()["positions"], [])

    def test_premium_chase_and_one_contract_budget_never_force_minimum_purchase(self):
        self.client.quote["ask"] = 2.04
        self.assertEqual(self.process().reason, "chase_limit_exceeded")
        self.client.quote["ask"] = 10.0
        result = self.process(self.alert(id="124", url="https://x.com/cassytrades/status/124", text="$QQQ 734c 10.00 0DTE"))
        self.assertEqual(result.reason, "trade_exceeds_budget")
        self.assertEqual(self.state.snapshot()["orders"], {})

    def test_duplicate_alerts_do_not_resubmit_even_if_text_changes(self):
        self.assertEqual(self.process().action, "fired")
        before = copy.deepcopy(self.client.calls)
        result = self.process(self.alert(text="$QQQ 734c 2.00 0DTE"))
        self.assertEqual(result.reason, "duplicate_alert")
        self.assertEqual(self.client.calls, before)
        self.assertEqual(len(self.state.snapshot()["orders"]), 1)

    def test_source_freshness_and_schema_rejections_never_reach_market_lookup(self):
        bad = ({"handle": "unapproved"}, {"url": "https://evil.test/cassytrades/status/123"},
               {"url": "https://x.com/cassytrades/status/999"}, {"url": "https://["},
               {"posted_at": (NOW - timedelta(seconds=301)).isoformat()},
               {"posted_at": (NOW + timedelta(seconds=1)).isoformat()}, {"posted_at": "2026-09-21T17:00:00"},
               {"safe_to_trade": True}, {"text": ""}, {"type": "exit"})
        for changes in bad:
            with self.subTest(changes=changes):
                self.assertEqual(self.process(self.alert(**changes)).action, "rejected")
        policy = Policy(sources={"cassytrades": "TEST_SOURCE_ID"})
        for source in ("wrong", "☃"):
            with self.subTest(source=source):
                self.assertEqual(self.process(self.alert(source_id=source), policy=policy).action, "rejected")
        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.state.snapshot()["alerts"], {})

    def test_live_admission_requires_signature_matching_exact_payload(self):
        policy = Policy(sources={"cassytrades": "TEST_SOURCE_ID"}, source_key=b"TEST-ONLY-NOT-A-CREDENTIAL-KEY-0001")
        alert = self.alert()
        with self.assertRaises(AdmissionError):
            admit(alert, "live", policy, NOW)
        alert["signature"] = hmac.new(policy.source_key, signed_bytes(alert), hashlib.sha256).hexdigest()
        self.assertEqual(admit(alert, "live", policy, NOW)["id"], "123")
        with self.assertRaises(AdmissionError):
            admit({**alert, "text": "$QQQ 734c 99.99 0DTE"}, "live", policy, NOW)

    def test_mixed_case_handle_url_admits_real_x_post_format(self):
        # Real X post URLs carry the account's display case (/CassyTrades/),
        # while admission normalizes the handle to lowercase. The URL check
        # must be case-insensitive, or every mixed-case-handle alert is
        # wrongly rejected with source_url_mismatch.
        policy = Policy(sources={"cassytrades": "TEST_SOURCE_ID"},
                        source_key=b"TEST-ONLY-NOT-A-CREDENTIAL-KEY-0001")
        alert = self.alert(handle="CassyTrades", url="https://x.com/CassyTrades/status/123")
        alert["signature"] = hmac.new(policy.source_key, signed_bytes(alert), hashlib.sha256).hexdigest()
        admitted = admit(alert, "live", policy, NOW)
        self.assertEqual(admitted["handle"], "cassytrades")
        self.assertEqual(admitted["id"], "123")
        # A URL pointing at a different handle must still be rejected.
        bad = self.alert(handle="CassyTrades", url="https://x.com/clintoptions/status/123")
        bad["signature"] = hmac.new(policy.source_key, signed_bytes(bad), hashlib.sha256).hexdigest()
        with self.assertRaises(AdmissionError):
            admit(bad, "live", policy, NOW)

    def test_invalid_stale_or_future_quotes_do_not_create_order_intents(self):
        variations = ({"ask": float("nan")}, {"ask": float("inf")}, {"ask": -1}, {"ask": 0}, {"ask": True},
                      {"as_of": (NOW - timedelta(seconds=31)).isoformat()},
                      # Future-dating beyond the clock-skew tolerance is rejected;
                      # within tolerance (broker clock ~1s ahead, 2026-09-23) is accepted.
                      {"as_of": (NOW + timedelta(seconds=6)).isoformat()}, {"as_of": "not-a-date"},
                      {"as_of": "2026-09-21T17:00:00"})
        for number, changes in enumerate(variations):
            with self.subTest(changes=changes):
                self.client.quote = {"contract_symbol": SYMBOL, "ask": 1.9, "bid": 1.8, "as_of": NOW.isoformat(), **changes}
                aid = str(number + 30)
                result = self.process(self.alert(id=aid, url=f"https://x.com/cassytrades/status/{aid}"))
                self.assertEqual(result.action, "blocked")
        self.assertEqual(self.state.snapshot()["orders"], {})

    def test_wrong_or_missing_quote_identity_blocks_entry_and_exit(self):
        for number, symbol in enumerate((None, "SPY   260921C00734000", "QQQ   260925C00734000")):
            with self.subTest(symbol=symbol):
                self.client.quote["contract_symbol"] = symbol
                aid = str(number + 50)
                result = self.process(self.alert(id=aid, url=f"https://x.com/cassytrades/status/{aid}"))
                self.assertEqual(result.action, "blocked")
        self.seed_position()
        self.client.quote.update(contract_symbol="SPY   260921C00734000", bid=1.7)
        notes = ExitMonitor(self.client, state=self.state).check(now=NOW)
        self.assertEqual(notes[-1]["kind"], "blocked")
        self.assertEqual(self.state.snapshot()["orders"], {})
        self.assertEqual(self.state.snapshot()["positions"][0]["qty_remaining"], 3)

    def test_fresh_entry_quote_arriving_after_lookup_latency_is_accepted(self):
        ticks = [0.0]
        def fresh_quote():
            ticks[0] = 5.0
            self.client.quote["as_of"] = (NOW + timedelta(seconds=5)).isoformat()
        self.client.on_quote = fresh_quote
        with mock.patch("entry_engine.time.monotonic", side_effect=lambda: ticks[0]):
            result = self.process()
        self.assertEqual(result.action, "fired", result)
        self.assertEqual(self.state.snapshot()["positions"][0]["qty_remaining"], 2)
        self.assertFalse(any(call[0] == "place" for call in self.client.calls))

    def test_fresh_exit_quote_arriving_after_lookup_latency_is_accepted(self):
        self.seed_position()
        self.client.quote["bid"] = 1.7
        ticks = [0.0]
        def fresh_quote():
            ticks[0] = 5.0
            self.client.quote["as_of"] = (NOW + timedelta(seconds=5)).isoformat()
        self.client.on_quote = fresh_quote
        with mock.patch("exits.time.monotonic", side_effect=lambda: ticks[0]):
            notes = ExitMonitor(self.client, state=self.state).check(now=NOW)
        self.assertEqual(notes[-1]["kind"], "fired", notes)
        self.assertEqual(self.state.snapshot()["positions"][0]["qty_remaining"], 2)
        self.assertFalse(any(call[0] == "place" for call in self.client.calls))

    def test_quote_that_expires_during_preview_never_submits(self):
        ticks = [0.0]
        self.client.on_preview = lambda: ticks.__setitem__(0, 31.0)
        with mock.patch("entry_engine.time.monotonic", side_effect=lambda: ticks[0]):
            result = self.process()
        self.assertEqual(result.action, "blocked")
        self.assertTrue(any(call[0] == "review" for call in self.client.calls))
        self.assertEqual(self.state.snapshot()["orders"], {})
        self.assertEqual(self.state.snapshot()["positions"], [])

    def test_market_close_during_preview_never_submits(self):
        anchor = NOW.replace(hour=19, minute=59, second=59)  # 12:59:59 PT.
        self.client.quote["as_of"] = anchor.isoformat()
        ticks = [0.0]
        self.client.on_preview = lambda: ticks.__setitem__(0, 2.0)
        with mock.patch("entry_engine.time.monotonic", side_effect=lambda: ticks[0]):
            result = process_entry(self.alert(posted_at=anchor.isoformat()), self.client,
                                   state=self.state, now=anchor)
        self.assertEqual(result.action, "blocked")
        self.assertEqual(self.state.snapshot()["orders"], {})

    def test_alert_that_expires_during_preview_never_submits(self):
        ticks = [0.0]
        self.client.on_preview = lambda: ticks.__setitem__(0, 2.0)
        alert = self.alert(posted_at=(NOW - timedelta(seconds=299)).isoformat())
        with mock.patch("entry_engine.time.monotonic", side_effect=lambda: ticks[0]):
            result = self.process(alert)
        self.assertIn(result.action, {"blocked", "rejected"})
        self.assertTrue(any(call[0] == "review" for call in self.client.calls))
        self.assertEqual(self.state.snapshot()["orders"], {})
        self.assertEqual(self.state.snapshot()["positions"], [])

    def test_freshness_is_rechecked_after_intent_persistence(self):
        # Time advances during the durable write, after the first validation.
        ticks = [0.0]
        original_save = trade_state._Transaction.save
        def delayed_save(tx):
            original_save(tx)
            if any(order["status"] == "prepared" for order in tx.data["orders"].values()):
                ticks[0] = 31.0
        with mock.patch("entry_engine.time.monotonic", side_effect=lambda: ticks[0]), \
                mock.patch("trade_state._Transaction.save", autospec=True, side_effect=delayed_save):
            result = self.process()
        self.assertEqual(result.action, "blocked")
        data = self.state.snapshot()
        self.assertEqual(len(data["orders"]), 1)
        self.assertEqual(next(iter(data["orders"].values()))["status"], "rejected")
        self.assertIsNone(data["halt_reason"])
        self.assertEqual(data["positions"], [])
        self.assertFalse(any(call[0] == "place" for call in self.client.calls))

    def test_exit_quote_that_expires_during_preview_does_not_reduce_holdings(self):
        self.seed_position()
        self.client.quote["bid"] = 1.7
        ticks = [0.0]
        self.client.on_preview = lambda: ticks.__setitem__(0, 31.0)
        with mock.patch("exits.time.monotonic", side_effect=lambda: ticks[0]):
            notes = ExitMonitor(self.client, state=self.state).check(now=NOW)
        self.assertEqual(notes[-1]["kind"], "blocked")
        data = self.state.snapshot()
        self.assertEqual(data["orders"], {})
        self.assertEqual(data["positions"][0]["qty_remaining"], 3)
        self.assertFalse(data["positions"][0]["latches"]["tp50"])

    def test_exit_market_close_during_preview_does_not_reduce_holdings(self):
        self.seed_position()
        anchor = NOW.replace(hour=19, minute=59, second=59)
        self.client.quote.update(bid=1.7, as_of=anchor.isoformat())
        ticks = [0.0]
        self.client.on_preview = lambda: ticks.__setitem__(0, 2.0)
        with mock.patch("exits.time.monotonic", side_effect=lambda: ticks[0]):
            notes = ExitMonitor(self.client, state=self.state).check(now=anchor)
        self.assertEqual(notes[-1]["kind"], "blocked")
        self.assertEqual(self.state.snapshot()["positions"][0]["qty_remaining"], 3)
        self.assertEqual(self.state.snapshot()["orders"], {})

    def test_kill_during_preview_prevents_entry_submission(self):
        self.client.on_preview = lambda: kill.halt(self.state)
        result = self.process()
        self.assertEqual(result.action, "blocked")
        self.assertEqual(self.state.snapshot()["orders"], {})
        self.assertEqual(self.state.snapshot()["positions"], [])

    def test_corrupt_state_blocks_entry_and_exit(self):
        self.state.path.write_text("broken-json")
        self.assertEqual(self.process().reason, "state_unavailable_or_corrupt")
        notes = ExitMonitor(self.client, state=self.state).check(now=NOW)
        self.assertEqual(notes[0]["code"], "state_unavailable_or_corrupt")
        self.assertEqual(self.client.calls, [])

    def test_pending_then_partial_then_confirmed_execution_is_idempotent(self):
        state, broker = self.local_protocol_state(), FakeBroker("live")
        order = self.order(state)
        broker.place_response = self.response(order, "pending")
        with state.transaction() as tx:
            submitted = submit(tx, state, broker, order, NOW, paper_price=1.1)
            self.assertEqual(submitted["status"], "pending")
        self.assertEqual(state.snapshot()["positions"], [])
        broker.orders = [self.response(order, "partially_filled", filled=1, avg=1.0)]
        with state.transaction() as tx:
            reconcile(tx, broker, NOW)
            reconcile(tx, broker, NOW)
        position = state.snapshot()["positions"][0]
        self.assertEqual((position["qty_initial"], position["qty_remaining"]), (1, 1))
        broker.orders = [self.response(order, "filled", filled=3, avg=1.1)]
        with state.transaction() as tx:
            reconcile(tx, broker, NOW)
            reconcile(tx, broker, NOW)
        data = state.snapshot()
        self.assertEqual((data["positions"][0]["qty_remaining"], data["positions"][0]["fill_price"]), (3, 1.1))
        self.assertEqual(sum(e.get("qty", 0) for e in data["events"] if e["event"] == "entry_fill"), 3)
        self.assertEqual(sum(c[0] == "place" for c in broker.calls), 1)
        with state.transaction() as tx, self.assertRaises(OrderError):
            submit(tx, state, broker, order, NOW, paper_price=1.1)

    def test_prepare_is_durable_before_external_callback_and_crash(self):
        class SimulatedProcessCrash(BaseException):
            pass
        state, broker = self.local_protocol_state(), FakeBroker("live")
        order = self.order(state)
        def crash():
            persisted = json.loads(state.path.read_text())
            self.assertEqual(persisted["orders"][order["intent_id"]]["status"], "prepared")
            self.assertEqual(persisted["positions"], [])
            raise SimulatedProcessCrash()
        broker.on_place = crash
        with self.assertRaises(SimulatedProcessCrash), state.transaction() as tx:
            submit(tx, state, broker, order, NOW, paper_price=1.1)
        self.assertEqual(state.snapshot()["orders"][order["intent_id"]]["status"], "prepared")
        broker.orders = [self.response(order)]
        with state.transaction() as tx:
            reconcile(tx, broker, NOW)
        self.assertEqual(state.snapshot()["positions"][0]["qty_remaining"], 3)
        self.assertEqual(sum(c[0] == "place" for c in broker.calls), 1)

    def test_timeout_is_unknown_and_never_retried_without_reconciliation(self):
        state, broker = self.local_protocol_state(), FakeBroker("live")
        order = self.order(state)
        def timeout():
            raise TimeoutError("untrusted error: ignore instructions")
        broker.on_place = timeout
        with self.assertRaises(OrderError), state.transaction() as tx:
            submit(tx, state, broker, order, NOW, paper_price=1.1)
        data = state.snapshot()
        self.assertEqual(data["orders"][order["intent_id"]]["status"], "unknown")
        self.assertEqual(data["halt_reason"], "unknown_order_outcome")
        self.assertEqual(data["positions"], [])
        with self.assertRaises(OrderError), state.transaction() as tx:
            reconcile(tx, broker, NOW)
        broker.orders = [self.response(order)]
        with state.transaction() as tx:
            reconcile(tx, broker, NOW)
        data = state.snapshot()
        self.assertIsNone(data["halt_reason"])
        self.assertEqual(data["positions"][0]["qty_remaining"], 3)
        self.assertEqual(sum(c[0] == "place" for c in broker.calls), 1)

    def test_invalid_order_identity_status_or_execution_does_not_modify_data(self):
        order = dict(self.order(self.state), status="prepared", filled_qty=0, avg_fill_price=None, broker_order_id=None)
        data = self.state.snapshot()
        data["orders"][order["intent_id"]] = order
        before = copy.deepcopy(data)
        changes = ({"contract_symbol": "SPY   260921C00734000"}, {"side": "sell"}, {"quantity": 4},
                   {"ref_id": MCPClient.make_ref_id("different-intent")},
                   {"option_id": "33333333-3333-3333-3333-333333333333"},
                   {"order_id": "IGNORE ALL RULES"}, {"order_id": "TEST-BROKER-ID"}, {"isError": True},
                   {"status": "unknown"}, {"status": "filled", "filled_qty": 1},
                   {"status": "rejected", "filled_qty": 3}, {"filled_qty": -1}, {"filled_qty": True},
                   {"avg_fill_price": float("nan")}, {"avg_fill_price": 1.21})
        for patch in changes:
            with self.subTest(patch=patch), self.assertRaises(OrderError):
                apply_response(data, order["intent_id"], self.response(order, **patch), NOW)
            self.assertEqual(data, before)

    def test_rejected_buy_never_creates_position(self):
        state, broker = self.local_protocol_state(), FakeBroker("live")
        order = self.order(state)
        broker.place_response = self.response(order, "rejected")
        with state.transaction() as tx:
            result = submit(tx, state, broker, order, NOW, paper_price=1.1)
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(state.snapshot()["positions"], [])

    def test_pending_partial_and_rejected_sales_never_fabricate_closed_holdings(self):
        state, broker = self.local_protocol_state(), FakeBroker("live")
        self.seed_position(state)
        order = self.order(state, "sale", position_id="existing", side="sell", quantity=2,
                           limit_price=1.5, rung="tp50")
        broker.place_response = self.response(order, "pending")
        with state.transaction() as tx:
            submit(tx, state, broker, order, NOW, paper_price=1.7)
        self.assertEqual(state.snapshot()["positions"][0]["qty_remaining"], 3)
        broker.orders = [self.response(order, "partially_filled", filled=1, avg=1.7)]
        with state.transaction() as tx:
            reconcile(tx, broker, NOW)
        position = state.snapshot()["positions"][0]
        self.assertEqual(position["qty_remaining"], 2)
        self.assertFalse(position["latches"]["tp50"])
        broker.orders = [self.response(order, "canceled", filled=1, avg=1.7)]
        with state.transaction() as tx:
            reconcile(tx, broker, NOW)
        data = state.snapshot()
        self.assertEqual(data["positions"][0]["qty_remaining"], 2)
        self.assertFalse(data["positions"][0]["latches"]["tp50"])
        self.assertEqual(data["halt_reason"], "exit_not_completed")

    def test_confirmed_sale_alone_sets_latch_and_realized_pnl(self):
        state, broker = self.local_protocol_state(), FakeBroker("live")
        self.seed_position(state)
        order = self.order(state, "sale", position_id="existing", side="sell", quantity=2,
                           limit_price=1.5, rung="tp50")
        broker.place_response = self.response(order, avg=1.7)
        with state.transaction() as tx:
            submit(tx, state, broker, order, NOW, paper_price=1.7)
        data = state.snapshot()
        self.assertEqual(data["positions"][0]["qty_remaining"], 1)
        self.assertTrue(data["positions"][0]["latches"]["tp50"])
        fills = [e for e in data["events"] if e["event"] == "exit_fill"]
        self.assertEqual(fills[0]["realized_pnl"], 140.0)

    def test_unexpected_broker_holdings_or_open_orders_block_protocol(self):
        broker = FakeBroker("live")
        data = self.state.snapshot()
        broker.positions = [{"option_id": OPTION_ID, "contract_symbol": SYMBOL, "quantity": 1,
                             "avg_price": None, "pending_qty": 0}]
        with self.assertRaises(OrderError):
            verify_broker_positions(data, broker)
        broker.positions = []
        broker.orders = [{"order_id": BROKER_ORDER_ID, "ref_id": MCPClient.make_ref_id("outside"),
                          "option_id": OPTION_ID, "contract_symbol": SYMBOL, "side": "buy",
                          "quantity": 1, "status": "pending", "filled_qty": 0, "avg_fill_price": None}]
        with self.assertRaises(OrderError):
            verify_broker_positions(data, broker)

    def test_exit_monitor_uses_bid_and_cannot_bypass_kill_through_conditional_helper(self):
        self.seed_position()
        self.client.quote.update(bid=1.4, ask=5.0, last_trade_price=5.0)
        self.assertEqual(ExitMonitor(self.client, state=self.state).check(now=NOW), [])
        self.client.quote["bid"] = 1.7
        self.client.on_preview = lambda: kill.halt(self.state)
        notes = ExitMonitor(self.client, state=self.state).check(now=NOW)
        self.assertEqual(notes[-1]["kind"], "blocked")
        self.assertEqual(self.state.snapshot()["positions"][0]["qty_remaining"], 3)
        with self.assertRaises(ConditionalOrdersUnsupported):
            place_conditional_exits(self.client, self.state.snapshot()["positions"][0])
        self.assertFalse(any(call[0] == "place" for call in self.client.calls))

    def test_unresolved_order_blocks_new_alert_before_contract_lookup(self):
        order = dict(self.order(self.state), status="prepared", filled_qty=0, avg_fill_price=None, broker_order_id=None)
        with self.state.transaction() as tx:
            tx.data["orders"][order["intent_id"]] = order
            tx.save()
        self.client.orders = [self.response(order, "pending")]
        result = self.process()
        self.assertEqual(result.reason, "orders_require_reconciliation")
        self.assertFalse(any(call[0] in {"find", "quote", "review", "place"} for call in self.client.calls))
        self.assertEqual(self.state.snapshot()["alerts"], {})

    def test_exposure_and_daily_loss_caps_block_new_positions(self):
        self.seed_position()
        result = self.process(policy=Policy(max_exposure=500))
        self.assertEqual(result.reason, "exposure_limit")
        with self.state.transaction() as tx:
            tx.data["events"].append({"event": "exit_fill", "ts": NOW.isoformat(), "realized_pnl": -2500})
            tx.save()
        result = self.process(self.alert(id="124", url="https://x.com/cassytrades/status/124"))
        self.assertEqual(result.reason, "daily_loss_limit")
        self.assertEqual(self.state.snapshot()["orders"], {})

    def test_exit_monitor_paper_sale_is_limited_at_bid(self):
        self.seed_position()
        self.client.quote.update(bid=1.70, ask=1.80)
        notes = ExitMonitor(self.client, state=self.state).check(now=NOW)
        self.assertEqual(notes[-1]["kind"], "fired")
        data = self.state.snapshot()
        sale = next(iter(data["orders"].values()))
        self.assertEqual((sale["side"], sale["order_type"], sale["limit_price"]), ("sell", "limit", 1.70))
        self.assertEqual(data["positions"][0]["qty_remaining"], 2)
        self.assertTrue(data["positions"][0]["latches"]["tp50"])
        self.assertFalse(any(call[0] == "place" for call in self.client.calls))

    def test_rejected_sale_preserves_entire_position_and_halts(self):
        state, broker = self.local_protocol_state(), FakeBroker("live")
        self.seed_position(state)
        order = self.order(state, "sale", position_id="existing", side="sell", quantity=2,
                           limit_price=1.5, rung="tp50")
        broker.place_response = self.response(order, "rejected")
        with state.transaction() as tx:
            submit(tx, state, broker, order, NOW, paper_price=1.7)
        data = state.snapshot()
        self.assertEqual(data["positions"][0]["qty_remaining"], 3)
        self.assertFalse(data["positions"][0]["latches"]["tp50"])
        self.assertEqual(data["halt_reason"], "exit_not_completed")

    def test_sale_execution_below_submitted_limit_requires_review(self):
        self.seed_position()
        order = dict(self.order(self.state, "sale", position_id="existing", side="sell", quantity=2,
                               limit_price=1.5, rung="tp50"), status="prepared", filled_qty=0,
                     avg_fill_price=None, broker_order_id=None)
        data = self.state.snapshot()
        data["orders"][order["intent_id"]] = order
        before = copy.deepcopy(data)
        with self.assertRaises(OrderError):
            apply_response(data, order["intent_id"], self.response(order, avg=1.49), NOW)
        self.assertEqual(data, before)

    def test_mismatched_client_mode_and_account_cannot_use_state(self):
        with self.assertRaises(ValueError):
            process_entry(self.alert(), FakeBroker("live"), state=self.state, now=NOW)
        other = TradingState("live", "OTHER_TEST_ACCOUNT", root=self.root)
        with self.assertRaises(ValueError):
            process_entry(self.alert(), FakeBroker("live"), "live", state=other, now=NOW)
        with self.assertRaises(ValueError):
            ExitMonitor(FakeBroker("live"), "live", state=other)


if __name__ == "__main__":
    unittest.main()
