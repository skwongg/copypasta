"""Fail-closed MCP adapter for the Robinhood agentic trading API.

Read-only tool schemas (tools/list, get_accounts, get_option_chains,
get_option_instruments, get_option_quotes, review_option_order,
get_option_positions, get_option_orders) were verified against the live
endpoint on 2026-09-22. Order placement/cancellation response shapes are
defensively normalized and fail closed: anything unexpected raises MCPError
instead of being guessed at.

LIVE_TRADING_ENABLED deliberately blocks broker mutations until the separate
activation review is completed. Paper mode performs no network or credential
access.
"""
from __future__ import annotations

import itertools
import json
import math
import re
import ssl
import urllib.error
import urllib.request
import uuid

ENDPOINT = "https://agent.robinhood.com/mcp/trading"
MCP_PROTOCOL_VERSION = "2025-03-26"
LIVE_TRADING_ENABLED = True  # Enabled 2026-09-22 after Silas's explicit approval;
# the adapter still requires the ARMED marker, a configured source allowlist,
# market hours, and the kill-switch clear before any mutation.
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
TOOL_NAMES = {
    "get_accounts": "get_accounts",
    "chains": "get_option_chains",
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

_OPTION_LEVELS = frozenset({"option_level_2", "option_level_3"})
_ORDER_STATES = {
    "queued": "pending",
    "confirmed": "pending",
    "pending_cancelled": "pending",
    "partially_filled": "partially_filled",
    "filled": "filled",
    "cancelled": "canceled",
    "rejected": "rejected",
    "failed": "rejected",
    "voided": "rejected",
}
_REF_ID_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "copypasta:order-ref-id")


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


def _uuid(value, field="id"):
    if type(value) is not str:
        raise MCPError("Invalid UUID identifier")
    try:
        uuid.UUID(value)
    except ValueError:
        raise MCPError("Invalid UUID identifier") from None
    return value


def _parse_price(value):
    if type(value) is not str or not value.strip():
        raise MCPError("Invalid price string")
    try:
        number = float(value)
    except ValueError:
        raise MCPError("Invalid price string") from None
    if not math.isfinite(number):
        raise MCPError("Invalid price string")
    return number


def _parse_int(value):
    if type(value) is int:
        number = value
    elif type(value) is str and re.fullmatch(r"\d+", value.strip()):
        number = int(value.strip())
    else:
        raise MCPError("Invalid integer string")
    return number


def _occ_symbol(chain_symbol, expiration_date, option_type, strike_price):
    """Construct the OCC option symbol from instrument fields.

    e.g. ("SPY", "2026-09-22", "put", "550.0000") -> "SPY   260922P00550000".
    """
    if (type(chain_symbol) is not str or not 0 < len(chain_symbol) <= 6
            or type(expiration_date) is not str
            or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", expiration_date)
            or option_type not in {"call", "put"}):
        raise MCPError("Cannot construct OCC symbol")
    try:
        millis = int(round(float(strike_price) * 1000))
    except (TypeError, ValueError):
        raise MCPError("Cannot construct OCC symbol") from None
    if millis <= 0 or millis > 99999999:
        raise MCPError("Cannot construct OCC symbol")
    yymmdd = expiration_date[2:4] + expiration_date[5:7] + expiration_date[8:10]
    return f"{chain_symbol:<6s}{yymmdd}{'C' if option_type == 'call' else 'P'}{millis:08d}"


def _unwrap_envelope(data):
    """Tool results arrive as {"data": ..., "guide": "..."}; normalize from data."""
    if isinstance(data, dict) and "guide" in data and "data" in data:
        inner = data["data"]
        if isinstance(inner, (dict, list)):
            return inner
        raise MCPError("Unsupported MCP result envelope")
    return data


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


_TYPE_PREDICATES = {
    "object": lambda x: type(x) is dict,
    "array": lambda x: type(x) is list,
    "string": lambda x: type(x) is str,
    "integer": lambda x: type(x) is int,
    "number": _number,
    "boolean": lambda x: type(x) is bool,
    "null": lambda x: x is None,
}


def _effective_kind(schema, value):
    """Resolve union type lists (e.g. ["null", "array"]); None matches "null"."""
    kind = schema.get("type")
    names = kind if isinstance(kind, list) else [kind]
    if any(type(n) is not str or n not in _TYPE_PREDICATES for n in names):
        raise MCPError("Unsupported MCP input schema")
    matches = [n for n in names if _TYPE_PREDICATES[n](value)]
    if len(matches) != 1:
        raise MCPError("MCP argument type does not match supported schema")
    return matches[0]


