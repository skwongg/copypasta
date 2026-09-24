"""Stateful fake of the Robinhood agentic MCP endpoint.

Tool input schemas and response shapes are copied from live read-only calls
made 2026-09-24 (get_accounts, get_option_chains, get_option_instruments,
get_option_quotes, review_option_order, get_option_orders). Order rows from
get_option_orders carry NO ref_id field — verified on real rows.
"""
import json
import uuid

ACCOUNT = "AGENTIC1"
CHAIN_ID = "c277b118-58d9-4060-8dc5-a3b5898955cb"
OPTION_ID = "81ac5b34-7ceb-497d-9308-5b72e8fb308d"
EXPIRY = "2026-09-24"
STRIKE = "769.0000"

S = {"type": "string"}
LEG = {"type": "object", "additionalProperties": False, "required": ["option_id", "side", "position_effect"],
       "properties": {"option_id": S, "position_effect": S, "ratio_quantity": {"type": "integer"}, "side": S}}
LEGS = {"type": ["null", "array"], "items": LEG}


def _obj(props, required=()):
    return {"type": "object", "additionalProperties": False, "properties": props, "required": list(required)}


TOOLS = [
    {"name": "get_accounts", "inputSchema": {"type": "object", "additionalProperties": False}},
    {"name": "get_option_chains", "inputSchema": _obj({"ids": S, "underlying_symbol": S})},
    {"name": "get_option_instruments", "inputSchema": _obj({
        "chain_id": S, "chain_symbol": S, "cursor": S, "expiration_dates": S, "ids": S, "state": S,
        "strike_price": S, "tradability": S, "type": S})},
    {"name": "get_option_quotes", "inputSchema": _obj({"instrument_ids": {"type": ["null", "array"], "items": S}},
                                                      ["instrument_ids"])},
    {"name": "review_option_order", "inputSchema": _obj({
        "account_number": S, "chain_symbol": S, "direction": S, "legs": LEGS, "market_hours": S, "price": S,
        "quantity": S, "stop_price": S, "time_in_force": S, "type": S, "underlying_type": S},
        ["account_number", "legs", "quantity"])},
    {"name": "place_option_order", "inputSchema": _obj({
        "account_number": S, "direction": S, "legs": LEGS, "market_hours": S, "price": S, "quantity": S,
        "ref_id": S, "stop_price": S, "time_in_force": S, "type": S}, ["account_number", "legs", "quantity"])},
    {"name": "cancel_option_order", "inputSchema": _obj({"account_number": S, "order_id": S},
                                                        ["account_number", "order_id"])},
    {"name": "get_option_positions", "inputSchema": _obj({
        "account_number": S, "chain_ids": S, "cursor": S, "expiration_date": S, "expiration_date_gte": S,
        "expiration_date_lte": S, "nonzero": {"type": "boolean"}, "option_ids": S, "option_type": S, "type": S},
        ["account_number"])},
    {"name": "get_option_orders", "inputSchema": _obj({
        "account_number": S, "chain_ids": S, "created_at_gte": S, "cursor": S, "order_id": S,
        "placed_agent": S, "state": S, "underlying_type": S}, ["account_number"])},
]


def _dec(n):
    return f"{n:.5f}"


