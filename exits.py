"""Automated exit ladder + position monitor for the Robinhood copy-trader.

Per 2026-09-21 decision with Silas: exit levels are computed from OUR fill
price, per position.

Exit policy (evaluated on every poll, in this order per position):
  * Stop loss: premium ratio <= 0.40 (-60%) -> sell ALL at market, latch sl,
    close the position.
  * Take-profit ladder, each rung a one-time latch, market orders:
      tp50  (ratio >= 1.50) -> sell floor(qty_remaining / 2)
      tp200 (ratio >= 3.00) -> sell floor(qty_remaining / 2)
      tp300 (ratio >= 4.00) -> sell ALL remaining
  * If a rung's computed sell qty is 0 (e.g. 1 contract left at tp50), the
    rung is latched WITHOUT selling so it is never retried; tp300 takes the
    last contract later.
  * The 60% stop keeps applying to whatever remains after partial
    take-profits.

SAFETY:
  * KILL halts ALL order placement INCLUDING automated exits (explicit
    decision). When halted, check() sells nothing and returns a "halted"
    notification; positions are unprotected and the user exits manually.
  * This module defaults to dry_run; it never constructs a live client and
    places no real orders by itself.
  * place_conditional_exits() is OPTIONAL and is never called from
    ExitMonitor.check() (see its docstring).
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import config
import kill
import ledger

POSITIONS_PATH = Path.home() / "workspace" / "copy-trader" / "positions.json"

# (latch key, trigger ratio), ascending. tp300 is the last rung and always
# sells whatever remains.
_TP_RUNGS = (
    ("tp50", 1.50),
    ("tp200", 3.0),
    ("tp300", 4.0),
)

_STOP_RATIO = 0.40  # -60%

_RUNG_LABELS = {
    "sl": "STOP-LOSS (-60%)",
    "tp50": "TP50 (+50%)",
    "tp200": "TP200 (+200%)",
    "tp300": "TP300 (+300%)",
}

_EMPTY_LATCHES = {"sl": False, "tp50": False, "tp200": False, "tp300": False}


# ---------------------------------------------------------------------------
# Position store
# ---------------------------------------------------------------------------

def load_positions() -> list[dict]:
    """Return the full position list (empty list when the file is absent)."""
    if not POSITIONS_PATH.exists():
        return []
    data = json.loads(POSITIONS_PATH.read_text())
    if isinstance(data, dict):
        data = data.get("positions", [])
    return data if isinstance(data, list) else []


def save_positions(positions: list[dict]) -> None:
    """Atomically write positions (tmp file + os.replace)."""
    POSITIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(POSITIONS_PATH.parent),
        prefix=POSITIONS_PATH.name + ".",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w") as f:
            json.dump({"positions": positions}, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, POSITIONS_PATH)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def open_positions() -> list[dict]:
    """Positions with status == "open" and qty_remaining > 0."""
    out = []
    for p in load_positions():
        if not isinstance(p, dict):
            continue
        if p.get("status") != "open":
            continue
        try:
            qty = int(p.get("qty_remaining", 0))
        except (TypeError, ValueError):
            qty = 0
        if qty > 0:
            out.append(p)
    return out


def register_position(contract_symbol: str, underlying: str, qty, fill_price) -> dict:
    """Append a new open position (called by the entry engine on every fill)."""
    qty = int(qty)
    fill_price = float(fill_price)
    if qty <= 0:
        raise ValueError("qty must be positive, got %r" % (qty,))
    if fill_price <= 0:
        raise ValueError("fill_price must be positive, got %r" % (fill_price,))
    positions = load_positions()
    position = {
        "contract_symbol": contract_symbol,
        "underlying": underlying,
        "qty_initial": qty,
        "qty_remaining": qty,
        "fill_price": fill_price,
        "opened_at": datetime.now(timezone.utc).isoformat(),
        "latches": dict(_EMPTY_LATCHES),
        "status": "open",
    }
    positions.append(position)
    save_positions(positions)
    return position


# ---------------------------------------------------------------------------
# Exit monitor
# ---------------------------------------------------------------------------

def _quote_price(quote) -> float | None:
    """Best-effort price from an option quote dict, or None if unusable.

    Prefers `last` when it is a positive number; otherwise falls back to
    (bid + ask) / 2. Returns None when nothing usable is present.
    """
    if not isinstance(quote, dict):
        return None
    last = quote.get("last")
    try:
        last = float(last) if last is not None else None
    except (TypeError, ValueError):
        last = None
    if last is not None and last > 0:
        return last
    try:
        bid = float(quote.get("bid"))
        ask = float(quote.get("ask"))
    except (TypeError, ValueError):
        return None
    if bid < 0 or ask < 0:
        return None
    mid = (bid + ask) / 2.0
    return mid if mid > 0 else None


class ExitMonitor:
    """Poll-based exit ladder evaluator.

    On each check() it quotes every open position and fires any unlatched
    rung whose trigger is met, placing market sell orders through the MCP
    client. Latches make every rung one-time; the stop loss re-checks on
    every poll against the remaining quantity.
    """

    def __init__(self, mcp, mode: str = "dry_run"):
        assert mcp.mode == mode, (
            "ExitMonitor mode %r does not match MCP client mode %r" % (mode, mcp.mode)
        )
        self.mcp = mcp
        self.mode = mode

    # -- internals ------------------------------------------------------

    def _sell(self, position: dict, qty: int, px: float, rung: str,
              notifications: list[dict]) -> None:
        """Place the market sell order, ledger it, and notify.

        Raises on order failure WITHOUT latching, so the rung is retried on
        the next poll.
        """
        contract = position["contract_symbol"]
        fill = float(position["fill_price"])
        before = int(position["qty_remaining"])
        result = self.mcp.place_option_order(contract, "sell", qty, "market")
        order_id = None
        if isinstance(result, dict):
            order_id = result.get("order_id")
        if not order_id:
            order_id = "dry_run" if self.mode == "dry_run" else "unknown"
        realized = round((px - fill) * qty * 100, 2)
        ledger.append({
            "event": "exit_fill",
            "contract_symbol": contract,
            "underlying": position.get("underlying"),
            "qty": qty,
            "price": px,
            "rung": rung,
            "realized_pnl": realized,
            "fees": 0.0,
            "order_id": order_id,
        })
        after = before - qty
        sign = "+" if realized >= 0 else ""
        text = (
            "%s: sold %d of %d %s @ $%.2f — realized P&L %s$%.2f\n"
            "Remaining: %d contract(s)"
            % (_RUNG_LABELS[rung], qty, before, contract, px, sign, realized, after)
        )
        notifications.append({
            "kind": "exit",
            "rung": rung,
            "contract_symbol": contract,
            "text": text,
        })

    def _evaluate(self, position: dict, notifications: list[dict]) -> None:
        """Evaluate one open position: stop first, then TP rungs ascending."""
        fill = float(position["fill_price"])
        if fill <= 0:
            raise ValueError("fill_price is not positive: %r" % (fill,))
        quote = self.mcp.get_option_quote(position["contract_symbol"])
        px = _quote_price(quote)
        if px is None:
            return  # no usable price: skip silently
        ratio = px / fill
        latches = position.setdefault("latches", dict(_EMPTY_LATCHES))
        for key in _EMPTY_LATCHES:
            latches.setdefault(key, False)
        remaining = int(position["qty_remaining"])

        # 1. Stop loss: premium touches 0.40x fill -> sell ALL, close.
        if not latches["sl"] and ratio <= _STOP_RATIO and remaining > 0:
            self._sell(position, remaining, px, "sl", notifications)
            latches["sl"] = True
            position["qty_remaining"] = 0
            position["status"] = "closed"
            return

        # 2. Take-profit ladder, ascending. Each unlatched rung whose trigger
        #    is met fires in this same pass; tp300 takes everything left.
        if remaining > 0:
            for rung, trigger in _TP_RUNGS:
                if latches[rung] or ratio < trigger:
                    continue
                n = remaining if rung == "tp300" else remaining // 2
                if n > 0:
                    self._sell(position, n, px, rung, notifications)
                    remaining -= n
                    position["qty_remaining"] = remaining
                # Latch either way: a rung computed at n == 0 is never
                # retried; tp300 takes the last contract later.
                latches[rung] = True
                if remaining == 0:
                    position["status"] = "closed"
                    return

    # -- public ---------------------------------------------------------

    def check(self) -> list[dict]:
        """Run one exit evaluation pass; return notification dicts."""
        # (a) Kill switch halts ALL order placement, including exits.
        if not kill.can_fire():
            n = len(open_positions())
            ledger.append({
                "event": "kill_halt",
                "detail": "exit monitor halted",
            })
            return [{
                "kind": "halted",
                "text": (
                    "Kill switch engaged — automated exits HALTED. "
                    "%d open position(s) unprotected; exit manually in the "
                    "Robinhood app." % n
                ),
            }]

        # (b) Outside market hours there is nothing to quote.
        if not config.in_market_hours():
            return []

        notifications: list[dict] = []
        positions = load_positions()
        for position in positions:
            if not isinstance(position, dict):
                continue
            if position.get("status") != "open":
                continue
            try:
                qty = int(position.get("qty_remaining", 0))
            except (TypeError, ValueError):
                qty = 0
            if qty <= 0:
                continue
            try:
                self._evaluate(position, notifications)
            except Exception as e:  # noqa: BLE001 - per-position isolation
                notifications.append({
                    "kind": "rejected",
                    "contract_symbol": position.get("contract_symbol"),
                    "reason": "%s: %s" % (type(e).__name__, e),
                    "text": "Exit evaluation failed for %s: %s"
                            % (position.get("contract_symbol"), e),
                })
            # Save after each position's evaluation.
            try:
                save_positions(positions)
            except Exception as e:  # noqa: BLE001
                notifications.append({
                    "kind": "rejected",
                    "contract_symbol": position.get("contract_symbol"),
                    "reason": "positions save failed: %s" % e,
                    "text": "Could not persist positions after evaluating %s"
                            % position.get("contract_symbol"),
                })
        return notifications


# ---------------------------------------------------------------------------
# Optional: native conditional orders
# ---------------------------------------------------------------------------

class ConditionalOrdersUnsupported(RuntimeError):
    """Raised when native conditional orders are not supported by the endpoint."""


def place_conditional_exits(mcp, position: dict) -> dict:
    """OPTIONAL — not called from ExitMonitor.check().

    Places native conditional stop-loss / take-profit orders for one position.
    Use ONLY if ``mcp.check_options_support()["conditional_orders"]`` is True.

    Until the live capability check proves otherwise, the poll monitor
    (ExitMonitor.check()) is the primary exit path: it works with nothing
    but basic market-order support and per-account conditional availability
    is unverified. The exact argument schema of the conditional tool is
    UNVERIFIED against the live endpoint (see mcp_client module docstring);
    this function uses a best-effort OCO structure and should be re-verified
    live before real use.

    Args:
        mcp: MCPClient in live mode.
        position: position record with contract_symbol, qty_remaining,
            fill_price, latches.

    Returns the raw tool result dict. Raises ConditionalOrdersUnsupported
    when the endpoint reports no conditional-order support.
    """
    support = mcp.check_options_support()
    if not support.get("conditional_orders"):
        raise ConditionalOrdersUnsupported(
            "Native conditional orders are not supported by this endpoint/account "
            "(check_options_support()['conditional_orders'] is False). "
            "Use ExitMonitor.check() polling instead."
        )
    tool = mcp._bind().get("conditional_place") if hasattr(mcp, "_bind") else None
    if not tool:
        raise ConditionalOrdersUnsupported(
            "No conditional_place tool bound from tools/list."
        )
    fill = float(position["fill_price"])
    remaining = int(position["qty_remaining"])
    if remaining <= 0:
        raise ValueError("position has no remaining quantity")
    # One OCO group: stop leg at 0.40x fill over ALL remaining, TP legs per
    # the ladder over half / half / rest. Exact trigger semantics are
    # broker-specific; verify live before use.
    oco = {
        "contract_symbol": position["contract_symbol"],
        "legs": [
            {"side": "sell", "quantity": remaining, "order_type": "stop_market",
             "trigger": "premium_ratio<=0.40",
             "limit_price": round(_STOP_RATIO * fill, 2)},
            {"side": "sell", "quantity": remaining // 2 or remaining,
             "order_type": "limit", "trigger": "premium_ratio>=1.50",
             "limit_price": round(1.50 * fill, 2)},
            {"side": "sell", "quantity": remaining // 2 or remaining,
             "order_type": "limit", "trigger": "premium_ratio>=3.00",
             "limit_price": round(3.00 * fill, 2)},
            {"side": "sell", "quantity": remaining, "order_type": "limit",
             "trigger": "premium_ratio>=4.00",
             "limit_price": round(4.00 * fill, 2)},
        ],
    }
    return mcp.call_tool(tool, oco)


# ---------------------------------------------------------------------------
# Self-test (fake MCP: no network, no real orders, dry_run only)
# ---------------------------------------------------------------------------

def _self_test() -> None:
    from contextlib import redirect_stdout
    import io
    import shutil
    import tempfile

    global POSITIONS_PATH
    old_positions_path = POSITIONS_PATH
    old_can_fire = kill.can_fire
    old_in_market_hours = config.in_market_hours
    old_ledger_path = ledger.LEDGER_PATH

    tmp = Path(tempfile.mkdtemp(prefix="copy-trader-exits-test-"))
    POSITIONS_PATH = tmp / "positions.json"
    ledger.LEDGER_PATH = tmp / "ledger.jsonl"

    class FakeMCP:
        """Fake option-quote / order-placement client (dry_run only)."""

        def __init__(self, quotes):
            self.mode = "dry_run"
            self.quotes = dict(quotes)
            self.orders = []  # (contract_symbol, side, qty, order_type)
            self.fail_on = set()

        def get_option_quote(self, contract_symbol):
            q = self.quotes.get(contract_symbol)
            if q is None:
                raise RuntimeError("no quote for %s" % contract_symbol)
            return q

        def place_option_order(self, contract_symbol, side, qty, order_type,
                               limit_price=None):
            if contract_symbol in self.fail_on:
                raise RuntimeError("order rejected")
            self.orders.append((contract_symbol, side, qty, order_type))
            return {"dry_run": True, "order_id": "DRY-%d" % len(self.orders)}

        def check_options_support(self):
            return {"conditional_orders": False}

    passed, failed = [], []

    def check(label, cond, detail=""):
        (passed if cond else failed).append(label)
        print(("PASS " if cond else "FAIL ") + label
              + ((" — " + detail) if detail and not cond else ""))

    buf = io.StringIO()
    try:
        kill.can_fire = lambda: True
        config.in_market_hours = lambda now=None: True

        def _reset():
            save_positions([])

        with redirect_stdout(buf):
            _reset()
            # --- 1. TP ladder: 4 contracts @ 1.00 ---------------------------
            mcp = FakeMCP({"C1": {"bid": 1.49, "ask": 1.51, "last": 1.50}})
            _reset()
            register_position("C1", "SPY", 4, 1.00)
            mon = ExitMonitor(mcp)
            notes = mon.check()
            p = load_positions()[0]
            check("tp50 sells 2 of 4",
                  len(mcp.orders) == 1 and mcp.orders[0][2] == 2
                  and p["qty_remaining"] == 2 and p["latches"]["tp50"]
                  and p["status"] == "open", str(mcp.orders))
            check("tp50 notification",
                  len(notes) == 1 and notes[0]["kind"] == "exit"
                  and notes[0]["rung"] == "tp50", str(notes))

            # --- 2. tp200: px 3.00 sells 1 of 2 -----------------------------
            mcp.quotes["C1"] = {"bid": 2.99, "ask": 3.01, "last": 3.00}
            notes = mon.check()
            p = load_positions()[0]
            check("tp200 sells 1 of 2",
                  len(mcp.orders) == 2 and mcp.orders[1][2] == 1
                  and p["qty_remaining"] == 1 and p["latches"]["tp200"],
                  str(mcp.orders))

            # --- 3. tp300: px 4.00 sells last 1, closes ----------------------
            mcp.quotes["C1"] = {"bid": 3.99, "ask": 4.01, "last": 4.00}
            notes = mon.check()
            p = load_positions()[0]
            check("tp300 sells last contract and closes",
                  len(mcp.orders) == 3 and mcp.orders[2][2] == 1
                  and p["qty_remaining"] == 0 and p["status"] == "closed"
                  and p["latches"]["tp300"], str(p))
            check("exit_fill ledger records pnl",
                  any(r.get("event") == "exit_fill" and r.get("rung") == "tp300"
                      and r.get("realized_pnl") == 300.0
                      for r in ledger.read_all()), str(ledger.read_all()))

            # --- 4. latches are one-time: repeat check sells nothing ---------
            n_orders = len(mcp.orders)
            notes = mon.check()
            check("repeat check at same px sells nothing",
                  len(mcp.orders) == n_orders and notes == [],
                  str(mcp.orders))

            # --- 5. stop loss: fill 2.00, px 0.79 sells all -----------------
            mcp2 = FakeMCP({"C2": {"bid": 0.78, "ask": 0.80, "last": 0.79}})
            _reset()
            register_position("C2", "QQQ", 4, 2.00)
            mon2 = ExitMonitor(mcp2)
            notes = mon2.check()
            p2 = [p for p in load_positions() if p["contract_symbol"] == "C2"][0]
            check("stop sells all at 0.79 (ratio 0.395 <= 0.40)",
                  len(mcp2.orders) == 1 and mcp2.orders[0][2] == 4
                  and p2["status"] == "closed" and p2["latches"]["sl"],
                  str(mcp2.orders))
            recs = [r for r in ledger.read_all()
                    if r.get("event") == "exit_fill" and r.get("rung") == "sl"]
            check("stop ledger pnl",
                  len(recs) == 1 and recs[0]["realized_pnl"] == -484.0,
                  str(recs))

            # --- 6. odd lot: 3 -> tp50 sells 1, keeps 2 ---------------------
            mcp3 = FakeMCP({"C3": {"last": 1.50}})
            _reset()
            register_position("C3", "SPY", 3, 1.00)
            mon3 = ExitMonitor(mcp3)
            mon3.check()
            p3 = [p for p in load_positions() if p["contract_symbol"] == "C3"][0]
            check("odd lot 3: tp50 sells floor(3/2)=1 keeps 2",
                  len(mcp3.orders) == 1 and mcp3.orders[0][2] == 1
                  and p3["qty_remaining"] == 2, str(p3))

            # --- 7. 1 contract: tp50 latches with 0 sold --------------------
            mcp4 = FakeMCP({"C4": {"last": 1.60}})
            _reset()
            register_position("C4", "SPY", 1, 1.00)
            mon4 = ExitMonitor(mcp4)
            notes = mon4.check()
            p4 = [p for p in load_positions() if p["contract_symbol"] == "C4"][0]
            check("1 contract: tp50 latches with 0 sold",
                  len(mcp4.orders) == 0 and p4["latches"]["tp50"]
                  and p4["qty_remaining"] == 1 and p4["status"] == "open"
                  and notes == [], str(p4))

            # --- 7b. tp200 also latches 0; tp300 sells the last one ---------
            mcp4.quotes["C4"] = {"last": 3.10}
            mon4.check()
            p4 = [p for p in load_positions() if p["contract_symbol"] == "C4"][0]
            check("1 contract: tp200 latches with 0 sold",
                  len(mcp4.orders) == 0 and p4["latches"]["tp200"]
                  and p4["qty_remaining"] == 1, str(p4))
            mcp4.quotes["C4"] = {"last": 4.20}
            notes = mon4.check()
            p4 = [p for p in load_positions() if p["contract_symbol"] == "C4"][0]
            check("1 contract: tp300 later sells it",
                  len(mcp4.orders) == 1 and mcp4.orders[0][2] == 1
                  and p4["status"] == "closed", str(mcp4.orders))

            # --- 8. kill halt: sells nothing, halted notification -----------
            mcp5 = FakeMCP({"C5": {"last": 10.0}})
            _reset()
            register_position("C5", "SPY", 2, 1.00)
            kill.can_fire = lambda: False
            mon5 = ExitMonitor(mcp5)
            notes = mon5.check()
            p5 = [p for p in load_positions() if p["contract_symbol"] == "C5"][0]
            check("kill halt: sells nothing, position untouched",
                  len(mcp5.orders) == 0 and p5["status"] == "open"
                  and p5["qty_remaining"] == 2, str(mcp5.orders))
            check("kill halt: halted notification",
                  len(notes) == 1 and notes[0]["kind"] == "halted"
                  and "HALTED" in notes[0]["text"], str(notes))
            check("kill halt: ledger records kill_halt",
                  any(r.get("event") == "kill_halt" for r in ledger.read_all()))
            kill.can_fire = lambda: True

            # --- 9. gap-up to 5x: all three rungs in one pass --------------
            mcp6 = FakeMCP({"C6": {"last": 5.00}})
            _reset()
            register_position("C6", "SPY", 4, 1.00)
            mon6 = ExitMonitor(mcp6)
            notes = mon6.check()
            p6 = [p for p in load_positions() if p["contract_symbol"] == "C6"][0]
            qtys = [o[2] for o in mcp6.orders]
            rungs = [n["rung"] for n in notes]
            check("gap-up 5x fires all three rungs in one pass",
                  qtys == [2, 1, 1] and rungs == ["tp50", "tp200", "tp300"]
                  and p6["status"] == "closed" and p6["qty_remaining"] == 0,
                  "orders=%s rungs=%s" % (qtys, rungs))
            pnl_total = sum(r.get("realized_pnl", 0)
                            for r in ledger.read_all()
                            if r.get("event") == "exit_fill"
                            and r.get("contract_symbol") == "C6")
            check("gap-up pnl sums correctly",
                  pnl_total == 1600.0, str(pnl_total))

            # --- 10. no usable price: silent skip ---------------------------
            mcp7 = FakeMCP({"C7": {"last": None, "bid": None, "ask": None}})
            _reset()
            register_position("C7", "SPY", 2, 1.00)
            mon7 = ExitMonitor(mcp7)
            notes = mon7.check()
            check("no usable price: skipped silently",
                  notes == [] and len(mcp7.orders) == 0, str(notes))

            # --- 11. per-position exception -> rejected, others continue ----
            mcp8 = FakeMCP({"C8": {"last": 5.00}})  # no quote for C9 -> error
            _reset()
            register_position("C8", "SPY", 2, 1.00)
            register_position("C9", "SPY", 2, 1.00)
            mon8 = ExitMonitor(mcp8)
            notes = mon8.check()
            kinds = [n["kind"] for n in notes]
            check("exception -> rejected notification, others continue",
                  "rejected" in kinds and "exit" in kinds
                  and any("C9" in n.get("text", "") for n in notes
                          if n["kind"] == "rejected")
                  and any(n.get("contract_symbol") == "C8" for n in notes
                          if n["kind"] == "exit"), str(notes))

            # --- 12. outside market hours: no-op ----------------------------
            config.in_market_hours = lambda now=None: False
            mcp9 = FakeMCP({"C10": {"last": 5.00}})
            _reset()
            register_position("C10", "SPY", 2, 1.00)
            notes = ExitMonitor(mcp9).check()
            check("outside market hours: no-op",
                  notes == [] and len(mcp9.orders) == 0, str(notes))
            config.in_market_hours = lambda now=None: True

            # --- 13. place_conditional_exits not called by check ------------
            try:
                place_conditional_exits(mcp, load_positions()[0])
                check("place_conditional_exits raises without support", False)
            except ConditionalOrdersUnsupported:
                check("place_conditional_exits raises without support", True)
            import inspect
            src = inspect.getsource(ExitMonitor.check)
            check("check() never calls place_conditional_exits",
                  "place_conditional_exits" not in src, src)

            # --- 14. mode mismatch asserts ----------------------------------
            try:
                ExitMonitor(mcp, mode="live")
                check("mode mismatch asserts", False)
            except AssertionError:
                check("mode mismatch asserts", True)
    finally:
        kill.can_fire = old_can_fire
        config.in_market_hours = old_in_market_hours
        ledger.LEDGER_PATH = old_ledger_path
        POSITIONS_PATH = old_positions_path
        shutil.rmtree(tmp, ignore_errors=True)

    print(buf.getvalue(), end="")
    print("\n%d passed, %d failed" % (len(passed), len(failed)))
    if failed:
        print("FAILED: " + ", ".join(failed))
        raise SystemExit(1)


if __name__ == "__main__":
    _self_test()
    print("exits.py self-test: OK")
