"""Entry engine for the Robinhood copy-trader.

Takes a detected trade entry alert, runs every guard in a fixed order, and
fires (or blocks) the order through a caller-supplied MCPClient.

Pure local logic apart from the order path itself: this module never
constructs an MCPClient (live or otherwise), makes no network calls of its
own, and handles no credentials. The default mode is "dry_run"; live orders
can only happen if the orchestrator explicitly passes mode="live" together
with a live-mode client.

Guard order (FIXED — do not reorder):
  1. kill.can_fire()                            -> blocked if not armed/halted
  2. config.in_market_hours()                   -> blocked outside hours
  3. resolver.resolve(...)                      -> "ambiguous" halts, never proceed
  4. contract.premium is not None               -> "needs_manual", never auto-fire
  5. live quote sanity + chase guard (<=10% over trader's premium)
  6. limit / qty sizing
  7. MAX_OPEN_EXPOSURE cap
  8. DAILY_LOSS_CAP
  9. review_option_order (called in BOTH modes, so mocks exercise the path)
 10. place_option_order
 11. fired: ledger entry_fill + exits.register_position + fill notification
 12. blocked/rejected: ledger entry_blocked/entry_rejected + notification
 13. any exception -> rejected with "<Type>: <msg>"

On fired entries, exits.register_position() is called via a guarded import:
if exits.py is not built yet the registration is skipped, never fatal.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import config
import kill
import ledger
import resolver
from config import (
    DAILY_LOSS_CAP,
    MARKET_TZ,
    MAX_ENTRY_SLIPPAGE,
    MAX_OPEN_EXPOSURE,
    TRADE_NOTIONAL_TARGET,
)

# exits.py is built in parallel; never fail if it is not present yet.
try:
    import exits

    _EXITS_AVAILABLE = True
except ImportError:  # pragma: no cover - depends on build order
    exits = None  # type: ignore[assignment]
    _EXITS_AVAILABLE = False

POSITIONS_PATH = Path.home() / "workspace" / "copy-trader" / "positions.json"


@dataclass
class EntryResult:
    action: str  # "fired" | "blocked" | "rejected" | "needs_manual" | "ambiguous" | "skipped"
    reason: str
    contract: object | None
    qty: int
    limit: float | None
    order_id: str | None
    fill_price: float | None
    notification: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _contract_desc(contract) -> str:
    """Short human description: 'SPY 700 call exp 2026-09-23'."""
    if contract is None:
        return "unknown contract"
    strike = ("%g" % contract.strike) if isinstance(contract.strike, float) else str(contract.strike)
    return f"{contract.underlying} {strike} {contract.option_type} exp {contract.expiry}"


def _read_positions() -> list:
    """Open positions list from positions.json (empty list if missing/bad)."""
    if not POSITIONS_PATH.exists():
        return []
    try:
        data = json.loads(POSITIONS_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return []
    if isinstance(data, dict):
        data = data.get("positions", [])
    return data if isinstance(data, list) else []


def _notify(kind: str, text: str, **fields) -> dict:
    note = {"kind": kind, "text": text}
    for k, v in fields.items():
        if v is not None:
            note[k] = v
    return note


def _ledger_terminal(alert: dict, contract, event: str, reason: str,
                     qty: int = 0, limit: float | None = None,
                     order_id: str | None = None) -> None:
    ledger.append({
        "event": event,
        "alert_id": alert.get("id"),
        "handle": alert.get("handle"),
        "contract_symbol": getattr(contract, "contract_symbol", None),
        "qty": qty,
        "limit": limit,
        "order_id": order_id,
        "reason": reason,
        "url": alert.get("url"),
    })


# ---------------------------------------------------------------------------
# main entry point
# ---------------------------------------------------------------------------


def process_entry(alert: dict, mcp, mode: str = "dry_run") -> EntryResult:
    assert mcp.mode == mode, "client mode mismatch"
    try:
        return _process_entry(alert, mcp, mode)
    except Exception as exc:  # guard 13: any exception -> rejected
        reason = f"{type(exc).__name__}: {exc}"
        _ledger_terminal(alert, None, "entry_rejected", reason)
        text = (
            f"ENTRY REJECTED: {alert.get('handle')} — unexpected error\n"
            f"Reason: {reason}\n"
            f"Link: {alert.get('url')}"
        )
        return EntryResult(
            action="rejected", reason=reason, contract=None, qty=0,
            limit=None, order_id=None, fill_price=None,
            notification=_notify("rejected", text, reason=reason),
        )


def _process_entry(alert: dict, mcp, mode: str) -> EntryResult:
    handle = alert.get("handle")
    alert_id = alert.get("id")

    # -- guard 1: kill switch / armed state --------------------------------
    if not kill.can_fire():
        reason = "kill switch engaged or trader not armed"
        _ledger_terminal(alert, None, "entry_blocked", reason)
        text = (
            f"ENTRY BLOCKED: {handle} — order not attempted\n"
            f"Reason: {reason}\n"
            f"Link: {alert.get('url')}"
        )
        return EntryResult(
            action="blocked", reason=reason, contract=None, qty=0,
            limit=None, order_id=None, fill_price=None,
            notification=_notify("blocked", text, reason=reason),
        )

    # -- guard 2: market hours ----------------------------------------------
    if not config.in_market_hours():
        reason = "outside market hours"
        _ledger_terminal(alert, None, "entry_blocked", reason)
        text = (
            f"ENTRY BLOCKED: {handle} — order not attempted\n"
            f"Reason: {reason}\n"
            f"Link: {alert.get('url')}"
        )
        return EntryResult(
            action="blocked", reason=reason, contract=None, qty=0,
            limit=None, order_id=None, fill_price=None,
            notification=_notify("blocked", text, reason=reason),
        )

    # -- guard 3: resolve the contract --------------------------------------
    resolved = resolver.resolve(alert.get("text", ""), handle,
                                alert.get("posted_at", ""), mcp)
    if isinstance(resolved, resolver.Ambiguous):
        reason = resolved.reason
        text = (
            f"AMBIGUOUS ENTRY: {handle} — could not resolve a single contract\n"
            f"Reason: {reason} | candidates: {len(resolved.candidates or [])}\n"
            f"Link: {alert.get('url')}"
        )
        ledger.append({
            "event": "entry_ambiguous",
            "alert_id": alert_id,
            "handle": handle,
            "reason": reason,
            "candidates": resolved.candidates,
            "url": alert.get("url"),
        })
        return EntryResult(
            action="ambiguous", reason=reason, contract=None, qty=0,
            limit=None, order_id=None, fill_price=None,
            notification=_notify("ambiguous", text, reason=reason,
                                 candidates=resolved.candidates),
        )
    contract = resolved
    premium = contract.premium

    # -- guard 4: premium must be disclosed; never auto-fire blind ----------
    if premium is None:
        reason = "post discloses no entry premium — limit must be set manually"
        desc = _contract_desc(contract)
        text = (
            f"MANUAL REVIEW NEEDED: {handle} posted {desc} with no entry price\n"
            f"Reason: {reason}\n"
            f"Link: {alert.get('url')}"
        )
        ledger.append({
            "event": "entry_needs_manual",
            "alert_id": alert_id,
            "handle": handle,
            "contract_symbol": contract.contract_symbol,
            "underlying": contract.underlying,
            "expiry": contract.expiry,
            "strike": contract.strike,
            "option_type": contract.option_type,
            "reason": reason,
            "url": alert.get("url"),
        })
        return EntryResult(
            action="needs_manual", reason=reason, contract=contract, qty=0,
            limit=None, order_id=None, fill_price=None,
            notification=_notify("needs_manual", text,
                                 contract_symbol=contract.contract_symbol,
                                 reason=reason),
        )

    # -- guard 5: live quote sanity + chase guard ----------------------------
    q = mcp.get_option_quote(contract.contract_symbol)
    ask = q.get("ask") if isinstance(q, dict) else None
    desc = _contract_desc(contract)
    if ask is None or ask <= 0:
        reason = "no live quote"
        _ledger_terminal(alert, contract, "entry_rejected", reason)
        text = (
            f"ENTRY REJECTED: {handle} {desc} — order not attempted\n"
            f"Reason: {reason}\n"
            f"Link: {alert.get('url')}"
        )
        return EntryResult(
            action="rejected", reason=reason, contract=contract, qty=0,
            limit=None, order_id=None, fill_price=None,
            notification=_notify("rejected", text,
                                 contract_symbol=contract.contract_symbol,
                                 reason=reason),
        )
    cap = (1 + MAX_ENTRY_SLIPPAGE) * premium
    if ask > cap:
        reason = (f"quote {ask:.2f} already >10% above their entry "
                  f"{premium:.2f} — not chasing")
        _ledger_terminal(alert, contract, "entry_blocked", reason)
        text = (
            f"ENTRY BLOCKED: {handle} {desc} — not chasing\n"
            f"Reason: {reason}\n"
            f"Link: {alert.get('url')}"
        )
        return EntryResult(
            action="blocked", reason=reason, contract=contract, qty=0,
            limit=None, order_id=None, fill_price=None,
            notification=_notify("blocked", text,
                                 contract_symbol=contract.contract_symbol,
                                 reason=reason),
        )

    # -- guard 6: limit / qty sizing -----------------------------------------
    limit = min(cap, ask)
    qty = max(1, round(TRADE_NOTIONAL_TARGET / (limit * 100)))

    # -- guard 7: exposure cap ----------------------------------------------
    positions = _read_positions()
    open_notional = sum(
        float(p.get("qty_remaining", 0)) * float(p.get("fill_price", 0)) * 100
        for p in positions
        if isinstance(p, dict) and p.get("status") == "open"
    )
    new_notional = qty * limit * 100
    if open_notional + new_notional > MAX_OPEN_EXPOSURE:
        reason = (f"open exposure ${open_notional:,.0f} + new "
                  f"${new_notional:,.0f} exceeds max ${MAX_OPEN_EXPOSURE:,.0f}")
        _ledger_terminal(alert, contract, "entry_blocked", reason, qty, limit)
        text = (
            f"ENTRY BLOCKED: {handle} {desc} — exposure cap\n"
            f"Reason: {reason}\n"
            f"Link: {alert.get('url')}"
        )
        return EntryResult(
            action="blocked", reason=reason, contract=contract, qty=qty,
            limit=limit, order_id=None, fill_price=None,
            notification=_notify("blocked", text,
                                 contract_symbol=contract.contract_symbol,
                                 qty=qty, limit_price=limit, reason=reason),
        )

    # -- guard 8: daily loss cap ---------------------------------------------
    today = datetime.now(MARKET_TZ).strftime("%Y-%m-%d")
    if ledger.realized_pnl_on(today) <= -DAILY_LOSS_CAP:
        reason = "daily loss cap -$2,500 hit — no new entries today"
        _ledger_terminal(alert, contract, "entry_blocked", reason, qty, limit)
        text = (
            f"ENTRY BLOCKED: {handle} {desc} — daily loss cap\n"
            f"Reason: {reason}\n"
            f"Link: {alert.get('url')}"
        )
        return EntryResult(
            action="blocked", reason=reason, contract=contract, qty=qty,
            limit=limit, order_id=None, fill_price=None,
            notification=_notify("blocked", text,
                                 contract_symbol=contract.contract_symbol,
                                 qty=qty, limit_price=limit, reason=reason),
        )

    # -- guard 9: review (both modes) ----------------------------------------
    mcp.review_option_order(contract.contract_symbol, "buy", qty, "limit", limit)

    # -- guard 10: place ------------------------------------------------------
    placed = mcp.place_option_order(contract.contract_symbol, "buy", qty, "limit", limit)
    if mode == "live":
        order_id = placed["order_id"]
        fill_price = placed.get("avg_fill_price") or limit
    else:
        order_id = f"DRYRUN-{alert_id}"
        fill_price = limit

    # -- guard 11: ledger + exits registration + fill notification -----------
    ledger.append({
        "event": "entry_fill",
        "alert_id": alert_id,
        "handle": handle,
        "contract_symbol": contract.contract_symbol,
        "underlying": contract.underlying,
        "expiry": contract.expiry,
        "strike": contract.strike,
        "option_type": contract.option_type,
        "qty": qty,
        "limit": limit,
        "trader_premium": premium,
        "order_id": order_id,
        "fill_price": fill_price,
        "url": alert.get("url"),
    })
    if _EXITS_AVAILABLE:
        exits.register_position(contract.contract_symbol, contract.underlying,
                                qty, fill_price)

    text = (
        f"ENTRY FIRED: bought {qty}x {desc} @ {limit:.2f} limit "
        f"(fill {fill_price:.2f})\n"
        f"Trader {handle} | contract {contract.contract_symbol} | order {order_id}\n"
        f"Trader entry {premium:.2f}; slippage cap {cap:.2f}"
    )
    return EntryResult(
        action="fired", reason="order placed", contract=contract, qty=qty,
        limit=limit, order_id=order_id, fill_price=fill_price,
        notification=_notify("fill", text,
                             contract_symbol=contract.contract_symbol,
                             qty=qty, limit_price=limit, order_id=order_id),
    )


# ---------------------------------------------------------------------------
# Self-test (fake mcp + fake resolver — no network, no live orders, no real
# ledger/positions/kill-state touched: all are redirected to temp files)
# ---------------------------------------------------------------------------


def _self_test() -> None:
    import tempfile

    passed, failed = [], []

    def check(label, cond, detail=""):
        (passed if cond else failed).append(label)
        print(("PASS " if cond else "FAIL ") + label
              + ((" — " + str(detail)) if detail and not cond else ""))

    # --- fakes -------------------------------------------------------------
    class FakeMCP:
        def __init__(self, mode="dry_run", ask=1.90, boom=False):
            self.mode = mode
            self._ask = ask
            self._boom = boom
            self.calls = []

        def get_option_quote(self, contract_symbol):
            self.calls.append(("quote", contract_symbol))
            if self._boom:
                raise RuntimeError("quote service exploded")
            return {"bid": (self._ask - 0.05) if self._ask else None,
                    "ask": self._ask,
                    "last": self._ask}

        def review_option_order(self, contract_symbol, side, qty, order_type,
                                limit_price=None):
            self.calls.append(("review", contract_symbol, side, qty,
                               order_type, limit_price))
            return {"ok": True, "simulated": True}

        def place_option_order(self, contract_symbol, side, qty, order_type,
                               limit_price=None):
            self.calls.append(("place", contract_symbol, side, qty,
                               order_type, limit_price))
            return {"dry_run": True, "would_call": {}}

    def priced(premium=1.85):
        return resolver.ResolvedContract(
            underlying="SPY", expiry="2026-09-23", strike=700.0,
            option_type="call", contract_symbol="SPY   260923C00700000",
            premium=premium, inferred_expiry=False)

    def fake_resolve(result):
        def _resolve(post_text, handle, posted_at_iso, mcp):
            return result
        return _resolve

    def alert(**kw):
        a = {"id": "alert-1", "handle": "@clintoptions",
             "text": "$SPY 700c @ 1.85",
             "posted_at": "2026-09-21T10:00:00-07:00",
             "url": "https://x.com/x/status/1", "type": "entry"}
        a.update(kw)
        return a

    # --- redirect all persistent state to temp files -----------------------
    tmp = Path(tempfile.mkdtemp(prefix="entry-engine-test-"))
    orig_ledger_path = ledger.LEDGER_PATH
    orig_positions_path = POSITIONS_PATH
    orig_can_fire = kill.can_fire
    orig_in_market_hours = config.in_market_hours
    orig_resolve = resolver.resolve
    orig_exits = globals().get("exits")
    orig_exits_available = _EXITS_AVAILABLE
    ledger.LEDGER_PATH = tmp / "ledger.jsonl"
    globals()["POSITIONS_PATH"] = tmp / "positions.json"
    kill.can_fire = lambda: True
    config.in_market_hours = lambda now=None: True

    # never let the self-test register positions in the real exits store
    class FakeExits:
        def __init__(self):
            self.calls = []

        def register_position(self, contract_symbol, underlying, qty, fill_price):
            self.calls.append((contract_symbol, underlying, qty, fill_price))
            return {"contract_symbol": contract_symbol}

    fake_exits = FakeExits()
    globals()["exits"] = fake_exits
    globals()["_EXITS_AVAILABLE"] = True

    def reset_state():
        for p in (ledger.LEDGER_PATH, POSITIONS_PATH):
            if p.exists():
                p.unlink()

    try:
        # --- 1. happy path -------------------------------------------------
        reset_state()
        resolver.resolve = fake_resolve(priced(1.85))
        mcp = FakeMCP(ask=1.90)
        r = process_entry(alert(), mcp)
        check("happy: fires", r.action == "fired", r)
        check("happy: limit = min(2.035, ask)",
              abs(r.limit - 1.90) < 1e-9, r.limit)
        check("happy: qty = round(500/(limit*100))",
              r.qty == max(1, round(500 / (1.90 * 100))) == 3, r.qty)
        check("happy: dry-run order id", r.order_id == "DRYRUN-alert-1", r.order_id)
        check("happy: fill_price == limit", r.fill_price == r.limit, r.fill_price)
        check("happy: notification kind fill",
              r.notification.get("kind") == "fill", r.notification)
        kinds = [c[0] for c in mcp.calls]
        check("happy: review called before place",
              kinds == ["quote", "review", "place"], kinds)
        recs = ledger.read_all()
        fills = [x for x in recs if x["event"] == "entry_fill"]
        check("happy: ledger entry_fill recorded", len(fills) == 1, recs)
        f = fills[0]
        check("happy: ledger fields",
              f["trader_premium"] == 1.85 and f["qty"] == 3
              and f["contract_symbol"] == "SPY   260923C00700000"
              and f["order_id"] == "DRYRUN-alert-1", f)
        check("happy: exits.register_position called",
              fake_exits.calls == [("SPY   260923C00700000", "SPY", 3, 1.90)],
              fake_exits.calls)
        fake_exits.calls.clear()

        # --- 2. chase-blocked ------------------------------------------------
        reset_state()
        resolver.resolve = fake_resolve(priced(1.85))
        mcp = FakeMCP(ask=2.50)
        r = process_entry(alert(id="alert-2"), mcp)
        check("chase: blocked", r.action == "blocked", r)
        check("chase: reason names quote vs entry",
              "2.50" in r.reason and "1.85" in r.reason and "not chasing" in r.reason,
              r.reason)
        check("chase: no place attempted",
              not any(c[0] == "place" for c in mcp.calls), mcp.calls)
        check("chase: ledger entry_blocked",
              any(x["event"] == "entry_blocked" for x in ledger.read_all()))

        # --- 3. unpriced -> needs_manual ------------------------------------
        reset_state()
        resolver.resolve = fake_resolve(priced(None))
        mcp = FakeMCP(ask=1.90)
        r = process_entry(alert(id="alert-3"), mcp)
        check("unpriced: needs_manual", r.action == "needs_manual", r)
        check("unpriced: reason", "no entry premium" in r.reason, r.reason)
        check("unpriced: never fetched a quote",
              not any(c[0] == "quote" for c in mcp.calls), mcp.calls)
        check("unpriced: notification kind",
              r.notification.get("kind") == "needs_manual", r.notification)

        # --- 4. ambiguous ----------------------------------------------------
        reset_state()
        resolver.resolve = fake_resolve(
            resolver.Ambiguous(reason="no explicit expiry and no trader default",
                               candidates=["SPY   260923C00700000"]))
        mcp = FakeMCP(ask=1.90)
        r = process_entry(alert(id="alert-4"), mcp)
        check("ambiguous: action", r.action == "ambiguous", r)
        check("ambiguous: notification kind + reason + candidates",
              r.notification.get("kind") == "ambiguous"
              and "no explicit expiry" in r.notification.get("reason", "")
              and r.notification.get("candidates") == ["SPY   260923C00700000"],
              r.notification)
        check("ambiguous: never proceeded to quote",
              mcp.calls == [], mcp.calls)

        # --- 5. kill halt -> blocked (kill guard is first) -------------------
        reset_state()
        kill.can_fire = lambda: False
        config.in_market_hours = lambda now=None: True  # prove kill is first
        resolver.resolve = fake_resolve(priced(1.85))
        mcp = FakeMCP(ask=1.90)
        r = process_entry(alert(id="alert-5"), mcp)
        check("kill: blocked", r.action == "blocked", r)
        check("kill: reason",
              r.reason == "kill switch engaged or trader not armed", r.reason)
        check("kill: resolver never consulted", mcp.calls == [], mcp.calls)
        kill.can_fire = lambda: True

        # --- 6. exposure cap -> blocked --------------------------------------
        reset_state()
        POSITIONS_PATH.write_text(json.dumps([
            {"contract_symbol": "QQQ   260923C00650000", "qty_remaining": 25,
             "fill_price": 2.00, "status": "open"},
        ]))
        resolver.resolve = fake_resolve(priced(1.85))
        mcp = FakeMCP(ask=1.90)
        r = process_entry(alert(id="alert-6"), mcp)
        check("exposure: blocked", r.action == "blocked", r)
        check("exposure: reason has numbers",
              "5,000" in r.reason and "570" in r.reason, r.reason)
        check("exposure: qty/limit still reported",
              r.qty == 3 and abs(r.limit - 1.90) < 1e-9, (r.qty, r.limit))
        check("exposure: no place attempted",
              not any(c[0] == "place" for c in mcp.calls), mcp.calls)

        # --- 7. daily loss cap -> blocked ------------------------------------
        reset_state()
        ledger.append({"event": "exit_fill", "realized_pnl": -2600.0, "fees": 0.0})
        resolver.resolve = fake_resolve(priced(1.85))
        mcp = FakeMCP(ask=1.90)
        r = process_entry(alert(id="alert-7"), mcp)
        check("loss cap: blocked", r.action == "blocked", r)
        check("loss cap: reason",
              r.reason == "daily loss cap -$2,500 hit — no new entries today",
              r.reason)

        # --- 8. off-hours -> blocked ------------------------------------------
        reset_state()
        config.in_market_hours = lambda now=None: False
        resolver.resolve = fake_resolve(priced(1.85))
        mcp = FakeMCP(ask=1.90)
        r = process_entry(alert(id="alert-8"), mcp)
        check("off-hours: blocked", r.action == "blocked", r)
        check("off-hours: reason", r.reason == "outside market hours", r.reason)
        config.in_market_hours = lambda now=None: True

        # --- 9. no live quote -> rejected -------------------------------------
        reset_state()
        resolver.resolve = fake_resolve(priced(1.85))
        mcp = FakeMCP(ask=None)
        r = process_entry(alert(id="alert-9"), mcp)
        check("no-quote: rejected", r.action == "rejected", r)
        check("no-quote: reason", r.reason == "no live quote", r.reason)
        check("no-quote: notification kind",
              r.notification.get("kind") == "rejected", r.notification)

        # --- 10. exception -> rejected -----------------------------------------
        reset_state()
        resolver.resolve = fake_resolve(priced(1.85))
        mcp = FakeMCP(ask=1.90, boom=True)
        r = process_entry(alert(id="alert-10"), mcp)
        check("exception: rejected", r.action == "rejected", r)
        check("exception: reason format",
              r.reason.startswith("RuntimeError: "), r.reason)
        check("exception: ledger entry_rejected",
              any(x["event"] == "entry_rejected" for x in ledger.read_all()))

        # --- 11. mode mismatch assert -------------------------------------------
        try:
            process_entry(alert(id="alert-11"), FakeMCP(mode="live"), mode="dry_run")
            check("mode mismatch: assert raised", False)
        except AssertionError as e:
            check("mode mismatch: assert raised", "client mode mismatch" in str(e), e)

        # --- 12. cap-bound limit (ask above cap would chase-block; use tight) ---
        reset_state()
        resolver.resolve = fake_resolve(priced(1.85))
        mcp = FakeMCP(ask=2.00)  # under cap 2.035 -> limit = ask = 2.00
        r = process_entry(alert(id="alert-12"), mcp)
        check("limit binds to ask under cap",
              r.action == "fired" and abs(r.limit - 2.00) < 1e-9
              and r.qty == max(1, round(500 / 200)), (r.action, r.limit, r.qty))
    finally:
        import shutil

        ledger.LEDGER_PATH = orig_ledger_path
        globals()["POSITIONS_PATH"] = orig_positions_path
        globals()["exits"] = orig_exits
        globals()["_EXITS_AVAILABLE"] = orig_exits_available
        kill.can_fire = orig_can_fire
        config.in_market_hours = orig_in_market_hours
        resolver.resolve = orig_resolve
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n%d passed, %d failed" % (len(passed), len(failed)))
    if failed:
        print("FAILED: " + ", ".join(failed))
        raise SystemExit(1)


if __name__ == "__main__":
    _self_test()
