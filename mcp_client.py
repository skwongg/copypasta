"""MCP client for Robinhood's Agentic MCP server (streamable HTTP transport).

Endpoint: https://agent.robinhood.com/mcp/trading
Spec: Model Context Protocol, streamable HTTP transport (JSON-RPC 2.0 over HTTP POST).

Transport mechanics (per the MCP streamable-HTTP spec, verified against the
public spec Sept 2026):
  * Every call is an HTTP POST of a JSON-RPC 2.0 envelope.
  * Request headers:
      Content-Type: application/json
      Accept: application/json, text/event-stream   (server may answer as
          plain JSON or as SSE with `event: message` / `data: <json>` frames)
      Authorization: Bearer <oauth2 token>          (Robinhood uses OAuth 2.0)
      Mcp-Session-Id: <id>                          (once a session exists)
      MCP-Protocol-Version: <version>               (negotiated at initialize)
  * The FIRST request must be `initialize` with protocolVersion, capabilities,
    and clientInfo. The server replies with its session id in the
    `Mcp-Session-Id` response header; all later requests echo it back.
    After `initialize` the client sends the `notifications/initialized`
    notification (a JSON-RPC notification: no `id`, no response expected).
  * Sessions can be torn down with an HTTP DELETE + `Mcp-Session-Id` header.
  * A 401 means the bearer token is invalid/expired -> AuthError.

Auth: the OAuth 2.0 bearer token comes from the sibling module
`oauth_client.get_valid_token()`. Import it lazily so this module works even
if oauth_client.py is not built yet; alternatively pass any zero-arg
`token_provider` callable (or a plain token string).

SAFETY: this module never constructs a live client on its own. `mode`
defaults to "dry_run"; order-mutating wrappers (place/cancel) refuse to call
the tool unless mode == "live", and "live" may only be chosen explicitly by
the orchestrator/operator at runtime. Nothing in this build may construct
`MCPClient(mode="live")`.

NOTE ON TOOL NAMES: Robinhood's real trading-tool names are UNVERIFIED (the
endpoint was not live-reachable during this build and options order support
is per-account). Capability binding is therefore heuristic: `_bind()` maps
stable capability keys to actual tool names found via tools/list, using
case-insensitive substring heuristics. If a capability cannot be mapped, the
typed wrappers raise CapabilityMissing. The exact live question to verify is
at the bottom of this docstring.

LIVE VERIFICATION QUESTION (still to answer against the real endpoint):
  "Call tools/list on https://agent.robinhood.com/mcp/trading with a valid
   OAuth token and report: (1) the full list of tool names, (2) which of them
   create options orders, (3) whether any tool supports stop/limit conditional
   options orders, (4) the exact parameter schema of the options order tool
   (field names for contract symbol, side, qty, order type, limit price), and
   (5) whether equity order tools exist alongside options tools."

Stdlib only (urllib). No network calls are made at import time.
"""

import itertools
import json
import os
import ssl
import urllib.error
import urllib.request

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class MCPError(Exception):
    """Base error for MCP transport / protocol failures."""


class AuthError(MCPError):
    """401 from the server: bearer token invalid or expired -> re-run OAuth."""


class CapabilityMissing(MCPError):
    """A capability could not be bound to any real tool name from tools/list."""

    def __init__(self, capability, tools_seen):
        self.capability = capability
        self.tools_seen = list(tools_seen)
        super().__init__(
            "Capability %r not found: no tool name matched the heuristics. "
            "Tools seen on the server: %s. Verify tool names live via "
            "tools/list (see module docstring)."
            % (capability, ", ".join(self.tools_seen) or "(none)")
        )


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

MCP_PROTOCOL_VERSION = "2025-03-26"  # negotiated at initialize; updated on server echo