def _validate_schema(schema, value):
    """Small, explicit JSON Schema subset; unsupported schemas fail closed."""
    allowed = {"type", "properties", "required", "additionalProperties", "items",
               "enum", "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
               "minLength", "maxLength", "minItems", "maxItems", "description", "title"}
    if not isinstance(schema, dict) or set(schema) - allowed:
        raise MCPError("Unsupported MCP input schema")
    kind = _effective_kind(schema, value)
    if "enum" in schema and value not in schema["enum"]:
        raise MCPError("MCP argument outside allowed enumeration")
    if kind == "object":
        props = schema.get("properties") or {}
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
    # "null" and scalar kinds carry no nested constraints.
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
    names = kind if isinstance(kind, list) else [kind]
    if (set(schema) - allowed or not names
            or any(type(n) is not str or n not in _TYPE_PREDICATES for n in names)):
        raise MCPError("Unsupported MCP input schema")
    if "enum" in schema and (not isinstance(schema["enum"], list) or not schema["enum"]):
        raise MCPError("Unsupported MCP enumeration")
    if "object" in names:
        props = schema.get("properties") or {}
        req = schema.get("required", [])
        if (not isinstance(props, dict) or not isinstance(req, list)
                or any(type(k) is not str or k not in props for k in req)
                or schema.get("additionalProperties", False) is not False):
            raise MCPError("Unsupported MCP object schema")
        for child in props.values():
            _check_schema_definition(child)
    elif "properties" in schema or "required" in schema or "additionalProperties" in schema:
        raise MCPError("Unsupported MCP schema constraint")
    if "array" in names:
        _check_schema_definition(schema.get("items"))
    elif "items" in schema:
        raise MCPError("Unsupported MCP schema constraint")
    for key in ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum"):
        if key in schema and (not ({"integer", "number"} & set(names)) or not _number(schema[key])):
            raise MCPError("Unsupported MCP numeric schema")
    for key, expected in (("minLength", "string"), ("maxLength", "string"),
                           ("minItems", "array"), ("maxItems", "array")):
        if key in schema and (expected not in names or type(schema[key]) is not int or schema[key] < 0):
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
    return _unwrap_envelope(data)


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
        self._instrument_cache = {}
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
            required = {"account_number", "legs", "quantity"}
            optional = {"type", "direction", "price", "stop_price", "time_in_force",
                        "market_hours", "ref_id"}
            if not required <= set(args) or set(args) - (required | optional):
                raise MCPError("Unsupported option order arguments")
            self._check_leg_args(args["legs"])
            qty = _parse_int(args["quantity"])
            if qty <= 0:
                raise MCPError("Invalid order quantity")
            if args.get("type", "limit") not in {"limit", "market", "stop_limit", "stop_market"}:
                raise MCPError("Unsupported order type")
            if "price" in args and _parse_price(args["price"]) <= 0:
                raise MCPError("Invalid limit price")
            if "stop_price" in args and _parse_price(args["stop_price"]) <= 0:
                raise MCPError("Invalid stop price")
            if args.get("time_in_force", "gfd") not in {"gfd", "gtc"}:
                raise MCPError("Unsupported time in force")
            if "ref_id" in args:
                _uuid(args["ref_id"], "ref_id")
        elif (set(args) != {"account_number", "order_id"}
              or type(args["order_id"]) is not str):
            raise MCPError("Invalid cancel order arguments")
        else:
            _uuid(args["order_id"], "order_id")

    @staticmethod
    def _check_leg_args(legs):
        if type(legs) is not list or len(legs) != 1 or type(legs[0]) is not dict:
            raise MCPError("Only single-leg orders are supported")
        leg = legs[0]
        if set(leg) - {"option_id", "side", "position_effect", "ratio_quantity"}:
            raise MCPError("Unsupported leg arguments")
        _uuid(leg.get("option_id"), "option_id")
        if leg.get("side") not in {"buy", "sell"}:
            raise MCPError("Invalid leg side")
        if leg.get("position_effect") not in {"open", "close"}:
            raise MCPError("Invalid leg position effect")
        if "ratio_quantity" in leg and (type(leg["ratio_quantity"]) is not int
                                        or leg["ratio_quantity"] != 1):
            raise MCPError("Single-leg ratio_quantity must be 1")

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
            "clientInfo": {"name": "copypasta", "version": "0.3"}}}, include_session=False)
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
        return {"options_orders": bool(bindings["place_option_order"]),
                "review_option_order": bool(bindings["review_option_order"]),
                "account_discovery": bool(bindings["get_accounts"]),
                "option_chains": bool(bindings["chains"]),
                "conditional_orders": False,
                "equities_orders": False,
                "live_trading_enabled": LIVE_TRADING_ENABLED,
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

    # ------------------------------------------------------------------
    # Account discovery. The account number is resolved at runtime and is
    # never hardcoded, logged, or persisted by this module.
    # ------------------------------------------------------------------
    def discover_agentic_account(self):
        data = self.call_tool(self._tool_for("get_accounts"), {})
        accounts = data.get("accounts") if isinstance(data, dict) else None
        if accounts is None and isinstance(data, list):
            accounts = data
        if not isinstance(accounts, list):
            raise MCPError("Unsupported accounts result")
        candidates = []
        for acct in accounts:
            if not isinstance(acct, dict):
                raise MCPError("Unsupported accounts result")
            number = acct.get("account_number")
            if (acct.get("agentic_allowed") is True
                    and acct.get("option_level") in _OPTION_LEVELS
                    and type(number) is str and number):
                candidates.append(number)
        if len(candidates) != 1:
            raise MCPError("Agentic account is not uniquely resolvable")
        return candidates[0]

    # ------------------------------------------------------------------
    # Contract resolution: chains -> instruments -> normalized contracts.
    # ------------------------------------------------------------------
    @staticmethod
    def _instrument_items(data):
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ("instruments", "results", "options"):
                items = data.get(key)
                if isinstance(items, list):
                    return items
        raise MCPError("Unsupported option instruments result")

    def _get_instrument(self, option_id):
        _uuid(option_id, "option_id")
        cached = self._instrument_cache.get(option_id)
        if cached is not None:
            return cached
        data = self.call_tool(self._tool_for("find_contracts"), {"ids": option_id})
        items = self._instrument_items(data)
        matches = [i for i in items if isinstance(i, dict) and i.get("id") == option_id]
        if len(matches) != 1:
            raise MCPError("Option instrument lookup did not return a unique contract")
        self._instrument_cache[option_id] = matches[0]
        return matches[0]

    def _occ_for_option(self, option_id):
        item = self._get_instrument(option_id)
        return _occ_symbol(item.get("chain_symbol"), item.get("expiration_date"),
                           item.get("type"), item.get("strike_price"))

    def find_option_contracts(self, underlying, expiry, strike, option_type):
        if (type(underlying) is not str or not underlying
                or type(expiry) is not str or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", expiry)
                or not _number(strike) or strike <= 0
                or option_type not in {"call", "put"}):
            raise MCPError("Invalid contract search parameters")
        data = self.call_tool(self._tool_for("chains"), {"underlying_symbol": underlying})
        if not isinstance(data, dict) or not isinstance(data.get("chains"), list):
            raise MCPError("Unsupported option chains result")
        matching = [c for c in data["chains"]
                    if isinstance(c, dict) and isinstance(c.get("expiration_dates"), list)
                    and expiry in c["expiration_dates"]
                    and type(c.get("id")) is str and c["id"]]
        if not matching:
            raise MCPError("No option chain covers the requested expiry")
        openable = [c for c in matching if c.get("can_open_position") is True]
        candidates = openable or matching
        if len(candidates) != 1:
            raise MCPError("Ambiguous option chain for expiry")
        chain_id = candidates[0]["id"]
        items = self._instrument_items(self.call_tool(self._tool_for("find_contracts"), {
            "chain_id": chain_id, "expiration_dates": expiry,
            "strike_price": f"{float(strike):.4f}", "type": option_type}))
        contracts = []
        for item in items:
            if not isinstance(item, dict):
                raise MCPError("Unsupported option instruments result")
            try:
                strike_value = float(item["strike_price"])
            except (KeyError, TypeError, ValueError):
                raise MCPError("Unsupported option instruments result") from None
            if (item.get("expiration_date") != expiry or item.get("type") != option_type
                    or abs(strike_value - float(strike)) > 1e-9
                    or item.get("state") != "active" or item.get("tradability") != "tradable"):
                continue
            option_id = _uuid(item.get("id"), "option_id")
            contracts.append({
                "option_id": option_id,
                "contract_symbol": _occ_symbol(item.get("chain_symbol"), expiry,
                                              option_type, item["strike_price"]),
                "underlying": item.get("chain_symbol"),
                "expiry": expiry,
                "strike": strike_value,
                "option_type": option_type,
                "tradability": item.get("tradability"),
                "multiplier": _parse_price(item["trade_value_multiplier"]),
            })
            self._instrument_cache[option_id] = item
        if not contracts:
            raise MCPError("No tradable contract matches the requested terms")
        return contracts

    # ------------------------------------------------------------------
    # Quotes: nested real shape normalized to the engine's flat contract.
    # ------------------------------------------------------------------
    @staticmethod
    def _normalize_quote(option_id, entry):
        if not isinstance(entry, dict) or not isinstance(entry.get("quote"), dict):
            raise MCPError("Unsupported option quote result")
        quote = entry["quote"]
        if quote.get("instrument_id") != option_id:
            raise MCPError("Quote instrument mismatch")
        ask = _parse_price(quote.get("ask_price"))
        bid = _parse_price(quote.get("bid_price"))
        as_of = quote.get("updated_at")
        if not ask > 0 or not 0 <= bid <= ask:
            raise MCPError("Invalid option quote prices")
        if type(as_of) is not str or not as_of:
            raise MCPError("Option quote missing timestamp")
        mark = None
        if quote.get("mark_price") not in (None, ""):
            mark = _parse_price(quote.get("mark_price"))
        return {"option_id": option_id, "bid": bid, "ask": ask,
                "as_of": as_of, "mark": mark}

    def get_option_quote(self, option_id):
        _uuid(option_id, "option_id")
        data = self.call_tool(self._tool_for("option_quote"), {"instrument_ids": [option_id]})
        if not isinstance(data, dict) or not isinstance(data.get("results"), list):
            raise MCPError("Unsupported option quotes result")
        matches = [self._normalize_quote(option_id, e) for e in data["results"]
                   if isinstance(e, dict) and isinstance(e.get("quote"), dict)
                   and e["quote"].get("instrument_id") == option_id]
        if len(matches) != 1:
            raise MCPError("Quote lookup did not return a unique quote")
        quote = matches[0]
        quote["contract_symbol"] = self._occ_for_option(option_id)
        return quote

    # ------------------------------------------------------------------
    # Review (simulation) and order placement.
    # ------------------------------------------------------------------
    @staticmethod
    def make_ref_id(intent_key):
        """Deterministic UUID idempotency key per logical order.

        Fresh per logical order; retries of the same intent reuse it, which is
        exactly the broker's ref_id contract.
        """
        if type(intent_key) is not str or not intent_key:
            raise MCPError("Invalid intent key for ref_id")
        return str(uuid.uuid5(_REF_ID_NAMESPACE, "copypasta:" + intent_key))

    def _review_or_place_args(self, option_id, side, position_effect, qty, order_type,
                              limit_price, time_in_force):
        _uuid(option_id, "option_id")
        if side not in {"buy", "sell"}:
            raise MCPError("Invalid order side")
        if position_effect not in {"open", "close"}:
            raise MCPError("Invalid position effect")
        qty = _parse_int(qty)
        if qty <= 0:
            raise MCPError("Invalid order quantity")
        if order_type != "limit":
            raise MCPError("Only limit orders are supported")
        if not _number(limit_price) or limit_price <= 0:
            raise MCPError("Invalid limit price")
        if time_in_force not in {"gfd", "gtc"}:
            raise MCPError("Unsupported time in force")
        args = dict(self._account_args(),
                    legs=[{"option_id": option_id, "side": side,
                           "position_effect": position_effect}],
                    quantity=str(qty), type="limit",
                    price=f"{float(limit_price):.2f}", time_in_force=time_in_force)
        return args

    def review_option_order(self, *, option_id, side, position_effect, qty, order_type,
                            limit_price, underlying, underlying_type="equity"):
        """Simulate the order. approved=True only when the broker raises no alerts."""
        if type(underlying) is not str or not underlying:
            raise MCPError("Invalid underlying for review")
        if underlying_type not in {"equity", "index"}:
            raise MCPError("Invalid underlying type for review")
        args = self._review_or_place_args(option_id, side, position_effect, qty,
                                          order_type, limit_price, "gfd")
        args["chain_symbol"] = underlying
        args["underlying_type"] = underlying_type
        data = self.call_tool(self._tool_for("review_option_order"), args)
        if not isinstance(data, dict) or "order_checks" not in data:
            raise MCPError("Unsupported review result")
        checks = data["order_checks"]
        if checks is None:
            checks = {}
        if not isinstance(checks, dict):
            raise MCPError("Unsupported review result")
        alerts = [checks] if checks else []
        fees = data.get("fees")
        return {"approved": not alerts,
                "alerts": alerts,
                "fees": fees if isinstance(fees, dict) else {},
                "raw": data}

    @staticmethod
    def normalize_order(data):
        """Single normalization entry point for place/cancel/order snapshots.

        Normalized shape: {"order_id", "ref_id", "option_id", "contract_symbol",
        "side", "quantity", "status", "filled_qty", "avg_fill_price"}. The broker
        row shapes for fills are unverified against a real trade, so anything
        unexpected fails closed here.
        """
        data = _unwrap_envelope(data)
        if not isinstance(data, dict):
            raise MCPError("Unsupported order result; broker reconciliation required")
        order_id = _uuid(data.get("id", data.get("order_id")), "order_id")
        ref_id = data.get("ref_id")
        if ref_id is not None:
            _uuid(ref_id, "ref_id")
        state = data.get("state", data.get("status"))
        if type(state) is not str or state not in _ORDER_STATES:
            raise MCPError("Unsupported order state; broker reconciliation required")
        status = _ORDER_STATES[state]
        quantity = _parse_int(data.get("quantity"))
        if quantity <= 0:
            raise MCPError("Invalid order quantity")
        filled_qty = _parse_int(data.get("processed_quantity", data.get("filled_qty", 0)))
        if not 0 <= filled_qty <= quantity:
            raise MCPError("Invalid cumulative fill quantity")
        avg_fill_price = None
        for key in ("avg_fill_price", "average_price", "filled_price", "premium"):
            if data.get(key) not in (None, ""):
                avg_fill_price = _parse_price(data[key])
                break
        if filled_qty > 0 and not (avg_fill_price is not None and avg_fill_price > 0):
            raise MCPError("Filled order missing a valid fill price")
        if filled_qty == 0 and avg_fill_price not in (None, 0):
            raise MCPError("Unfilled order has inconsistent fill price")
        if status == "filled" and filled_qty != quantity:
            raise MCPError("Filled order does not have its full confirmed quantity")
        if status == "partially_filled" and not 0 < filled_qty < quantity:
            raise MCPError("Partial order has an inconsistent filled quantity")
        if status == "rejected" and filled_qty:
            raise MCPError("Rejected order has unexpected fills")
        option_id = None
        side = data.get("side")
        legs = data.get("legs")
        if isinstance(legs, list) and legs:
            if len(legs) != 1 or not isinstance(legs[0], dict):
                raise MCPError("Only single-leg orders are supported")
            option_id = _uuid(legs[0].get("option_id"), "option_id")
            side = legs[0].get("side", side)
        if side not in {"buy", "sell"}:
            raise MCPError("Invalid order side")
        contract_symbol = data.get("contract_symbol")
        if contract_symbol is not None and (type(contract_symbol) is not str or not contract_symbol):
            raise MCPError("Invalid order contract symbol")
        return {"order_id": order_id, "ref_id": ref_id, "option_id": option_id,
                "contract_symbol": contract_symbol, "side": side, "quantity": quantity,
                "status": status, "filled_qty": filled_qty,
                "avg_fill_price": avg_fill_price}

    def _resolve_order_symbol(self, order):
        if order.get("contract_symbol"):
            return order
        if order.get("option_id"):
            order = dict(order, contract_symbol=self._occ_for_option(order["option_id"]))
        return order

    def place_option_order(self, *, option_id, side, position_effect, qty, order_type,
                           limit_price, ref_id, time_in_force="gfd"):
        self._mutation_allowed()
        _uuid(ref_id, "ref_id")
        args = self._review_or_place_args(option_id, side, position_effect, qty,
                                          order_type, limit_price, time_in_force)
        args["ref_id"] = ref_id
        result = self.normalize_order(self.call_tool(self._tool_for("place_option_order"), args))
        if "account_number" in result and result["account_number"] != self.account_number:
            raise MCPError("Order response account mismatch; reconciliation required")
        if (result["ref_id"] is not None and result["ref_id"] != ref_id):
            raise MCPError("Order response ref_id mismatch; reconciliation required")
        if result["option_id"] is not None and result["option_id"] != option_id:
            raise MCPError("Order response contract mismatch; reconciliation required")
        if result["side"] != side or result["quantity"] != _parse_int(args["quantity"]):
            raise MCPError("Order response does not match the submitted intent; reconciliation required")
        if result["filled_qty"] > _parse_int(args["quantity"]):
            raise MCPError("Order overfill; reconciliation required")
        return self._resolve_order_symbol(result)

    def cancel_order(self, order_id):
        self._mutation_allowed()
        _uuid(order_id, "order_id")
        result = self.normalize_order(self.call_tool(self._tool_for("cancel_order"),
                                                     dict(self._account_args(), order_id=order_id)))
        if result["order_id"] != order_id:
            raise MCPError("Cancel response order mismatch; reconciliation required")
        return self._resolve_order_symbol(result)

    # ------------------------------------------------------------------
    # Positions and order snapshots.
    # ------------------------------------------------------------------
    def get_positions(self):
        data = self.call_tool(self._tool_for("positions"),
                              dict(self._account_args(), nonzero=True))
        if not isinstance(data, dict) or not isinstance(data.get("positions"), list):
            raise MCPError("Unsupported positions result")
        option_ids = []
        rows = []
        for row in data["positions"]:
            if not isinstance(row, dict):
                raise MCPError("Unsupported positions result")
            if "account_number" in row and row["account_number"] != self.account_number:
                raise MCPError("Positions snapshot account mismatch")
            quantity = _parse_int(row.get("quantity", 0))
            if quantity < 0:
                raise MCPError("Invalid position quantity")
            avg_price = None
            if row.get("average_price") not in (None, ""):
                avg_price = _parse_price(row["average_price"])
            pending = 0
            for key, value in row.items():
                if key.startswith("pending_") and value not in (None, ""):
                    pending += _parse_int(value)
            option_id = None
            for key in ("option_id", "instrument_id", "id"):
                if isinstance(row.get(key), str):
                    try:
                        option_id = _uuid(row[key], "option_id")
                        break
                    except MCPError:
                        continue
            contract_symbol = None
            strike = row.get("strike_price", row.get("strike"))
            if (type(row.get("chain_symbol")) is str and type(row.get("expiration_date")) is str
                    and row.get("type") in {"call", "put"} and strike not in (None, "")):
                try:
                    contract_symbol = _occ_symbol(row["chain_symbol"], row["expiration_date"],
                                                 row["type"], strike)
                except MCPError:
                    contract_symbol = None
            if contract_symbol is None and option_id is not None:
                option_ids.append(option_id)
            rows.append({"option_id": option_id, "contract_symbol": contract_symbol,
                         "quantity": quantity, "avg_price": avg_price, "pending_qty": pending})
        if option_ids:
            for option_id in dict.fromkeys(option_ids):
                try:
                    symbol = self._occ_for_option(option_id)
                except MCPError:
                    continue
                for row in rows:
                    if row["option_id"] == option_id and row["contract_symbol"] is None:
                        row["contract_symbol"] = symbol
        return rows

    def get_orders(self, status=None):
        args = self._account_args()
        want_open = False
        if status is not None:
            if status == "open":
                want_open = True  # no server-side "open" filter; filter client-side below
            elif type(status) is str and status in _ORDER_STATES:
                args["state"] = status
            else:
                raise MCPError("Unsupported order status filter")
        data = self.call_tool(self._tool_for("orders"), args)
        if not isinstance(data, dict) or not isinstance(data.get("orders"), list):
            raise MCPError("Unsupported orders result")
        orders = []
        for row in data["orders"]:
            order = self.normalize_order(row)
            if "account_number" in row and row["account_number"] != self.account_number:
                raise MCPError("Orders snapshot account mismatch")
            if want_open and order["status"] not in {"pending", "partially_filled"}:
                continue
            orders.append(self._resolve_order_symbol(order))
        return orders
