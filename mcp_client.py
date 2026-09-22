"""Fail-closed MCP adapter. No network or credential access in paper mode.

The exact tool names are allowlisted, but this adapter's argument/result contract
has NOT been verified against a real account. LIVE_TRADING_ENABLED deliberately
blocks broker mutations until that separate review is completed. The normalized
shapes below are an offline contract, not a claim about Robinhood's live schema.
"""
from __future__ import annotations

import itertools
import json
import math
import ssl
import urllib.error
import urllib.request

ENDPOINT = "https://agent.robinhood.com/mcp/trading"
MCP_PROTOCOL_VERSION = "2025-03-26"
LIVE_TRADING_ENABLED = False  # Source-only gate; deliberately no environment bypass.
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
TOOL_NAMES = {
    "find_contracts": "get_option_instruments",
    "option_quote": "get_option_quotes",
    "review_option_order": "review_option_order",
    "place_option_order": "place_option_order",
    "cancel_order": "cancel_option_order",
    "positions": "get_option_positions",
    "orders": "get_option_orders",
}
MUTATING_TOOLS = frozenset({"place_option_order", "cancel_option_order"})
ALLOWED_TOOLS = frozenset(TOOL_NAMES.values())


class MCPError(Exception):
    """Redacted transport or contract failure."""


class AuthError(MCPError):
    pass


class CapabilityMissing(MCPError):
    def __init__(self, capability, tools_seen=()):
        self.capability = capability
        self.tools_seen = list(tools_seen)
        super().__init__("Required exact MCP capability unavailable")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise MCPError("MCP redirect refused")