class MCPClient:
    """Streamable-HTTP MCP client for Robinhood's trading MCP endpoint.

    Args:
        token_provider: zero-arg callable returning a bearer token string, OR
            a plain token string, OR None. When None, `_token()` lazily
            imports `get_valid_token` from the sibling `oauth_client` module.
        endpoint: MCP streamable-HTTP URL (default the trading endpoint).
        mode: "dry_run" (default) or "live". Order-mutating tools are gated on
            mode == "live". NOTHING IN THIS BUILD MAY CONSTRUCT A LIVE CLIENT.
    """

    def __init__(self, token_provider=None, endpoint="https://agent.robinhood.com/mcp/trading",
                 mode="dry_run"):
        self.endpoint = endpoint
        self.mode = mode
        self.token_provider = token_provider
        self.session_id = None
        self.protocol_version = MCP_PROTOCOL_VERSION
        self._id_counter = itertools.count(1)
        self._tools_cache = None  # raw tools/list result
        self._bindings = None     # capability -> real tool name

        # urllib honors https_proxy/http_proxy env vars through its default
        # ProxyHandler, which matters inside the sandbox egress proxy. TLS
        # verification stays on (default SSL context).
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler(),
            urllib.request.HTTPSHandler(context=ssl.create_default_context()),
        )

    # -- auth -------------------------------------------------------------
    def _token(self):
        if self.token_provider is None:
            from oauth_client import get_valid_token  # sibling module; built separately
            return get_valid_token()
        if callable(self.token_provider):
            return self.token_provider()
        return self.token_provider

    # -- low-level transport ----------------------------------------------
    def _request_headers(self, include_session=True):
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": self.protocol_version,
            "User-Agent": "copy-trader-mcp-client/0.1",
        }
        token = self._token()
        if token:
            headers["Authorization"] = "Bearer %s" % token
        if include_session and self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        return headers

    def _parse_response_body(self, body: bytes, content_type: str, request_id):
        """Accept either plain JSON or an SSE stream; return the matching payload."""
        text = body.decode("utf-8", errors="replace")
        if "text/event-stream" in (content_type or ""):
            payloads = []
            for line in text.splitlines():
                line = line.strip()
                if line.startswith("data:"):
                    chunk = line[len("data:"):].strip()
                    if chunk and chunk != "[DONE]":
                        try:
                            payloads.append(json.loads(chunk))
                        except json.JSONDecodeError:
                            continue
            if not payloads:
                raise MCPError("SSE response contained no data frames")
            # Prefer the frame carrying our request id.
            for p in payloads:
                if isinstance(p, dict) and p.get("id") == request_id:
                    return p
            return payloads[-1]
        return json.loads(text)

    def _post(self, payload, include_session=True):
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(self.endpoint, data=data, method="POST")
        for k, v in self._request_headers(include_session=include_session).items():
            req.add_header(k, v)
        try:
            with self._opener.open(req, timeout=60) as resp:
                new_session = resp.headers.get("Mcp-Session-Id")
                if new_session:
                    self.session_id = new_session
                echoed_version = resp.headers.get("MCP-Protocol-Version")
                if echoed_version:
                    self.protocol_version = echoed_version
                return self._parse_response_body(
                    resp.read(), resp.headers.get("Content-Type", ""), payload.get("id")
                )
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise AuthError(
                    "401 Unauthorized from Robinhood MCP: bearer token invalid "
                    "or expired. Re-run the OAuth flow (oauth_client) and retry."
                )
            detail = e.read().decode("utf-8", errors="replace")[:500]
            raise MCPError("HTTP %d from MCP server: %s" % (e.code, detail))

    def _rpc(self, method, params=None):
        """Low-level JSON-RPC 2.0 call over streamable HTTP with session handling."""
        self._ensure_session()
        request_id = next(self._id_counter)
        envelope = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            envelope["params"] = params
        response = self._post(envelope)
        if not isinstance(response, dict) or response.get("jsonrpc") != "2.0":
            raise MCPError("Malformed JSON-RPC response: %r" % (response,))
        if "error" in response:
            err = response["error"]
            raise MCPError("JSON-RPC error %s: %s"
                           % (err.get("code"), err.get("message")))
        return response.get("result")

    def _ensure_session(self):
        """Run the initialize + notifications/initialized handshake once."""
        if self.session_id is not None:
            return
        request_id = next(self._id_counter)
        init = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "initialize",
            "params": {
                "protocolVersion": self.protocol_version,
                "capabilities": {},
                "clientInfo": {"name": "copy-trader", "version": "0.1"},
            },
        }
        # initialize goes out WITHOUT a session id (server creates one).
        response = self._post(init, include_session=False)
        if not isinstance(response, dict) or "error" in response:
            raise MCPError("initialize failed: %r" % (response,))
        server_version = (response.get("result") or {}).get("protocolVersion")
        if server_version:
            self.protocol_version = server_version
        if not self.session_id:
            raise MCPError("initialize succeeded but server returned no Mcp-Session-Id")
        # Complete the handshake with the initialized notification (no id,
        # no response expected).
        notify = {"jsonrpc": "2.0", "method": "notifications/initialized"}
        data = json.dumps(notify).encode("utf-8")
        req = urllib.request.Request(self.endpoint, data=data, method="POST")
        for k, v in self._request_headers().items():
            req.add_header(k, v)
        try:
            with self._opener.open(req, timeout=60) as resp:
                resp.read()
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise AuthError("401 on notifications/initialized: re-run OAuth.")
            # Notifications may legitimately return 202/empty; only hard-fail
            # on auth. Other statuses are tolerated here.
            pass

    def close(self):
        """Best-effort session teardown (HTTP DELETE + Mcp-Session-Id)."""
        if not self.session_id:
            return
        req = urllib.request.Request(self.endpoint, method="DELETE")
        for k, v in self._request_headers().items():
            req.add_header(k, v)
        try:
            with self._opener.open(req, timeout=15) as resp:
                resp.read()
        except Exception:
            pass
        finally:
            self.session_id = None

    # -- tools/list + capability binding ----------------------------------
    def list_tools(self):
        """Return the raw list of tool dicts from tools/list (cached)."""
        if self._tools_cache is None:
            result = self._rpc("tools/list", {}) or {}
            self._tools_cache = result.get("tools", [])
        return self._tools_cache

    def refresh_tools(self):
        """Clear the tools/list cache (e.g. after a server-side tool rollout)."""
        self._tools_cache = None
        self._bindings = None

    # capability -> required substrings (all must appear, case-insensitive)
    _HEURISTICS = {
        "quote": (("quote",), ("quote", "price")),
        "find_contracts": (("contract",), ("option", "chain"), ("expir",)),
        "option_quote": (("option", "quote"), ("quote", "contract")),
        "review_option_order": (("review", "option", "order"), ("preview", "option", "order"),
                                ("simulate", "option", "order")),
        "place_option_order": (("place", "option", "order"), ("create", "option", "order"),
                               ("submit", "option", "order")),
        "cancel_order": (("cancel", "order"),),
        "positions": (("position",),),
        "orders": (("order", "history"), ("list", "order"), ("get", "order")),
        "conditional_place": (("conditional", "order"), ("stop", "order"), ("oco",)),
        "equity_quote": (("equity", "quote"), ("stock", "quote")),
        "place_equity_order": (("place", "equity", "order"), ("place", "stock", "order"),
                               ("create", "equity", "order")),
    }

    def _bind(self):
        """Map capability keys to real tool names using substring heuristics.

        The heuristics deliberately do NOT invent tool names: a capability is
        bound only if some tool from tools/list matches. Ambiguity rule: the
        first match in server order wins; the full tools list is available via
        list_tools() for inspection.
        """
        if self._bindings is not None:
            return self._bindings
        tools = self.list_tools()
        names = [(t.get("name") or "") for t in tools]
        lowered = [n.lower() for n in names]
        bindings = {}
        for capability, alternatives in self._HEURISTICS.items():
            bindings[capability] = None
            for alt in alternatives:
                for i, lname in enumerate(lowered):
                    if all(sub in lname for sub in alt):
                        bindings[capability] = names[i]
                        break
                if bindings[capability]:
                    break
        self._bindings = bindings
        return bindings

    def _tool_for(self, capability):
        name = self._bind().get(capability)
        if not name:
            raise CapabilityMissing(capability,
                                    [t.get("name") for t in self.list_tools()])
        return name

    def check_options_support(self):
        """Answer THE capability question from live tools/list data.

        Returns {"options_orders": bool, "conditional_orders": bool,
                 "equities_orders": bool, "tools_found": [...], "missing": [...]}.
        """
        bindings = self._bind()
        tools_found = sorted({n for n in bindings.values() if n})
        key_caps = ["quote", "find_contracts", "option_quote", "review_option_order",
                    "place_option_order", "cancel_order", "positions", "orders",
                    "conditional_place"]
        missing = [c for c in key_caps if not bindings.get(c)]
        return {
            "options_orders": bool(bindings.get("place_option_order")),
            "conditional_orders": bool(bindings.get("conditional_place")),
            "equities_orders": bool(bindings.get("place_equity_order")),
            "tools_found": tools_found,
            "missing": missing,
        }

    def call_tool(self, name, arguments):
        """Call a tool by its REAL server name; return the result dict."""
        result = self._rpc("tools/call", {"name": name, "arguments": arguments or {}})
        return result if isinstance(result, dict) else {"result": result}

    # -- typed wrappers ----------------------------------------------------
    def get_quote(self, symbol):
        """Underlying/equity quote -> {"bid","ask","last",...}."""
        tool = self._tool_for("quote")
        return self.call_tool(tool, {"symbol": symbol})

    def find_option_contracts(self, underlying, expiry, strike: float, option_type):
        """Find option contracts -> list of {"contract_symbol","expiry","strike","option_type"}.

        expiry: "YYYY-MM-DD"; option_type: "call" | "put".
        """
        tool = self._tool_for("find_contracts")
        return self.call_tool(tool, {
            "underlying": underlying,
            "expiry": expiry,
            "strike": strike,
            "option_type": option_type,
        })

    def get_option_quote(self, contract_symbol):
        """Option quote -> {"bid","ask","last",...}."""
        tool = self._tool_for("option_quote")
        return self.call_tool(tool, {"contract_symbol": contract_symbol})

    def review_option_order(self, contract_symbol, side, qty, order_type, limit_price=None):
        """Simulate/review an option order -> simulation dict (no side effects)."""
        tool = self._tool_for("review_option_order")
        args = {"contract_symbol": contract_symbol, "side": side,
                "quantity": qty, "order_type": order_type}
        if limit_price is not None:
            args["limit_price"] = limit_price
        return self.call_tool(tool, args)

    def place_option_order(self, contract_symbol, side, qty, order_type, limit_price=None):
        """Place an option order. DRY-RUN GATED: calls the tool ONLY in live mode."""
        args = {"contract_symbol": contract_symbol, "side": side,
                "quantity": qty, "order_type": order_type}
        if limit_price is not None:
            args["limit_price"] = limit_price
        tool = self._tool_for("place_option_order")
        if self.mode != "live":
            return {"dry_run": True, "would_call": {"tool": tool, "arguments": args}}
        return self.call_tool(tool, args)

    def cancel_order(self, order_id):
        """Cancel an order. DRY-RUN GATED: calls the tool ONLY in live mode."""
        tool = self._tool_for("cancel_order")
        if self.mode != "live":
            return {"dry_run": True,
                    "would_call": {"tool": tool, "arguments": {"order_id": order_id}}}
        return self.call_tool(tool, {"order_id": order_id})

    def get_positions(self):
        """Open positions -> list of {"contract_symbol","qty","avg_price",...}."""
        tool = self._tool_for("positions")
        return self.call_tool(tool, {})

    def get_orders(self, status=None):
        """Orders -> list. Optional status filter (e.g. "open", "filled")."""
        tool = self._tool_for("orders")
        args = {}
        if status is not None:
            args["status"] = status
        return self.call_tool(tool, args)


