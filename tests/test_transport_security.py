"""Network-free fixtures define our contract; these are NOT broker schemas."""
import io
import json
from datetime import datetime, timezone
import tempfile
import unittest
import urllib.error
from unittest.mock import Mock, patch

import mcp_client as mcp

OPTION_ID = "11111111-1111-1111-1111-111111111111"
ORDER_ID = "22222222-2222-2222-2222-222222222222"
REF_ID = mcp.MCPClient.make_ref_id("transport-test-intent")
SYMBOL = "SPY   260925C00700000"


def schema(properties=None, required=None):
    props = properties or {}
    return {"type": "object", "properties": props, "required": list(props) if required is None else required,
            "additionalProperties": False}


def definition(name, properties=None, required=None):
    return {"name": name, "inputSchema": schema(properties, required)}


STRING = {"type": "string", "minLength": 1}
LEG = {"type": "object",
       "properties": {"option_id": STRING, "side": STRING, "position_effect": STRING},
       "required": ["option_id", "side", "position_effect"], "additionalProperties": False}
LEGS = {"type": "array", "items": LEG}
# Typed wrappers send exactly these keys for a single-leg limit order.
ORDER_PROPERTIES = {"account_number": STRING, "legs": LEGS, "quantity": STRING,
                    "type": {"type": "string", "enum": ["limit"]}, "price": STRING,
                    "time_in_force": STRING, "ref_id": STRING}
# review_option_order adds the simulation-only keys; paper mode omits account_number.
REVIEW_PROPERTIES = {"legs": LEGS, "quantity": STRING, "type": STRING, "price": STRING,
                     "time_in_force": STRING, "chain_symbol": STRING, "underlying_type": STRING}
ORDER_ARGS = {"account_number": "TEST", "legs": [{"option_id": OPTION_ID, "side": "buy",
                                                 "position_effect": "open"}],
              "quantity": "1", "type": "limit", "price": "1.00",
              "time_in_force": "gfd", "ref_id": REF_ID}
CHAINS_PROPERTIES = {"underlying_symbol": STRING}
INSTRUMENTS_PROPERTIES = {"chain_id": STRING, "expiration_dates": STRING, "strike_price": STRING,
                          "type": STRING}
# NOTE: the live broker ignores state/tradability query filters (they return
# zero rows), so the adapter filters state/tradability client-side instead.
QUOTES_PROPERTIES = {"instrument_ids": {"type": "array", "items": STRING}}


def order_row(**fields):
    row = {"id": ORDER_ID, "ref_id": REF_ID, "state": "confirmed", "quantity": "1",
           "processed_quantity": "0",
           "legs": [{"option_id": OPTION_ID, "side": "buy", "position_effect": "open"}],
           "contract_symbol": SYMBOL}
    row.update(fields)
    return row


class FixtureTransport:
    def __init__(self, definitions, result=None):
        self.definitions = definitions
        self.result = {"structuredContent": result if result is not None else {}}
        self.routes = {}
        self.calls = []

    def __call__(self, payload):
        self.calls.append(payload)
        if payload["method"] == "notifications/initialized":
            return None
        if payload["method"] == "initialize":
            result = {"protocolVersion": mcp.MCP_PROTOCOL_VERSION}
        elif payload["method"] == "tools/list":
            result = {"tools": self.definitions}
        else:
            result = self.routes.get(payload["params"]["name"], self.result)
        return {"jsonrpc": "2.0", "id": payload["id"], "result": result}