def _number(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def _json_loads(value):
    def pairs(items):
        result = {}
        for key, item in items:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = item
        return result
    def reject_constant(_):
        raise ValueError("non-finite JSON number")
    return json.loads(value, object_pairs_hook=pairs, parse_constant=reject_constant)


def _validate_schema(schema, value):
    """Small, explicit JSON Schema subset; unsupported schemas fail closed."""
    allowed = {"type", "properties", "required", "additionalProperties", "items",
               "enum", "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
               "minLength", "maxLength", "minItems", "maxItems", "description", "title"}
    if not isinstance(schema, dict) or set(schema) - allowed:
        raise MCPError("Unsupported MCP input schema")
    kind = schema.get("type")
    predicates = {"object": lambda x: type(x) is dict,
                  "array": lambda x: type(x) is list,
                  "string": lambda x: type(x) is str,
                  "integer": lambda x: type(x) is int,
                  "number": _number,
                  "boolean": lambda x: type(x) is bool,
                  "null": lambda x: x is None}
    if type(kind) is not str or kind not in predicates or not predicates[kind](value):
        raise MCPError("MCP argument type does not match supported schema")
    if "enum" in schema and value not in schema["enum"]:
        raise MCPError("MCP argument outside allowed enumeration")
    if kind == "object":
        props = schema.get("properties")
        required = schema.get("required", [])
        if (not isinstance(props, dict) or not isinstance(required, list)
                or any(type(k) is not str or k not in props for k in required)
                or schema.get("additionalProperties", False) is not False):
            raise MCPError("Unsupported MCP object schema")
        if set(value) - set(props) or set(required) - set(value):
            raise MCPError("MCP arguments do not match required properties")
        # Even optional schema branches must use the supported subset.
        for prop in props.values():
            _check_schema_definition(prop)
        for key, item in value.items():
            _validate_schema(props[key], item)
    elif kind == "array":
        if "items" not in schema:
            raise MCPError("MCP array schema requires items")
        _check_schema_definition(schema["items"])
        for item in value:
            _validate_schema(schema["items"], item)
    for field, op in (("minimum", lambda a, b: a >= b),
                      ("maximum", lambda a, b: a <= b),
                      ("exclusiveMinimum", lambda a, b: a > b),
                      ("exclusiveMaximum", lambda a, b: a < b)):
        if field in schema:
            if not _number(value) or not _number(schema[field]) or not op(value, schema[field]):
                raise MCPError("MCP numeric argument violates schema")
    for low, high, expected in (("minLength", "maxLength", "string"),
                                ("minItems", "maxItems", "array")):
        for field in (low, high):
            if field in schema:
                bound = schema[field]
                if (kind != expected or type(bound) is not int or bound < 0
                        or (field == low and len(value) < bound)
                        or (field == high and len(value) > bound)):
                    raise MCPError("MCP argument length violates schema")


def _check_schema_definition(schema):
    """Validate schema syntax without inventing values for optional properties."""
    if not isinstance(schema, dict):
        raise MCPError("Unsupported MCP schema")
    allowed = {"type", "properties", "required", "additionalProperties", "items",
               "enum", "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
               "minLength", "maxLength", "minItems", "maxItems", "description", "title"}
    kind = schema.get("type")
    if set(schema) - allowed or type(kind) is not str or kind not in {"object", "array", "string", "integer", "number", "boolean", "null"}:
        raise MCPError("Unsupported MCP input schema")
    if "enum" in schema and (not isinstance(schema["enum"], list) or not schema["enum"]):
        raise MCPError("Unsupported MCP enumeration")
    if kind == "object":
        props = schema.get("properties")
        req = schema.get("required", [])
        if (type(props) is not dict or type(req) is not list
                or any(type(k) is not str or k not in props for k in req)
                or schema.get("additionalProperties", False) is not False):
            raise MCPError("Unsupported MCP object schema")
        for child in props.values():
            _check_schema_definition(child)
    elif "properties" in schema or "required" in schema or "additionalProperties" in schema:
        raise MCPError("Unsupported MCP schema constraint")
    if kind == "array":
        _check_schema_definition(schema.get("items"))
    elif "items" in schema:
        raise MCPError("Unsupported MCP schema constraint")
    for key in ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum"):
        if key in schema and (kind not in {"integer", "number"} or not _number(schema[key])):
            raise MCPError("Unsupported MCP numeric schema")
    for key, expected in (("minLength", "string"), ("maxLength", "string"),
                           ("minItems", "array"), ("maxItems", "array")):
        if key in schema and (kind != expected or type(schema[key]) is not int or schema[key] < 0):
            raise MCPError("Unsupported MCP length schema")


def _tool_result(result):
    if not isinstance(result, dict) or result.get("isError", False) is not False:
        raise MCPError("MCP tool failed or returned an invalid envelope")
    if "structuredContent" in result:
        data = result["structuredContent"]
    else:
        content = result.get("content")
        if (not isinstance(content, list) or len(content) != 1
                or not isinstance(content[0], dict) or content[0].get("type") != "text"
                or not isinstance(content[0].get("text"), str)):
            raise MCPError("MCP tool result requires one JSON text item or structured content")
        try:
            data = _json_loads(content[0]["text"])
        except (ValueError, TypeError):
            raise MCPError("MCP tool returned invalid JSON") from None
    if not isinstance(data, (dict, list)):
        raise MCPError("MCP tool result must be a JSON object or list")
    return data


class MCPClient:
    def __init__(self, token_provider=None, endpoint=ENDPOINT, mode="dry_run",
                 account_number=None, state_context=None, mutation_guard=None, transport=None):
        if endpoint != ENDPOINT:
            raise MCPError("Unapproved MCP endpoint")
        if mode not in {"dry_run", "live"}:
            raise MCPError("Invalid MCP mode")
        if mode == "live" and (type(account_number) is not str or not account_number.strip()):
            raise MCPError("Live MCP requires an explicit account number")
        self.endpoint, self.mode = endpoint, mode
        self.account_number, self.state_context = account_number, state_context
        self.token_provider, self.transport = token_provider, transport
        self.mutation_guard = mutation_guard
        self.session_id = None
        self.protocol_version = MCP_PROTOCOL_VERSION
        self._initialized = False
        self._id_counter = itertools.count(1)
        self._tools_cache = None
        self._bindings = None
        self._opener = urllib.request.build_opener(
            _NoRedirect(), urllib.request.HTTPSHandler(context=ssl.create_default_context()))

    def _token(self):
        if self.mode != "live":
            raise MCPError("Paper mode is offline; supply an explicit test transport")
        if self.token_provider is None:
            from oauth_client import get_valid_token
            token = get_valid_token()
        else:
            token = self.token_provider() if callable(self.token_provider) else self.token_provider
        if type(token) is not str or not token or any(c in token for c in "\r\n"):
            raise AuthError("No valid MCP bearer token available")
        return token

    def _request_headers(self, include_session=True):
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                   "MCP-Protocol-Version": self.protocol_version, "Authorization": "Bearer " + self._token()}
        if include_session and self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        return headers

    def _mutation_allowed(self):
        if self.mode != "live":
            raise MCPError("Broker mutations forbidden in paper mode")
        if not LIVE_TRADING_ENABLED:
            raise MCPError("Live trading disabled: broker adapter and account schema unverified")
        if type(self.account_number) is not str or not self.account_number.strip():
            raise MCPError("Broker mutations require an explicit account number")
        if self.state_context is not None and (
                self.state_context.mode != "live" or self.state_context.account != self.account_number):
            raise MCPError("Client mode/account does not match its trading state")
        try:
            if self.mutation_guard is not None:
                allowed = self.mutation_guard()
            else:
                import kill
                allowed = (kill.can_fire(context=self.state_context) if self.state_context is not None
                           else kill.can_fire(mode=self.mode, account=self.account_number))
        except Exception:
            raise MCPError("Broker mutation safety gate failed") from None
        if allowed is not True:
            raise MCPError("Broker mutation blocked by arm/kill gate")

    def assert_mutation_allowed(self):
        """Preflight without discovering tools or accessing credentials/network."""
        self._mutation_allowed()

    def _validate_dispatch(self, payload):
        if type(payload) is not dict or payload.get("jsonrpc") != "2.0":
            raise MCPError("Invalid outgoing JSON-RPC envelope")
        method = payload.get("method")
        if method not in {"initialize", "notifications/initialized", "tools/list", "tools/call"}:
            raise MCPError("Unapproved MCP method")
        if method == "tools/call":
            params = payload.get("params")
            if type(params) is not dict or set(params) != {"name", "arguments"}:
                raise MCPError("Invalid outgoing tool call")
            name = params["name"]
            if name not in ALLOWED_TOOLS:
                raise CapabilityMissing("unapproved tool")
            if name in MUTATING_TOOLS:
                self._mutation_allowed()
            definitions = {t["name"]: t for t in self._tools_cache or []}
            if name not in definitions:
                raise MCPError("Tool schema must be discovered before dispatch")
            schema = definitions[name].get("inputSchema")
            _check_schema_definition(schema)
            _validate_schema(schema, params["arguments"])
            if name in MUTATING_TOOLS:
                self._validate_mutation_arguments(name, params["arguments"])

    def _validate_mutation_arguments(self, name, args):
        """Raw dispatch must satisfy the same contract as typed order wrappers."""
        if type(args) is not dict or args.get("account_number") != self.account_number:
            raise MCPError("Order account does not match the explicit client account")
        if name == "place_option_order":
            required = {"account_number", "contract_symbol", "side", "quantity", "order_type",
                        "limit_price", "client_order_id"}
            if set(args) != required:
                raise MCPError("Unsupported option order arguments")
            self._order_args(args["contract_symbol"], args["side"], args["quantity"],
                             args["order_type"], args["limit_price"])
            if type(args["client_order_id"]) is not str or not args["client_order_id"]:
                raise MCPError("An explicit idempotency identifier is required")
        elif (set(args) != {"account_number", "order_id"}
              or type(args["order_id"]) is not str or not args["order_id"]):
            raise MCPError("Invalid cancel order arguments")

    @staticmethod
    def _check_response(response, request_id):
        if (type(response) is not dict or response.get("jsonrpc") != "2.0"
                or type(response.get("id")) is not type(request_id) or response.get("id") != request_id):
            raise MCPError("JSON-RPC response ID or envelope mismatch")
        if "error" in response:
            raise MCPError("MCP returned a JSON-RPC error")
        if "result" not in response:
            raise MCPError("MCP response has no result")
        return response

    def _parse_response_body(self, body, content_type, request_id):
        if len(body) > MAX_RESPONSE_BYTES:
            raise MCPError("MCP response exceeds size limit")
        try:
            text = body.decode("utf-8")
            if "text/event-stream" in (content_type or ""):
                matches = []
                for block in text.replace("\r\n", "\n").split("\n\n"):
                    lines = [line[5:].lstrip() for line in block.splitlines() if line.startswith("data:")]
                    if not lines:
                        continue
                    frame = _json_loads("\n".join(lines))
                    if isinstance(frame, dict) and frame.get("id") == request_id:
                        matches.append(frame)
                if len(matches) != 1:
                    raise MCPError("SSE response has no unique matching response ID")
                response = matches[0]
            else:
                response = _json_loads(text)
        except (ValueError, UnicodeError):
            raise MCPError("Invalid MCP JSON response") from None
        return self._check_response(response, request_id)

    def _post(self, payload, include_session=True):
        # This gate also covers direct _rpc/_post calls and optional helpers.
        self._validate_dispatch(payload)
        notification = "id" not in payload
        if self.transport is not None:
            self._validate_dispatch(payload)
            response = self.transport(payload)
            return None if notification else self._check_response(response, payload["id"])
        if self.mode != "live":
            raise MCPError("Paper mode is offline; supply an explicit test transport")
        req = urllib.request.Request(self.endpoint, data=json.dumps(payload, allow_nan=False).encode(),
                                     headers=self._request_headers(include_session), method="POST")
        # Fresh arm/kill check AFTER token retrieval and immediately before send.
        self._validate_dispatch(payload)
        try:
            with self._opener.open(req, timeout=30) as resp:
                if resp.geturl() != ENDPOINT or not 200 <= resp.status < 300:
                    raise MCPError("MCP redirect or unsuccessful response refused")
                body = resp.read(MAX_RESPONSE_BYTES + 1)
                if len(body) > MAX_RESPONSE_BYTES:
                    raise MCPError("MCP response exceeds size limit")
                result = None if notification else self._parse_response_body(
                    body, resp.headers.get("Content-Type", ""), payload["id"])
                session = resp.headers.get("Mcp-Session-Id")
                if session:
                    if len(session) > 256 or not session.isascii() or any(ord(c) < 33 or ord(c) > 126 for c in session):
                        raise MCPError("Invalid MCP session header")
                    self.session_id = session
                return result
        except urllib.error.HTTPError as exc:
            exc.close()
            if exc.code == 401:
                raise AuthError("MCP authorization failed") from None
            raise MCPError("MCP HTTP request failed") from None
        except (urllib.error.URLError, OSError, ValueError):
            raise MCPError("MCP transport failed") from None

    def _ensure_session(self):
        if self._initialized:
            return
        rid = next(self._id_counter)
        response = self._post({"jsonrpc": "2.0", "id": rid, "method": "initialize", "params": {
            "protocolVersion": MCP_PROTOCOL_VERSION, "capabilities": {},
            "clientInfo": {"name": "copypasta", "version": "0.2"}}}, include_session=False)
        result = response["result"]
        if not isinstance(result, dict) or result.get("protocolVersion") != MCP_PROTOCOL_VERSION:
            raise MCPError("Unsupported MCP protocol version")
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self._initialized = True

    def _rpc(self, method, params=None):
        if method == "tools/call":
            name = params.get("name") if type(params) is dict else None
            if name not in ALLOWED_TOOLS:
                raise CapabilityMissing("unapproved tool")
            if name in MUTATING_TOOLS:
                self._mutation_allowed()
        elif method != "tools/list":
            raise MCPError("Unapproved RPC method")
        self._ensure_session()
        payload = {"jsonrpc": "2.0", "id": next(self._id_counter), "method": method, "params": params or {}}
        return self._post(payload)["result"]

    def close(self):
        # Local teardown only; no hidden credential refresh or network operation.
        self.session_id = None
        self._initialized = False

    def list_tools(self):
        if self._tools_cache is None:
            result = self._rpc("tools/list", {})
            if not isinstance(result, dict) or type(result.get("tools")) is not list or result.get("nextCursor"):
                raise MCPError("Unsupported MCP tools listing")
            items = result["tools"]
            if any(type(t) is not dict or type(t.get("name")) is not str for t in items):
                raise MCPError("Invalid MCP tools listing")
            if len({t["name"] for t in items}) != len(items):
                raise MCPError("Duplicate MCP tool name")
            self._tools_cache = items
        return self._tools_cache

    def refresh_tools(self):
        self._tools_cache = self._bindings = None

    def _bind(self):
        names = {t["name"] for t in self.list_tools()}
        return {cap: name if name in names else None for cap, name in TOOL_NAMES.items()}

    def _tool_for(self, capability):
        name = self._bind().get(capability)
        if name is None:
            raise CapabilityMissing(capability)
        return name

    def check_options_support(self):
        bindings = self._bind()
        return {"options_orders": bool(bindings["place_option_order"]), "conditional_orders": False,
                "equities_orders": False, "live_trading_enabled": LIVE_TRADING_ENABLED,
                "tools_found": sorted(n for n in bindings.values() if n),
                "missing": [k for k, v in bindings.items() if v is None]}

    def call_tool(self, name, arguments):
        if name not in ALLOWED_TOOLS:
            raise CapabilityMissing("unapproved tool")
        if name in MUTATING_TOOLS:
            self._mutation_allowed()
        self.list_tools()
        return _tool_result(self._rpc("tools/call", {"name": name, "arguments": arguments}))

    def _account_args(self):
        return {"account_number": self.account_number} if self.account_number is not None else {}

    def find_option_contracts(self, underlying, expiry, strike, option_type):
        data = self.call_tool(self._tool_for("find_contracts"), {
            "underlying": underlying, "expiry": expiry, "strike": strike, "option_type": option_type})
        if type(data) is not list or any(
                type(x) is not dict or type(x.get("contract_symbol")) is not str or not x["contract_symbol"]
                or type(x.get("underlying")) is not str or type(x.get("expiry")) is not str
                or not _number(x.get("strike")) or x["strike"] <= 0 or x.get("option_type") not in {"call", "put"}
                for x in data):
            raise MCPError("Unsupported option instruments result")
        return data

    def get_option_quote(self, contract_symbol):
        data = self.call_tool(self._tool_for("option_quote"), {"contract_symbol": contract_symbol})
        if type(data) is list and len(data) == 1:
            data = data[0]
        if (type(data) is not dict or data.get("contract_symbol") != contract_symbol
                or not _number(data.get("ask")) or data["ask"] <= 0
                or not _number(data.get("bid")) or data["bid"] < 0 or data["bid"] > data["ask"]):
            raise MCPError("Unsupported or invalid option quote result")
        return data

    def _order_args(self, contract_symbol, side, qty, order_type, limit_price):
        if (type(contract_symbol) is not str or not contract_symbol or type(side) is not str or side not in {"buy", "sell"}
                or type(qty) is not int or qty <= 0 or order_type != "limit"
                or not _number(limit_price) or limit_price <= 0):
            raise MCPError("Unsupported option order arguments")
        return dict(self._account_args(), contract_symbol=contract_symbol, side=side,
                    quantity=qty, order_type=order_type, limit_price=limit_price)

    def review_option_order(self, contract_symbol, side, qty, order_type, limit_price=None, *, client_order_id=None):
        args = self._order_args(contract_symbol, side, qty, order_type, limit_price)
        if client_order_id is not None:
            if type(client_order_id) is not str or not client_order_id:
                raise MCPError("Invalid idempotency identifier")
            args["client_order_id"] = client_order_id
        data = self.call_tool(self._tool_for("review_option_order"), args)
        if type(data) is not dict or type(data.get("approved")) is not bool:
            raise MCPError("Unsupported review result: explicit boolean approval required")
        return data

    @staticmethod
    def normalize_order(data):
        statuses = {"pending", "partially_filled", "filled", "canceled", "rejected"}
        required = {"order_id", "client_order_id", "contract_symbol", "side", "quantity",
                    "status", "filled_qty", "avg_fill_price"}
        if (type(data) is not dict or not required <= data.keys()
                or type(data.get("order_id")) is not str or not data["order_id"]
                or data.get("status") not in statuses or type(data.get("filled_qty")) is not int
                or data["filled_qty"] < 0):
            raise MCPError("Unsupported order result; broker reconciliation required")
        if ("quantity" in data and (type(data["quantity"]) is not int or data["quantity"] <= 0
                                   or data["filled_qty"] > data["quantity"])):
            raise MCPError("Invalid order quantity or overfill")
        for key in ("contract_symbol", "client_order_id"):
            if key in data and (type(data[key]) is not str or not data[key]):
                raise MCPError("Invalid order identity")
        if "side" in data and data["side"] not in {"buy", "sell"}:
            raise MCPError("Invalid order side")
        price = data.get("avg_fill_price")
        if data["filled_qty"] > 0 and (not _number(price) or price <= 0):
            raise MCPError("Filled order missing a valid fill price")
        if data["status"] == "filled" and data["filled_qty"] != data["quantity"]:
            raise MCPError("Filled order does not have its full confirmed quantity")
        if data["status"] == "partially_filled" and not 0 < data["filled_qty"] < data["quantity"]:
            raise MCPError("Partial order has an inconsistent filled quantity")
        if data["status"] == "rejected" and data["filled_qty"]:
            raise MCPError("Rejected order has unexpected fills")
        if data["filled_qty"] == 0 and not (price is None or (_number(price) and price == 0)):
            raise MCPError("Unfilled order has inconsistent fill price")
        return dict(data)

    def place_option_order(self, contract_symbol, side, qty, order_type, limit_price=None, *, client_order_id=None):
        self._mutation_allowed()
        args = self._order_args(contract_symbol, side, qty, order_type, limit_price)
        if type(client_order_id) is not str or not client_order_id:
            raise MCPError("An explicit idempotency identifier is required")
        args["client_order_id"] = client_order_id
        result = self.normalize_order(self.call_tool(self._tool_for("place_option_order"), args))
        if ("account_number" in result and result["account_number"] != self.account_number):
            raise MCPError("Order response account mismatch; reconciliation required")
        if result["filled_qty"] > qty or any(
                key in result and result[key] != args[key]
                for key in ("contract_symbol", "side", "quantity", "client_order_id")):
            raise MCPError("Order response does not match the submitted intent; reconciliation required")
        return result

    def cancel_order(self, order_id):
        self._mutation_allowed()
        if type(order_id) is not str or not order_id:
            raise MCPError("Invalid order identifier")
        return self.normalize_order(self.call_tool(self._tool_for("cancel_order"),
                                    dict(self._account_args(), order_id=order_id)))

    def get_positions(self):
        data = self.call_tool(self._tool_for("positions"), self._account_args())
        if type(data) is not list or any(
                type(x) is not dict or type(x.get("contract_symbol")) is not str or not x["contract_symbol"]
                or type(x.get("quantity")) is not int or x["quantity"] < 0
                or ("account_number" in x and x["account_number"] != self.account_number)
                for x in data):
            raise MCPError("Unsupported positions result")
        return data

    def get_orders(self, status=None):
        args = self._account_args()
        if status is not None:
            args["status"] = status
        data = self.call_tool(self._tool_for("orders"), args)
        if type(data) is not list:
            raise MCPError("Unsupported orders result")
        orders = [self.normalize_order(x) for x in data]
        if any("account_number" in x and x["account_number"] != self.account_number for x in orders):
            raise MCPError("Orders snapshot account mismatch")
        return orders