class FakeRobinhood:
    def __init__(self, clock):
        self.clock = clock              # callable -> aware datetime
        self.bid, self.ask = 0.32, 0.33
        self.place_state = "filled"     # state the place response reports
        self.place_includes_ref_id = True   # unknown for real place responses; list rows never have it
        self.orders = []                # internal rows (with our private _ref_id)
        self.positions = {}             # option_id -> qty
        self.placed = []                # audit of place calls

    # ---- broker-side events -------------------------------------------------
    def fill(self, order_id, price=None):
        row = next(o for o in self.orders if o["id"] == order_id)
        qty = float(row["quantity"])
        px = price if price is not None else float(row["price"])
        row.update(state="filled", processed_quantity=_dec(qty), pending_quantity=_dec(0))
        row["legs"][0]["executions"] = [{"id": str(uuid.uuid4()), "price": f"{px:.8f}", "quantity": _dec(qty)}]
        leg = row["legs"][0]
        delta = qty if leg["side"] == "buy" else -qty
        self.positions[leg["option_id"]] = self.positions.get(leg["option_id"], 0) + int(delta)

    def manual_close_all(self):
        self.positions = {}

    # ---- MCP transport --------------------------------------------------------
    def __call__(self, payload):
        method = payload["method"]
        if method == "notifications/initialized":
            return None
        if method == "initialize":
            result = {"protocolVersion": "2025-03-26"}
        elif method == "tools/list":
            result = {"tools": TOOLS}
        else:
            name, args = payload["params"]["name"], payload["params"]["arguments"]
            data = getattr(self, "t_" + name)(args)
            text = json.dumps({"data": data, "guide": "fake guide text"})
            result = {"content": [{"type": "text", "text": text}], "isError": False}
        return {"jsonrpc": "2.0", "id": payload["id"], "result": result}

    def t_get_accounts(self, _):
        return {"accounts": [
            {"account_number": "MAIN0001", "agentic_allowed": False, "option_level": "option_level_2"},
            {"account_number": ACCOUNT, "agentic_allowed": True, "option_level": "option_level_2", "type": "cash"}]}

    def t_get_option_chains(self, args):
        assert args == {"underlying_symbol": "SPY"}, args
        return {"chains": [{"id": CHAIN_ID, "symbol": "SPY", "can_open_position": True,
                            "expiration_dates": [EXPIRY, "2026-09-25"], "trade_value_multiplier": "100.0000"}]}

    def _instrument(self):
        return {"id": OPTION_ID, "chain_id": CHAIN_ID, "chain_symbol": "SPY", "underlying_type": "equity",
                "expiration_date": EXPIRY, "strike_price": STRIKE, "type": "call", "state": "active",
                "tradability": "tradable", "trade_value_multiplier": "100.0000"}

    def t_get_option_instruments(self, args):
        if args.get("ids") == OPTION_ID:
            return {"instruments": [self._instrument()]}
        if (args.get("chain_id"), args.get("expiration_dates"), args.get("strike_price"), args.get("type")) == \
                (CHAIN_ID, EXPIRY, STRIKE, "call"):
            return {"instruments": [self._instrument()]}
        return {"instruments": []}

    def t_get_option_quotes(self, args):
        ts = self.clock().strftime("%Y-%m-%dT%H:%M:%S.%f") + "248Z"   # real: 9 fractional digits + Z
        return {"results": [{"quote": {"instrument_id": i, "ask_price": f"{self.ask:.6f}",
                                       "bid_price": f"{self.bid:.6f}", "mark_price": f"{(self.bid + self.ask) / 2:.6f}",
                                       "updated_at": ts}} for i in args["instrument_ids"] if i == OPTION_ID]}

    def t_review_option_order(self, args):
        return dict(args, direction="debit", order_checks={}, fees={"total_fee": "0.04"})

    def t_place_option_order(self, args):
        assert args["account_number"] == ACCOUNT
        leg = args["legs"][0]
        row = {"id": str(uuid.uuid4()), "chain_id": CHAIN_ID, "chain_symbol": "SPY", "state": "confirmed",
               "type": "limit", "trigger": "immediate", "quantity": _dec(int(args["quantity"])),
               "processed_quantity": _dec(0), "pending_quantity": _dec(int(args["quantity"])),
               "price": f"{float(args['price']):.8f}", "premium": f"{float(args['price']) * 100:.8f}",
               "time_in_force": args.get("time_in_force", "gfd"), "placed_agent": "agentic",
               "created_at": self.clock().isoformat().replace("+00:00", "Z"),
               "legs": [{"id": str(uuid.uuid4()), "option_id": leg["option_id"], "side": leg["side"],
                         "position_effect": leg["position_effect"], "ratio_quantity": 1,
                         "expiration_date": EXPIRY, "strike_price": STRIKE, "option_type": "call"}],
               "_ref_id": args.get("ref_id")}
        self.orders.insert(0, row)
        self.placed.append(dict(args))
        if self.place_state == "filled":
            self.fill(row["id"])
        return {"order": self._public(row, place=True)}

    def _public(self, row, place=False):
        out = {k: v for k, v in row.items() if k != "_ref_id"}
        if place and self.place_includes_ref_id:
            out["ref_id"] = row["_ref_id"]
        return json.loads(json.dumps(out))

    def t_cancel_option_order(self, args):
        row = next(o for o in self.orders if o["id"] == args["order_id"])
        row["state"] = "cancelled"
        return {"order": self._public(row)}

    def t_get_option_positions(self, args):
        return {"positions": [{"option_id": oid, "chain_symbol": "SPY", "expiration_date": EXPIRY, "type": "call",
                               "strike_price": STRIKE, "quantity": _dec(q), "average_price": "33.0000"}
                              for oid, q in self.positions.items() if q]}

    def t_get_option_orders(self, args):
        rows = self.orders
        if "state" in args:
            rows = [r for r in rows if r["state"] == args["state"]]
        return {"orders": [self._public(r) for r in rows]}   # real list rows: no ref_id