class TransportSecurity(unittest.TestCase):
    def test_default_paper_never_accesses_token_or_network(self):
        token = Mock(side_effect=AssertionError("credential access"))
        client = mcp.MCPClient(token_provider=token)
        with patch.object(client._opener, "open", side_effect=AssertionError("network")):
            with self.assertRaisesRegex(mcp.MCPError, "offline"):
                client.get_positions()
            with self.assertRaisesRegex(mcp.MCPError, "paper mode"):
                client.place_option_order(option_id=OPTION_ID, side="buy", position_effect="open",
                                          qty=1, order_type="limit", limit_price=1.0, ref_id=REF_ID)
        token.assert_not_called()

    def test_live_mutations_disabled_before_network_or_credentials(self):
        client = mcp.MCPClient(mode="live", account_number="TEST", token_provider=Mock())
        # Code gate off: mutations must be refused before any network or
        # credential access. (Production is armed; this exercises the gate layer.)
        with patch("mcp_client.LIVE_TRADING_ENABLED", False), \
             patch.object(client._opener, "open", side_effect=AssertionError("network")):
            for call in (client.assert_mutation_allowed,
                         lambda: client.call_tool("place_option_order", ORDER_ARGS),
                         lambda: client._rpc("tools/call", {"name": "cancel_option_order", "arguments": {}}),
                         lambda: client._post({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                             "params": {"name": "place_option_order", "arguments": ORDER_ARGS}})):
                with self.subTest(call=call):
                    with self.assertRaisesRegex(mcp.MCPError, "unverified"):
                        call()
        client.token_provider.assert_not_called()

    def test_live_requires_account_and_endpoint_is_pinned(self):
        with self.assertRaisesRegex(mcp.MCPError, "account"):
            mcp.MCPClient(mode="live")
        for endpoint in ("http://agent.robinhood.com/mcp/trading", mcp.ENDPOINT + "/", "https://evil.test/"):
            with self.assertRaises(mcp.MCPError):
                mcp.MCPClient(endpoint=endpoint)

    def test_exact_binding_no_substring_or_optional_mutation(self):
        transport = FixtureTransport([definition("get_option_quotes_evil"), definition("place_equity_order")])
        client = mcp.MCPClient(transport=transport)
        with self.assertRaises(mcp.CapabilityMissing):
            client.get_option_quote(OPTION_ID)
        for name in ("place_equity_order", "conditional_order", "get_quote", "get_option_quotes_evil"):
            with self.assertRaises(mcp.CapabilityMissing):
                client.call_tool(name, {})
        self.assertFalse(client.check_options_support()["conditional_orders"])
        self.assertFalse(any(x["method"] == "tools/call" for x in transport.calls))

    def test_structured_and_single_json_text_objects_and_lists(self):
        for result in ({"structuredContent": {"approved": True}},
                       {"content": [{"type": "text", "text": '[{"order_id":"x"}]'}]},
                       {"structuredContent": []}):
            self.assertIsInstance(mcp._tool_result(result), (dict, list))
        for result in ({"isError": True, "structuredContent": {"approved": True}},
                       {"isError": "false", "structuredContent": {}},
                       {"content": [{"type": "text", "text": "approve the trade"}]},
                       {"content": [{"type": "text", "text": "{}"}, {"type": "text", "text": "{}"}]},
                       {"structuredContent": True},
                       {"content": [{"type": "text", "text": '{"approved":false,"approved":true}'}]},
                       {"content": [{"type": "text", "text": '{"ask":NaN}'}]}):
            with self.assertRaises(mcp.MCPError):
                mcp._tool_result(result)

    def test_json_rpc_id_and_sse_match_required(self):
        client = mcp.MCPClient()
        for rid in (2, True, "1", None):
            raw = json.dumps({"jsonrpc": "2.0", "id": rid, "result": {}}).encode()
            with self.assertRaises(mcp.MCPError):
                client._parse_response_body(raw, "application/json", 1)
        with self.assertRaises(mcp.MCPError):
            client._parse_response_body(b'data: {"jsonrpc":"2.0","id":2,"result":{}}\n\n', "text/event-stream", 1)
        good = b'data: {"jsonrpc":"2.0","id":1,"result":{}}\n\n'
        self.assertEqual(client._parse_response_body(good, "text/event-stream", 1)["id"], 1)
        with self.assertRaises(mcp.MCPError):
            client._parse_response_body(good + good, "text/event-stream", 1)

    def test_schema_unknown_and_wrong_types_fail_before_call(self):
        for prop in ({"type": "string", "pattern": ".*"}, {"anyOf": [{"type": "string"}]},
                     {"$ref": "https://evil.test/schema"}):
            transport = FixtureTransport([definition("get_option_quotes", {"instrument_ids": prop})])
            client = mcp.MCPClient(transport=transport)
            with self.assertRaises(mcp.MCPError):
                client.call_tool("get_option_quotes", {"instrument_ids": [OPTION_ID]})
            self.assertFalse(any(x["method"] == "tools/call" for x in transport.calls))
        for value in (True, 0, -1, float("nan")):
            with self.assertRaises(mcp.MCPError):
                mcp._validate_schema({"type": "number", "exclusiveMinimum": 0}, value)
        with self.assertRaises(mcp.MCPError):
            mcp._validate_schema(schema({"known": STRING}), {"known": "x", "secret": "y"})

    def quote_routes(self, transport, bid="1.00", ask="1.10", instrument_id=OPTION_ID):
        instrument = {"id": OPTION_ID, "chain_symbol": "SPY", "expiration_date": "2026-09-25",
                      "type": "call", "strike_price": "700.0000"}
        transport.routes["get_option_quotes"] = {"structuredContent": {"results": [{
            "quote": {"instrument_id": instrument_id, "ask_price": ask, "bid_price": bid,
                      "updated_at": "2026-09-21T17:00:00+00:00"}}]}}
        transport.routes["get_option_instruments"] = {"structuredContent": {"instruments": [instrument]}}

    def test_injected_offline_quote_flow_fails_closed_on_mismatch(self):
        transport = FixtureTransport([definition("get_option_quotes", QUOTES_PROPERTIES),
                                      definition("get_option_instruments", {"ids": STRING})])
        self.quote_routes(transport)
        client = mcp.MCPClient(transport=transport, token_provider=Mock(side_effect=AssertionError("token")))
        quote = client.get_option_quote(OPTION_ID)
        self.assertEqual((quote["ask"], quote["bid"], quote["contract_symbol"]),
                         (1.1, 1.0, SYMBOL))
        client.token_provider.assert_not_called()
        self.quote_routes(transport, instrument_id="other-instrument")
        with self.assertRaises(mcp.MCPError):
            client.get_option_quote(OPTION_ID)

    def test_review_requires_broker_checks_and_fails_closed(self):
        transport = FixtureTransport([definition("review_option_order", REVIEW_PROPERTIES)])
        client = mcp.MCPClient(transport=transport)

        def review(**kwargs):
            return client.review_option_order(option_id=OPTION_ID, side="buy", position_effect="open",
                                              qty=1, order_type="limit", limit_price=1.0,
                                              underlying="SPY", **kwargs)

        transport.routes["review_option_order"] = {"structuredContent": {"approved": "yes"}}
        with self.assertRaises(mcp.MCPError):  # no order_checks: unsupported review result
            review()
        transport.routes["review_option_order"] = {"structuredContent": {"order_checks": None}}
        self.assertTrue(review()["approved"])
        transport.routes["review_option_order"] = {"structuredContent": {
            "order_checks": {"OPTION_NO_BID_PRICE": "no bid"}}}
        preview = review()
        self.assertFalse(preview["approved"])
        self.assertEqual(len(preview["alerts"]), 1)

    def test_normalize_order_fails_closed(self):
        for data in (order_row(id="x"),
                     order_row(ref_id="not-a-uuid"),
                     order_row(state="bogus"),
                     order_row(quantity="0"),
                     order_row(state="filled", processed_quantity="0"),
                     order_row(state="filled", processed_quantity="1"),
                     order_row(state="partially_filled", processed_quantity="1", avg_fill_price="1.00"),
                     order_row(state="rejected", processed_quantity="1", avg_fill_price="1.00"),
                     order_row(state="pending", processed_quantity="2"),
                     order_row(legs=[{"option_id": OPTION_ID, "side": "hold", "position_effect": "open"}]),
                     order_row(legs=[])):
            with self.subTest(data=data), self.assertRaises(mcp.MCPError):
                mcp.MCPClient.normalize_order(data)

    def test_fresh_kill_check_after_token_and_before_http_dispatch(self):
        allowed = [True]
        def token():
            allowed[0] = False  # Kill became active while OAuth was refreshing.
            return "dummy-token"
        client = mcp.MCPClient(mode="live", account_number="TEST", token_provider=token,
                               mutation_guard=lambda: allowed[0])
        client._tools_cache = [definition("place_option_order", ORDER_PROPERTIES)]
        payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                   "params": {"name": "place_option_order", "arguments": ORDER_ARGS}}
        with patch.object(mcp, "LIVE_TRADING_ENABLED", True), patch.object(client._opener, "open") as opener:
            with self.assertRaisesRegex(mcp.MCPError, "arm/kill"):
                client._post(payload)
            opener.assert_not_called()

    def test_raw_paper_mutation_gate_and_account_mismatch(self):
        payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                   "params": {"name": "place_option_order", "arguments": ORDER_ARGS}}
        client = mcp.MCPClient(transport=Mock())
        with self.assertRaisesRegex(mcp.MCPError, "paper mode"):
            client._post(payload)
        client.transport.assert_not_called()
        live = mcp.MCPClient(mode="live", account_number="OTHER", transport=Mock(), mutation_guard=lambda: True)
        live._tools_cache = [definition("place_option_order", ORDER_PROPERTIES)]
        with patch.object(mcp, "LIVE_TRADING_ENABLED", True):
            with self.assertRaisesRegex(mcp.MCPError, "account"):
                live._post(payload)
        live.transport.assert_not_called()

    def test_schema_must_include_idempotency_argument(self):
        props = {k: v for k, v in ORDER_PROPERTIES.items() if k != "ref_id"}
        transport = FixtureTransport([definition("place_option_order", props)])
        client = mcp.MCPClient(mode="live", account_number="TEST", transport=transport, mutation_guard=lambda: True)
        with patch.object(mcp, "LIVE_TRADING_ENABLED", True):
            with self.assertRaises(mcp.MCPError):
                client.place_option_order(option_id=OPTION_ID, side="buy", position_effect="open",
                                          qty=1, order_type="limit", limit_price=1.0, ref_id=REF_ID)
        self.assertFalse(any(p["method"] == "tools/call" for p in transport.calls))

    def test_mutation_fixture_normalizes_fill_and_preserves_idempotency(self):
        result = dict(order_row(), state="filled", processed_quantity="1", avg_fill_price="1.00")
        transport = FixtureTransport([definition("place_option_order", ORDER_PROPERTIES)], result)
        client = mcp.MCPClient(mode="live", account_number="TEST", transport=transport, mutation_guard=lambda: True)
        # Patching this source constant is permitted only inside this offline fixture.
        with patch.object(mcp, "LIVE_TRADING_ENABLED", True):
            order = client.place_option_order(option_id=OPTION_ID, side="buy", position_effect="open",
                                              qty=1, order_type="limit", limit_price=1.0, ref_id=REF_ID)
        self.assertEqual((order["filled_qty"], order["avg_fill_price"], order["status"]),
                         (1, 1.0, "filled"))
        self.assertEqual(order["ref_id"], REF_ID)
        self.assertEqual(transport.calls[-1]["params"]["arguments"]["ref_id"], REF_ID)

    def test_find_option_contracts_resolves_chain_then_instruments(self):
        transport = FixtureTransport([definition("get_option_chains", CHAINS_PROPERTIES),
                                      definition("get_option_instruments", INSTRUMENTS_PROPERTIES)])
        transport.routes["get_option_chains"] = {"structuredContent": {"chains": [
            {"id": "chain-1", "expiration_dates": ["2026-09-25"], "can_open_position": True}]}}
        instrument = {"id": OPTION_ID, "chain_symbol": "SPY", "expiration_date": "2026-09-25",
                      "type": "call", "strike_price": "700.0000", "state": "active",
                      "tradability": "tradable", "trade_value_multiplier": "100"}
        transport.routes["get_option_instruments"] = {"structuredContent": {"instruments": [instrument]}}
        client = mcp.MCPClient(transport=transport)
        self.assertEqual(client.find_option_contracts("SPY", "2026-09-25", 700.0, "call"),
                         [{"option_id": OPTION_ID, "contract_symbol": SYMBOL, "underlying": "SPY",
                           "expiry": "2026-09-25", "strike": 700.0, "option_type": "call",
                           "tradability": "tradable", "multiplier": 100.0}])
        transport.routes["get_option_instruments"] = {"structuredContent": {"instruments": [
            dict(instrument, strike_price="701.0000")]}}
        with self.assertRaises(mcp.MCPError):
            client.find_option_contracts("SPY", "2026-09-25", 700.0, "call")

    def test_normalize_order_preserves_intent_and_rejects_unknown_states(self):
        norm = mcp.MCPClient.normalize_order(order_row())
        self.assertEqual((norm["order_id"], norm["ref_id"], norm["option_id"], norm["side"],
                          norm["quantity"], norm["status"], norm["filled_qty"], norm["contract_symbol"]),
                         (ORDER_ID, REF_ID, OPTION_ID, "buy", 1, "pending", 0, SYMBOL))
        for field in ("id", "state", "quantity"):
            missing = order_row()
            missing.pop(field)
            with self.subTest(field=field), self.assertRaises(mcp.MCPError):
                mcp.MCPClient.normalize_order(missing)
        without_legs = order_row()
        without_legs.pop("legs")
        with self.assertRaises(mcp.MCPError):
            mcp.MCPClient.normalize_order(without_legs)
        for status in ("open", "expired", "bogus"):
            with self.subTest(status=status), self.assertRaises(mcp.MCPError):
                mcp.MCPClient.normalize_order(order_row(state=status))
        # Known broker aliases map to the engine's pending/canceled states.
        self.assertEqual(mcp.MCPClient.normalize_order(order_row(state="queued"))["status"], "pending")
        self.assertEqual(mcp.MCPClient.normalize_order(order_row(state="confirmed"))["status"], "pending")
        self.assertEqual(mcp.MCPClient.normalize_order(order_row(state="cancelled"))["status"], "canceled")
        for status, qty, avg in (("filled", "0", None), ("partially_filled", "1", "1.00"),
                                 ("rejected", "1", "1.00")):
            with self.subTest(status=status), self.assertRaises(mcp.MCPError):
                mcp.MCPClient.normalize_order(order_row(state=status, processed_quantity=qty,
                                                        avg_fill_price=avg))

    def test_review_approval_contract_matches_engine(self):
        transport = FixtureTransport([definition("review_option_order", REVIEW_PROPERTIES)],
                                     {"order_checks": {}})
        client = mcp.MCPClient(transport=transport)
        for side, effect in (("buy", "open"), ("sell", "close")):
            preview = client.review_option_order(option_id=OPTION_ID, side=side, position_effect=effect,
                                                 qty=1, order_type="limit", limit_price=1.0,
                                                 underlying="SPY")
            self.assertTrue(preview["approved"])
        with self.assertRaises(mcp.MCPError):
            client.review_option_order(option_id=OPTION_ID, side="sell", position_effect="close",
                                       qty=1, order_type="market", limit_price=1.0, underlying="SPY")

    def test_raw_mutation_cannot_bypass_limit_qty_or_idempotency_contract(self):
        permissive_leg = {"type": "object",
                          "properties": {"option_id": STRING, "side": STRING, "position_effect": STRING},
                          "required": [], "additionalProperties": False}
        permissive = {key: {"type": "array", "items": permissive_leg} if key == "legs" else STRING
                      for key in ORDER_PROPERTIES}
        client = mcp.MCPClient(mode="live", account_number="TEST", transport=Mock(), mutation_guard=lambda: True)
        client._tools_cache = [definition("place_option_order", permissive, required=[])]
        def args(**changes):
            row = dict(ORDER_ARGS)
            row.update(changes)
            return row
        bad = [args(type="bogus"),  # raw contract rejects unknown types; the typed
                               # wrapper additionally restricts the engine to limit-only
               args(legs=[{"option_id": OPTION_ID, "side": "sell_short", "position_effect": "open"}]),
               args(quantity="0"),
               args(price="-1.00"),
               args(ref_id="not-a-uuid")]
        with patch.object(mcp, "LIVE_TRADING_ENABLED", True):
            for arguments in bad:
                payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                           "params": {"name": "place_option_order", "arguments": arguments}}
                with self.subTest(arguments=arguments), self.assertRaises(mcp.MCPError):
                    client._post(payload)
            # The typed wrapper makes ref_id mandatory: it cannot be omitted.
            with self.assertRaises(TypeError):
                client.place_option_order(option_id=OPTION_ID, side="buy", position_effect="open",
                                          qty=1, order_type="limit", limit_price=1.0)
        client.transport.assert_not_called()

    def test_real_adapter_paper_entry_and_exit_contract(self):
        # Cross-module regression: independent engine fakes had hidden a side /
        # order-type mismatch between the engine and actual MCP wrappers.
        import entry_engine
        import exits
        import kill
        from trade_state import TradingState
        from config import Policy
        now = datetime(2026, 9, 21, 15, 0, tzinfo=timezone.utc)
        symbol = SYMBOL
        instrument = {"id": OPTION_ID, "chain_symbol": "SPY", "expiration_date": "2026-09-25",
                      "type": "call", "strike_price": "700.0000", "state": "active",
                      "tradability": "tradable", "trade_value_multiplier": "100"}
        tools = [definition("get_option_chains", CHAINS_PROPERTIES),
                 definition("get_option_instruments", INSTRUMENTS_PROPERTIES),
                 definition("get_option_quotes", QUOTES_PROPERTIES),
                 definition("review_option_order", REVIEW_PROPERTIES)]
        fixture = FixtureTransport(tools)
        bid_ask = [0.95, 1.0]
        def transport(payload):
            if payload["method"] == "tools/call":
                name = payload["params"]["name"]
                if name == "get_option_chains":
                    fixture.routes["get_option_chains"] = {"structuredContent": {"chains": [
                        {"id": "chain-1", "expiration_dates": ["2026-09-25"],
                         "can_open_position": True}]}}
                elif name == "get_option_instruments":
                    fixture.routes["get_option_instruments"] = {"structuredContent": {
                        "instruments": [instrument]}}
                elif name == "get_option_quotes":
                    fixture.routes["get_option_quotes"] = {"structuredContent": {"results": [{
                        "quote": {"instrument_id": OPTION_ID, "ask_price": str(bid_ask[1]),
                                  "bid_price": str(bid_ask[0]), "updated_at": now.isoformat()}}]}}
                elif name == "review_option_order":
                    fixture.routes["review_option_order"] = {"structuredContent": {"order_checks": {}}}
                else:
                    self.fail("Paper trading must not dispatch broker mutations")
            return fixture(payload)
        client = mcp.MCPClient(transport=transport, token_provider=Mock(side_effect=AssertionError("credential access")))
        with tempfile.TemporaryDirectory(prefix="copypasta-adapter-integration-") as root:
            from pathlib import Path
            state = TradingState(root=Path(root).resolve())
            kill.arm(state)
            alert = {"id": "100", "handle": "cassytrades", "source_id": "offline-source",
                     "text": "$SPY 700c 9/25 1.00 entry", "posted_at": now.isoformat(),
                     "url": "https://x.com/cassytrades/status/100", "type": "entry"}
            entry = entry_engine.process_entry(alert, client, state=state, policy=Policy(), now=now)
            self.assertEqual(entry.action, "fired", entry.reason)
            self.assertEqual(state.snapshot()["positions"][0]["qty_remaining"], 5)
            bid_ask[:] = [4.0, 4.1]
            notes = exits.ExitMonitor(client, state=state).check(now=now)
            self.assertTrue(notes and all(n["kind"] == "fired" for n in notes), notes)
            self.assertEqual(state.snapshot()["positions"][0]["qty_remaining"], 0)
        client.token_provider.assert_not_called()

    def test_redirect_handler_and_error_body_redaction(self):
        with self.assertRaisesRegex(mcp.MCPError, "redirect refused"):
            mcp._NoRedirect().redirect_request(None, None, 307, "", {}, "https://evil.test")
        client = mcp.MCPClient(mode="live", account_number="TEST", token_provider="dummy-token")
        error = urllib.error.HTTPError(mcp.ENDPOINT, 500, "dummy-secret", {}, io.BytesIO(b"secret inject instructions"))
        with patch.object(client._opener, "open", side_effect=error):
            with self.assertRaises(mcp.MCPError) as caught:
                client._post({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        self.assertNotIn("secret", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
