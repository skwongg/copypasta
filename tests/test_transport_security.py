"""Network-free fixtures define our contract; these are NOT broker schemas."""
import io
import json
from datetime import datetime, timezone
import tempfile
import unittest
import urllib.error
from unittest.mock import Mock, patch

import mcp_client as mcp


def schema(properties=None, required=None):
    props = properties or {}
    return {"type": "object", "properties": props, "required": list(props) if required is None else required,
            "additionalProperties": False}


def definition(name, properties=None, required=None):
    return {"name": name, "inputSchema": schema(properties, required)}


STRING = {"type": "string", "minLength": 1}
ORDER_PROPERTIES = {"account_number": STRING, "contract_symbol": STRING,
                    "side": {"type": "string", "enum": ["buy", "sell"]},
                    "quantity": {"type": "integer", "minimum": 1},
                    "order_type": {"type": "string", "enum": ["limit"]},
                    "limit_price": {"type": "number", "exclusiveMinimum": 0},
                    "client_order_id": STRING}
ORDER_ARGS = {"account_number": "TEST", "contract_symbol": "SPY260925C00700000", "side": "buy",
              "quantity": 1, "order_type": "limit", "limit_price": 1.0, "client_order_id": "intent-1"}


class FixtureTransport:
    def __init__(self, definitions, result=None):
        self.definitions = definitions
        self.result = {"structuredContent": result if result is not None else {}}
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
            result = self.result
        return {"jsonrpc": "2.0", "id": payload["id"], "result": result}