# ---------------------------------------------------------------------------
# Self-test (mocked transport — no network, no real orders)
# ---------------------------------------------------------------------------

class _FakeTransportClient(MCPClient):
    """MCPClient with _rpc stubbed: replays scripted JSON-RPC results."""

    def __init__(self, tools, **kwargs):
        super().__init__(token_provider=lambda: "fake-token", **kwargs)
        self._fake_tools = tools
        self.rpc_calls = []  # record of (method, params) the client attempted

    def _ensure_session(self):
        self.session_id = "fake-session"

    def _rpc(self, method, params=None):
        self.rpc_calls.append((method, params))
        if method == "tools/list":
            return {"tools": self._fake_tools}
        if method == "tools/call":
            return {"content": [{"type": "text", "text": "ok"}],
                    "echo": {"name": params["name"], "arguments": params["arguments"]}}
        raise AssertionError("unexpected rpc: %r" % method)


def _fake_tool(name, description=""):
    return {"name": name, "description": description, "inputSchema": {"type": "object"}}


def run_self_test():
    passed, failed = [], []

    def check(label, cond, detail=""):
        (passed if cond else failed).append(label)
        print(("PASS " if cond else "FAIL ") + label + ((" — " + detail) if detail and not cond else ""))

    # --- Scenario A: plausible tool names ----------------------------------
    tools_a = [
        _fake_tool("get_quote", "Get latest quote for a symbol"),
        _fake_tool("get_option_chain", "List option contracts for an underlying"),
        _fake_tool("get_option_quote", "Quote for an option contract"),
        _fake_tool("preview_option_order", "Simulate an option order"),
        _fake_tool("place_option_order", "Submit an option order"),
        _fake_tool("cancel_order", "Cancel an open order"),
        _fake_tool("get_positions", "Open positions"),
        _fake_tool("list_orders", "Order history"),
    ]
    c = _FakeTransportClient(tools_a)  # default mode="dry_run"
    check("list_tools returns raw tool dicts",
          isinstance(c.list_tools(), list) and c.list_tools()[0]["name"] == "get_quote")
    b = c._bind()
    check("bind place_option_order", b["place_option_order"] == "place_option_order", str(b))
    check("bind cancel_order", b["cancel_order"] == "cancel_order")
    check("bind review_option_order", b["review_option_order"] == "preview_option_order")
    check("bind find_contracts", b["find_contracts"] == "get_option_chain")
    check("bind conditional_place unmapped", b["conditional_place"] is None)
    sup = c.check_options_support()
    check("support: options_orders True", sup["options_orders"] is True, str(sup))
    check("support: conditional_orders False", sup["conditional_orders"] is False, str(sup))
    check("support: missing lists conditional_place", "conditional_place" in sup["missing"], str(sup))

    # --- dry_run gate: mutating wrappers must NOT call the tool ------------
    n_before = len(c.rpc_calls)
    r = c.place_option_order("SPY260926C00700000", "buy_to_open", 1, "limit", 2.50)
    check("place dry_run returns dry_run=True", r.get("dry_run") is True, str(r))
    check("place dry_run names would-be tool+args",
          r.get("would_call", {}).get("tool") == "place_option_order"
          and r["would_call"]["arguments"]["contract_symbol"] == "SPY260926C00700000"
          and r["would_call"]["arguments"]["limit_price"] == 2.50, str(r))
    check("place dry_run made no tools/call",
          all(m != "tools/call" for m, _ in c.rpc_calls[n_before:]),
          str([m for m, _ in c.rpc_calls[n_before:]]))
    r = c.cancel_order("ord-123")
    check("cancel dry_run returns dry_run=True", r.get("dry_run") is True, str(r))
    check("cancel dry_run made no tools/call",
          all(m != "tools/call" for m, _ in c.rpc_calls), str(c.rpc_calls))

    # --- read-only wrappers DO call through --------------------------------
    c.get_quote("SPY")
    c.find_option_contracts("SPY", "2026-09-25", 700.0, "call")
    c.get_option_quote("SPY260925C00700000")
    c.review_option_order("SPY260925C00700000", "buy_to_open", 1, "limit", 1.20)
    c.get_positions()
    c.get_orders(status="open")
    called = [p["name"] for m, p in c.rpc_calls if m == "tools/call"]
    check("read wrappers reached tools/call",
          called == ["get_quote", "get_option_chain", "get_option_quote",
                     "preview_option_order", "get_positions", "list_orders"], str(called))
    rev = [p for m, p in c.rpc_calls if m == "tools/call" and p["name"] == "preview_option_order"][0]
    check("review passes limit_price", rev["arguments"].get("limit_price") == 1.20, str(rev))

    # --- Scenario B: different naming scheme still binds -------------------
    tools_b = [
        _fake_tool("Quote", "quote"),
        _fake_tool("OptionChainLookup", "chain"),
        _fake_tool("OptionQuote", "oq"),
        _fake_tool("ReviewOptionOrder", "rev"),
        _fake_tool("CreateOptionOrder", "create"),
        _fake_tool("CancelOrder", "cancel"),
        _fake_tool("Positions", "pos"),
        _fake_tool("OrderHistory", "hist"),
        _fake_tool("ConditionalOrderEntry", "cond"),
        _fake_tool("PlaceEquityOrder", "eq"),
    ]
    c2 = _FakeTransportClient(tools_b)
    b2 = c2._bind()
    check("alt bind place_option_order", b2["place_option_order"] == "CreateOptionOrder", str(b2))
    check("alt bind review_option_order", b2["review_option_order"] == "ReviewOptionOrder")
    check("alt bind conditional_place", b2["conditional_place"] == "ConditionalOrderEntry")
    check("alt bind quote (case-insensitive)", b2["quote"] == "Quote")
    sup2 = c2.check_options_support()
    check("alt support conditional_orders True", sup2["conditional_orders"] is True, str(sup2))
    check("alt support equities_orders True", sup2["equities_orders"] is True, str(sup2))

    # --- Scenario C: options ordering tool absent -> CapabilityMissing ------
    tools_c = [_fake_tool("get_quote"), _fake_tool("get_positions")]
    c3 = _FakeTransportClient(tools_c)
    try:
        c3.place_option_order("X", "buy_to_open", 1, "market")
        check("CapabilityMissing raised for place_option_order", False)
    except CapabilityMissing as e:
        check("CapabilityMissing raised for place_option_order",
              e.capability == "place_option_order" and "get_quote" in str(e))
    sup3 = c3.check_options_support()
    check("no options support detected", sup3["options_orders"] is False
          and "place_option_order" in sup3["missing"], str(sup3))

    # --- Scenario D: SSE response parsing (no network) ---------------------
    c4 = _FakeTransportClient(tools_a)
    sse = (b'event: message\n'
           b'data: {"jsonrpc":"2.0","id":7,"result":{"tools":[]}}\n\n'
           b'event: message\n'
           b'data: {"jsonrpc":"2.0","id":8,"result":{"tools":[{"name":"t"}]}}\n\n')
    parsed = c4._parse_response_body(sse, "text/event-stream; charset=utf-8", 8)
    check("SSE parse picks matching id",
          parsed.get("result", {}).get("tools") == [{"name": "t"}], str(parsed))
    plain = c4._parse_response_body(b'{"jsonrpc":"2.0","id":9,"result":{"ok":true}}',
                                    "application/json", 9)
    check("plain JSON parse", plain.get("result") == {"ok": True}, str(plain))

    # --- Scenario E: 401 -> AuthError --------------------------------------
    c5 = MCPClient(token_provider=lambda: "bad")
    def boom(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)
    c5._opener.open = boom
    try:
        c5._post({"jsonrpc": "2.0", "id": 1, "method": "ping"}, include_session=False)
        check("401 raises AuthError", False)
    except AuthError as e:
        check("401 raises AuthError", "re-run" in str(e).lower() or "OAuth" in str(e))
    except Exception as e:  # noqa: BLE001
        check("401 raises AuthError", False, "got %r" % e)

    # --- Scenario F: live mode actually calls (still fake transport) -------
    c6 = _FakeTransportClient(tools_a, mode="live")
    r6 = c6.place_option_order("SPY260926C00700000", "buy_to_open", 1, "market")
    check("live mode reaches tools/call",
          any(m == "tools/call" and p["name"] == "place_option_order"
              for m, p in c6.rpc_calls), str(r6))
    check("live mode result not dry_run", r6.get("dry_run") is not True, str(r6))

    print("\n%d passed, %d failed" % (len(passed), len(failed)))
    if failed:
        print("FAILED: " + ", ".join(failed))
        raise SystemExit(1)


if __name__ == "__main__":
    run_self_test()