class TransportSecurity(unittest.TestCase):
    def test_default_paper_never_accesses_token_or_network(self):
        token = Mock(side_effect=AssertionError("credential access"))
        client = mcp.MCPClient(token_provider=token)
        with patch.object(client._opener, "open", side_effect=AssertionError("network")):
            with self.assertRaisesRegex(mcp.MCPError, "offline"):
                client.get_positions()
            with self.assertRaisesRegex(mcp.MCPError, "paper mode"):
                client.place_option_order("C", "buy", 1, "limit", 1)
        token.assert_not_called()

    def test_live_mutations_disabled_before_network_or_credentials(self):
        client = mcp.MCPClient(mode="live", account_number="TEST", token_provider=Mock())
        with patch.object(client._opener, "open", side_effect=AssertionError("network")):
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
            client.get_option_quote("C")
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
            transport = FixtureTransport([definition("get_option_quotes", {"contract_symbol": prop})])
            client = mcp.MCPClient(transport=transport)
            with self.assertRaises(mcp.MCPError):
                client.call_tool("get_option_quotes", {"contract_symbol": "C"})
            self.assertFalse(any(x["method"] == "tools/call" for x in transport.calls))
        for value in (True, 0, -1, float("nan")):
            with self.assertRaises(mcp.MCPError):
                mcp._validate_schema({"type": "number", "exclusiveMinimum": 0}, value)
        with self.assertRaises(mcp.MCPError):
            mcp._validate_schema(schema({"known": STRING}), {"known": "x", "secret": "y"})

    def test_injected_offline_quotes_review_and_list(self):
        transport = FixtureTransport([definition("get_option_quotes", {"contract_symbol": STRING})],
                                     [{"contract_symbol": "C", "bid": 1.0, "ask": 1.1}])
        client = mcp.MCPClient(transport=transport, token_provider=Mock(side_effect=AssertionError("token")))
        self.assertEqual(client.get_option_quote("C")["ask"], 1.1)
        client.token_provider.assert_not_called()
        transport.result = {"structuredContent": [{"contract_symbol": "wrong", "bid": 1, "ask": 2}]}
        with self.assertRaises(mcp.MCPError):
            client.get_option_quote("C")

    def test_missing_approval_and_order_fill_details_fail_closed(self):
        props = dict(ORDER_PROPERTIES)
        props.pop("client_order_id")
        props.pop("account_number")
        transport = FixtureTransport([definition("review_option_order", props)], {"approved": "yes"})
        with self.assertRaisesRegex(mcp.MCPError, "boolean approval"):
            mcp.MCPClient(transport=transport).review_option_order("C", "buy", 1, "limit", 1)
        for data in ({"order_id": "x", "status": "filled", "filled_qty": 0},
                     {"order_id": "x", "status": "filled", "filled_qty": 1},
                     {"order_id": "x", "status": "unknown", "filled_qty": 0},
                     {"order_id": "x", "status": "pending", "filled_qty": True}):
            with self.assertRaises(mcp.MCPError):
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
        props = dict(ORDER_PROPERTIES)
        props.pop("client_order_id")
        transport = FixtureTransport([definition("place_option_order", props)])
        client = mcp.MCPClient(mode="live", account_number="TEST", transport=transport, mutation_guard=lambda: True)
        with patch.object(mcp, "LIVE_TRADING_ENABLED", True):
            with self.assertRaises(mcp.MCPError):
                client.place_option_order("C", "buy", 1, "limit", 1, client_order_id="intent-1")
        self.assertFalse(any(p["method"] == "tools/call" for p in transport.calls))

    def test_mutation_fixture_normalizes_fill_and_preserves_idempotency(self):
        result = dict(ORDER_ARGS, order_id="broker-1", status="filled", filled_qty=1, avg_fill_price=1.0)
        transport = FixtureTransport([definition("place_option_order", ORDER_PROPERTIES)], result)
        client = mcp.MCPClient(mode="live", account_number="TEST", transport=transport, mutation_guard=lambda: True)
        # Patching this source constant is permitted only inside this offline fixture.
        with patch.object(mcp, "LIVE_TRADING_ENABLED", True):
            order = client.place_option_order(ORDER_ARGS["contract_symbol"], "buy", 1, "limit", 1,
                                             client_order_id="intent-1")
        self.assertEqual(order["filled_qty"], 1)
        self.assertEqual(transport.calls[-1]["params"]["arguments"]["client_order_id"], "intent-1")

    def test_instruments_list_exact_fields_and_review_approval(self):
        instrument = {"contract_symbol": "SPY260925C00700000", "underlying": "SPY", "expiry": "2026-09-25",
                      "strike": 700.0, "option_type": "call"}
        props = {"underlying": STRING, "expiry": STRING, "strike": {"type": "number"}, "option_type": STRING}
        transport = FixtureTransport([definition("get_option_instruments", props)], [instrument])
        client = mcp.MCPClient(transport=transport)
        self.assertEqual(client.find_option_contracts("SPY", "2026-09-25", 700.0, "call"), [instrument])
        transport.result = {"structuredContent": [{"contract_symbol": "C"}]}
        with self.assertRaises(mcp.MCPError):
            client.find_option_contracts("SPY", "2026-09-25", 700.0, "call")

    def test_order_contract_preserves_intent_and_rejects_unknown_status_aliases(self):
        valid = dict(ORDER_ARGS, order_id="broker-1", status="pending", filled_qty=0, avg_fill_price=None)
        self.assertEqual(mcp.MCPClient.normalize_order(valid), valid)
        for field in ("client_order_id", "contract_symbol", "quantity", "side", "avg_fill_price"):
            missing = dict(valid)
            missing.pop(field)
            with self.subTest(field=field), self.assertRaises(mcp.MCPError):
                mcp.MCPClient.normalize_order(missing)
        for status in ("open", "queued", "cancelled", "expired"):
            with self.subTest(status=status), self.assertRaises(mcp.MCPError):
                mcp.MCPClient.normalize_order(dict(valid, status=status))
        for status, qty in (("filled", 0), ("partially_filled", 1), ("rejected", 1)):
            with self.subTest(status=status), self.assertRaises(mcp.MCPError):
                mcp.MCPClient.normalize_order(dict(valid, status=status, filled_qty=qty, avg_fill_price=1.0))

    def test_normalized_side_and_order_type_match_engine_contract(self):
        props = dict(ORDER_PROPERTIES)
        props.pop("account_number")
        props.pop("client_order_id")
        transport = FixtureTransport([definition("review_option_order", props)], {"approved": True})
        client = mcp.MCPClient(transport=transport)
        self.assertTrue(client.review_option_order("C", "buy", 1, "limit", 1)["approved"])
        self.assertTrue(client.review_option_order("C", "sell", 1, "limit", 1)["approved"])
        with self.assertRaises(mcp.MCPError):
            client.review_option_order("C", "sell", 1, "market")

    def test_raw_mutation_cannot_bypass_limit_qty_or_idempotency_contract(self):
        permissive = {key: {"type": "number" if key == "limit_price" else "integer" if key == "quantity" else "string"}
                      for key in ORDER_PROPERTIES}
        client = mcp.MCPClient(mode="live", account_number="TEST", transport=Mock(), mutation_guard=lambda: True)
        client._tools_cache = [definition("place_option_order", permissive, required=[])]
        bad = [dict(ORDER_ARGS, order_type="market"), dict(ORDER_ARGS, side="sell_short"),
               dict(ORDER_ARGS, quantity=0), dict(ORDER_ARGS, limit_price=-1),
               {k: v for k, v in ORDER_ARGS.items() if k != "client_order_id"}]
        with patch.object(mcp, "LIVE_TRADING_ENABLED", True):
            for arguments in bad:
                payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                           "params": {"name": "place_option_order", "arguments": arguments}}
                with self.subTest(arguments=arguments), self.assertRaises(mcp.MCPError):
                    client._post(payload)
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
        symbol = "SPY   260925C00700000"
        instrument = {"contract_symbol": symbol, "underlying": "SPY", "expiry": "2026-09-25",
                      "strike": 700.0, "option_type": "call"}
        instrument_props = {"underlying": STRING, "expiry": STRING, "strike": {"type": "number"}, "option_type": STRING}
        order_props = {k: v for k, v in ORDER_PROPERTIES.items() if k not in {"account_number", "client_order_id"}}
        tools = [definition("get_option_instruments", instrument_props),
                 definition("get_option_quotes", {"contract_symbol": STRING}),
                 definition("review_option_order", order_props)]
        fixture = FixtureTransport(tools)
        bid_ask = [0.95, 1.0]
        def transport(payload):
            if payload["method"] == "tools/call":
                name = payload["params"]["name"]
                if name == "get_option_instruments":
                    fixture.result = {"structuredContent": [instrument]}
                elif name == "get_option_quotes":
                    fixture.result = {"structuredContent": {"contract_symbol": symbol,
                        "bid": bid_ask[0], "ask": bid_ask[1], "as_of": now.isoformat()}}
                elif name == "review_option_order":
                    fixture.result = {"structuredContent": {"approved": True}}
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
