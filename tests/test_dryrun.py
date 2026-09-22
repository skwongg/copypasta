#!/usr/bin/env python3
"""Integration dry-run test suite for the Robinhood copy-trader.

QA for the full entry -> ledger -> positions -> exit pipeline with mocked
quotes/fills. HARD RULES for this suite:

  * NO real orders, NO live network, NO credentials, ever.
  * Every MCP stand-in runs in mode="dry_run" (asserted at construction).
  * All production state paths (ledger.jsonl, positions.json,
    notifications.jsonl, ARMED, KILL) are redirected per-test into a temp
    dir. Production files are snapshotted (mtime/size/sha256) at import
    time and verified byte-identical at the end of the suite.

Run from ~/workspace/copy-trader:
    python3 -m unittest tests.test_dryrun -v
"""

from __future__ import annotations

import hashlib
import inspect
import io
import json
import os
import re
import shutil
import sys
import tempfile
import tokenize
import unittest
from contextlib import redirect_stdout
from pathlib import Path

_CT_DIR = Path(__file__).resolve().parent.parent
if str(_CT_DIR) not in sys.path:
    sys.path.insert(0, str(_CT_DIR))

import config            # noqa: E402
import entry_engine      # noqa: E402
import exits             # noqa: E402
import fire_entries      # noqa: E402
import kill              # noqa: E402
import ledger            # noqa: E402
import mcp_client        # noqa: E402
import oauth_client      # noqa: E402
import resolver          # noqa: E402
import run_exits         # noqa: E402

NOTIFIER_JSON = _CT_DIR / "notifier" / "copy-trader-notifier.json"
NOTIFIER_CHAT_ID = "35a06c41-4e5c-458d-b2ab-6170fa3e92dd"

# A trading-day timestamp (Mon 2026-09-21) used by resolver 0DTE inference.
MON_0935_PT = "2026-09-21T09:35:00-07:00"


# ---------------------------------------------------------------------------
# production-state snapshot (taken once, at import, before any test runs)
# ---------------------------------------------------------------------------

_PROD_FILES = ("ARMED", "KILL", "ledger.jsonl", "positions.json",
               "notifications.jsonl", ".tokens.json", ".oauth_client.json")


def _snapshot_production() -> dict:
    snap = {}
    for name in _PROD_FILES:
        p = _CT_DIR / name
        if p.exists():
            st = p.stat()
            snap[name] = (st.st_mtime_ns, st.st_size,
                          hashlib.sha256(p.read_bytes()).hexdigest())
        else:
            snap[name] = None
    return snap


_PROD_SNAPSHOT = _snapshot_production()


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------

class FakeMCP:
    """Stand-in for MCPClient implementing the same typed methods.

    dry_run only (asserted); canned quotes/contracts; records every call.
    Never touches the network.
    """

    def __init__(self, mode="dry_run", quotes=None):
        assert mode == "dry_run", "tests must never use live mode"
        self.mode = mode
        self._quotes = dict(quotes or {})
        self.calls = []

    def find_option_contracts(self, underlying, expiry, strike, option_type):
        self.calls.append(("find", underlying, expiry, strike, option_type))
        return [{
            "contract_symbol": resolver.occ_symbol(underlying, expiry, strike,
                                                   option_type),
            "expiry": expiry,
            "strike": strike,
            "option_type": option_type,
        }]

    def get_option_quote(self, contract_symbol):
        self.calls.append(("quote", contract_symbol))
        ask = self._quotes.get(contract_symbol, self._quotes.get("*", 1.90))
        return {"bid": round(ask - 0.05, 2), "ask": ask, "last": ask}

    def review_option_order(self, contract_symbol, side, qty, order_type,
                            limit_price=None):
        self.calls.append(("review", contract_symbol, side, qty, order_type,
                           limit_price))
        return {"ok": True, "simulated": True}

    def place_option_order(self, contract_symbol, side, qty, order_type,
                           limit_price=None):
        self.calls.append(("place", contract_symbol, side, qty, order_type,
                           limit_price))
        return {"dry_run": True, "order_id": "DRY-%s" % contract_symbol}

    def get_positions(self):
        return []


class ExitFakeMCP:
    """Quote/order fake for the exit monitor. dry_run only."""

    def __init__(self, quotes):
        self.mode = "dry_run"
        self.quotes = dict(quotes)
        self.orders = []  # (contract_symbol, side, qty, order_type)

    def get_option_quote(self, contract_symbol):
        return self.quotes[contract_symbol]

    def place_option_order(self, contract_symbol, side, qty, order_type,
                           limit_price=None):
        self.orders.append((contract_symbol, side, qty, order_type))
        return {"dry_run": True, "order_id": "DRY-%d" % len(self.orders)}

    def get_positions(self):
        return []


def _alert(aid="e1", handle="CassyTrades", text="$SPY 759 PUTS 1.85",
           posted_at=MON_0935_PT, atype="entry"):
    return {"id": aid, "handle": handle, "text": text,
            "posted_at": posted_at,
            "url": "https://x.com/x/status/" + aid, "type": atype}


def _call_kinds(mcp):
    return [c[0] for c in mcp.calls]


# ---------------------------------------------------------------------------
# isolated base: redirect every production path into a temp dir
# ---------------------------------------------------------------------------

class IsolatedTest(unittest.TestCase):
    """Redirects ledger/positions/kill-state/notifications to a temp dir."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ct-dryrun-"))
        self._orig = {
            "ledger": ledger.LEDGER_PATH,
            "ee_pos": entry_engine.POSITIONS_PATH,
            "ex_pos": exits.POSITIONS_PATH,
            "armed": kill.ARMED_PATH,
            "kill": kill.KILL_PATH,
            "kill_pos": kill.POSITIONS_PATH,
            "notify": fire_entries.NOTIFICATIONS_PATH,
            "in_mh": config.in_market_hours,
        }
        ledger.LEDGER_PATH = self.tmp / "ledger.jsonl"
        entry_engine.POSITIONS_PATH = self.tmp / "positions.json"
        exits.POSITIONS_PATH = self.tmp / "positions.json"
        kill.ARMED_PATH = self.tmp / "ARMED"
        kill.KILL_PATH = self.tmp / "KILL"
        kill.POSITIONS_PATH = self.tmp / "positions.json"
        fire_entries.NOTIFICATIONS_PATH = str(self.tmp / "notifications.jsonl")
        config.in_market_hours = lambda now=None: True
        (self.tmp / "ARMED").touch()  # armed (no KILL) by default
        self.addCleanup(self._restore)

    def _restore(self):
        ledger.LEDGER_PATH = self._orig["ledger"]
        entry_engine.POSITIONS_PATH = self._orig["ee_pos"]
        exits.POSITIONS_PATH = self._orig["ex_pos"]
        kill.ARMED_PATH = self._orig["armed"]
        kill.KILL_PATH = self._orig["kill"]
        kill.POSITIONS_PATH = self._orig["kill_pos"]
        fire_entries.NOTIFICATIONS_PATH = self._orig["notify"]
        config.in_market_hours = self._orig["in_mh"]
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- small helpers --------------------------------------------------
    def entry_records(self, event):
        return [r for r in ledger.read_all() if r.get("event") == event]

    def queued_notifications(self):
        p = Path(fire_entries.NOTIFICATIONS_PATH)
        if not p.exists():
            return []
        return [json.loads(ln) for ln in p.read_text().splitlines()
                if ln.strip()]


# ===========================================================================
# A. ENTRY GUARDS (entry_engine.process_entry, real resolver, FakeMCP)
# ===========================================================================

class TestEntryGuards(IsolatedTest):

    def test_01_happy_path_fires(self):
        """CassyTrades '$SPY 759 PUTS 1.85', ask 1.90 -> fires at limit 1.90 x3."""
        mcp = FakeMCP(quotes={"*": 1.90})
        r = entry_engine.process_entry(_alert(), mcp)
        self.assertEqual(mcp.mode, "dry_run")

        self.assertEqual(r.action, "fired", r.reason)
        # limit = min(1.1*1.85, 1.90) = 1.90 ; qty = max(1, round(500/190)) = 3
        self.assertEqual(r.limit, 1.90)
        self.assertEqual(r.qty, 3)
        self.assertEqual(r.order_id, "DRYRUN-e1")
        self.assertEqual(r.fill_price, r.limit)
        self.assertEqual(r.notification.get("kind"), "fill")
        self.assertEqual(_call_kinds(mcp), ["find", "quote", "review", "place"])

        fills = self.entry_records("entry_fill")
        self.assertEqual(len(fills), 1)
        f = fills[0]
        self.assertEqual(f["qty"], 3)
        self.assertEqual(f["fill_price"], 1.90)
        self.assertEqual(f["limit"], 1.90)
        self.assertEqual(f["trader_premium"], 1.85)
        self.assertEqual(f["order_id"], "DRYRUN-e1")
        self.assertEqual(f["contract_symbol"], "SPY   260921P00759000")

        open_pos = exits.open_positions()
        self.assertEqual(len(open_pos), 1)
        self.assertEqual(open_pos[0]["qty_remaining"], 3)
        self.assertEqual(open_pos[0]["fill_price"], 1.90)
        self.assertEqual(open_pos[0]["status"], "open")

    def test_02_chase_guard_blocks(self):
        """ask 2.50 vs premium 1.85 (>10% over) -> blocked, no place call."""
        mcp = FakeMCP(quotes={"*": 2.50})
        r = entry_engine.process_entry(_alert(aid="e2"), mcp)
        self.assertEqual(r.action, "blocked", r)
        self.assertIn("not chasing", r.reason)
        self.assertIn("2.50", r.reason)
        self.assertIn("1.85", r.reason)
        self.assertNotIn("place", _call_kinds(mcp))
        self.assertEqual(len(self.entry_records("entry_blocked")), 1)

    def test_03_unpriced_needs_manual(self):
        """No disclosed premium -> needs_manual; no quote fetched, no fire."""
        mcp = FakeMCP(quotes={"*": 1.90})
        r = entry_engine.process_entry(
            _alert(aid="e3", text="$SPY 759 PUTS"), mcp)
        self.assertEqual(r.action, "needs_manual", r)
        self.assertIn("no entry premium", r.reason)
        self.assertNotIn("quote", _call_kinds(mcp))
        self.assertNotIn("place", _call_kinds(mcp))
        self.assertEqual(r.notification.get("kind"), "needs_manual")
        self.assertEqual(len(self.entry_records("entry_needs_manual")), 1)

    def test_04_ambiguous_capricekayem_no_expiry(self):
        """capricekayem post with no expiry and no trader default -> ambiguous."""
        mcp = FakeMCP(quotes={"*": 1.90})
        r = entry_engine.process_entry(
            _alert(aid="e4", handle="capricekayem"), mcp)
        self.assertEqual(r.action, "ambiguous", r)
        self.assertIn("no explicit expiry", r.reason)
        # resolver bails before any MCP lookup
        self.assertEqual(mcp.calls, [])
        self.assertEqual(r.notification.get("kind"), "ambiguous")

    def test_05_kill_halt_blocks(self):
        """KILL file present -> blocked before anything else runs."""
        (self.tmp / "KILL").touch()
        mcp = FakeMCP(quotes={"*": 1.90})
        r = entry_engine.process_entry(_alert(aid="e5"), mcp)
        self.assertEqual(r.action, "blocked", r)
        self.assertEqual(r.reason, "kill switch engaged or trader not armed")
        self.assertEqual(mcp.calls, [])  # resolver never consulted
        self.assertEqual(len(self.entry_records("entry_blocked")), 1)

    def test_06_exposure_cap_blocks(self):
        """$4,800 open + $570 new > $5,000 max -> blocked."""
        (self.tmp / "positions.json").write_text(json.dumps([
            {"contract_symbol": "QQQ   260921C00734000",
             "qty_remaining": 24, "fill_price": 2.00, "status": "open"},
        ]))
        mcp = FakeMCP(quotes={"*": 1.90})
        r = entry_engine.process_entry(_alert(aid="e6"), mcp)
        self.assertEqual(r.action, "blocked", r)
        self.assertIn("exceeds max", r.reason)
        self.assertIn("5,000", r.reason)
        self.assertEqual(r.qty, 3)
        self.assertEqual(r.limit, 1.90)
        self.assertNotIn("place", _call_kinds(mcp))

    def test_07_daily_loss_cap_blocks(self):
        """Realized -$2,600 today -> blocked."""
        ledger.append({"event": "exit_fill", "realized_pnl": -2600.0,
                       "fees": 0.0})
        mcp = FakeMCP(quotes={"*": 1.90})
        r = entry_engine.process_entry(_alert(aid="e7"), mcp)
        self.assertEqual(r.action, "blocked", r)
        self.assertEqual(r.reason,
                         "daily loss cap -$2,500 hit — no new entries today")
        self.assertNotIn("place", _call_kinds(mcp))

    def test_08_sizing_edge_small_premium(self):
        """premium 0.20, ask 0.19 -> limit 0.19, qty round(500/19) = 26."""
        mcp = FakeMCP(quotes={"*": 0.19})
        r = entry_engine.process_entry(
            _alert(aid="e8", text="$SPY 759 PUTS 0.20"), mcp)
        self.assertEqual(r.action, "fired", r.reason)
        self.assertEqual(r.limit, 0.19)
        self.assertEqual(r.qty, 26)

    def test_09_cheaper_than_premium_fires(self):
        """premium 2.00, ask 1.50 (cheaper is allowed) -> limit 1.50, fires."""
        mcp = FakeMCP(quotes={"*": 1.50})
        r = entry_engine.process_entry(
            _alert(aid="e9", text="$SPY 759 PUTS 2.00"), mcp)
        self.assertEqual(r.action, "fired", r.reason)
        self.assertEqual(r.limit, 1.50)
        self.assertEqual(r.qty, 3)  # round(500/150)

    def test_10_off_hours_blocks(self):
        """config.in_market_hours False -> blocked, resolver never consulted."""
        config.in_market_hours = lambda now=None: False
        mcp = FakeMCP(quotes={"*": 1.90})
        r = entry_engine.process_entry(_alert(aid="e10"), mcp)
        self.assertEqual(r.action, "blocked", r)
        self.assertEqual(r.reason, "outside market hours")
        self.assertEqual(mcp.calls, [])


# ===========================================================================
# B. EXIT LADDER (exits.ExitMonitor, ExitFakeMCP)
# ===========================================================================

class TestExitLadder(IsolatedTest):

    def _exit_fills(self, contract=None):
        recs = self.entry_records("exit_fill")
        if contract:
            recs = [r for r in recs if r.get("contract_symbol") == contract]
        return recs

    def test_10_full_ladder_tp50_tp200_tp300(self):
        """4 @ 1.00: 1.50 sells 2, 3.00 sells 1, 4.00 sells last; latches one-time."""
        mcp = ExitFakeMCP({"C1": {"last": 1.50}})
        self.assertEqual(mcp.mode, "dry_run")
        exits.register_position("C1", "SPY", 4, 1.00)
        mon = exits.ExitMonitor(mcp, mode="dry_run")

        notes = mon.check()  # tp50 @ 1.50x
        self.assertEqual(mcp.orders, [("C1", "sell", 2, "market")])
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0]["kind"], "exit")
        self.assertEqual(notes[0]["rung"], "tp50")
        p = exits.load_positions()[0]
        self.assertEqual(p["qty_remaining"], 2)
        self.assertTrue(p["latches"]["tp50"])
        self.assertEqual(p["status"], "open")

        mcp.quotes["C1"] = {"last": 3.00}
        notes = mon.check()  # tp200 @ 3.00x
        self.assertEqual(len(mcp.orders), 2)
        self.assertEqual(mcp.orders[1][2], 1)
        self.assertEqual(notes[0]["rung"], "tp200")
        p = exits.load_positions()[0]
        self.assertEqual(p["qty_remaining"], 1)
        self.assertTrue(p["latches"]["tp200"])

        mcp.quotes["C1"] = {"last": 4.00}
        notes = mon.check()  # tp300 @ 4.00x takes the last contract
        self.assertEqual(len(mcp.orders), 3)
        self.assertEqual(mcp.orders[2][2], 1)
        self.assertEqual(notes[0]["rung"], "tp300")
        p = exits.load_positions()[0]
        self.assertEqual(p["qty_remaining"], 0)
        self.assertEqual(p["status"], "closed")
        self.assertTrue(p["latches"]["tp300"])

        fills = self._exit_fills("C1")
        self.assertEqual([f["rung"] for f in fills], ["tp50", "tp200", "tp300"])
        self.assertEqual([f["realized_pnl"] for f in fills],
                         [100.0, 200.0, 300.0])
        self.assertEqual([f["qty"] for f in fills], [2, 1, 1])

        # repeat quote: latches are one-time, position closed -> nothing sells
        n = len(mcp.orders)
        notes = mon.check()
        self.assertEqual(len(mcp.orders), n)
        self.assertEqual(notes, [])

    def test_11_stop_loss_sells_all(self):
        """fill 2.00, quote 0.79 (ratio 0.395 <= 0.40) -> sells ALL."""
        mcp = ExitFakeMCP({"C2": {"last": 0.79}})
        exits.register_position("C2", "QQQ", 4, 2.00)
        mon = exits.ExitMonitor(mcp, mode="dry_run")
        notes = mon.check()
        self.assertEqual(mcp.orders, [("C2", "sell", 4, "market")])
        self.assertEqual(notes[0]["rung"], "sl")
        p = exits.load_positions()[0]
        self.assertEqual(p["status"], "closed")
        self.assertTrue(p["latches"]["sl"])
        fills = self._exit_fills("C2")
        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0]["realized_pnl"],
                         round((0.79 - 2.00) * 4 * 100, 2))  # -484.0
        self.assertLess(fills[0]["realized_pnl"], 0)

    def test_12_stop_applies_after_partial_tp(self):
        """tp50 sells 2 of 4; drop to 0.39 -> stop sells remaining 2."""
        mcp = ExitFakeMCP({"C3": {"last": 1.50}})
        exits.register_position("C3", "SPY", 4, 1.00)
        mon = exits.ExitMonitor(mcp, mode="dry_run")
        mon.check()
        self.assertEqual(mcp.orders, [("C3", "sell", 2, "market")])

        mcp.quotes["C3"] = {"last": 0.39}
        notes = mon.check()
        self.assertEqual(len(mcp.orders), 2)
        self.assertEqual(mcp.orders[1], ("C3", "sell", 2, "market"))
        self.assertEqual(notes[0]["rung"], "sl")
        p = exits.load_positions()[0]
        self.assertEqual(p["qty_remaining"], 0)
        self.assertEqual(p["status"], "closed")
        fills = self._exit_fills("C3")
        self.assertEqual([f["rung"] for f in fills], ["tp50", "sl"])
        self.assertEqual(fills[1]["realized_pnl"],
                         round((0.39 - 1.00) * 2 * 100, 2))  # -122.0

    def test_13_kill_halt_mid_run(self):
        """KILL engaged -> zero sells, 'halted' notification, kill_halt logged."""
        mcp = ExitFakeMCP({"C4": {"last": 10.0}})
        exits.register_position("C4", "SPY", 2, 1.00)
        (self.tmp / "KILL").touch()
        mon = exits.ExitMonitor(mcp, mode="dry_run")
        notes = mon.check()
        self.assertEqual(mcp.orders, [])
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0]["kind"], "halted")
        self.assertIn("HALTED", notes[0]["text"])
        p = exits.load_positions()[0]
        self.assertEqual(p["status"], "open")
        self.assertEqual(p["qty_remaining"], 2)
        self.assertEqual(len(self.entry_records("kill_halt")), 1)


# ===========================================================================
# C. END-TO-END (fire_entries in dry_run with temp queue)
# ===========================================================================

class _FireFakeMCP:
    """MCPClient stand-in for fire_entries.fire: asserts dry_run, canned data."""

    def __init__(self, token_provider=None, mode="dry_run"):
        assert mode == "dry_run", "e2e must never use live mode"
        self.mode = mode
        self.token_provider = token_provider
        self.calls = []

    def find_option_contracts(self, underlying, expiry, strike, option_type):
        self.calls.append(("find", underlying, expiry, strike, option_type))
        return [{
            "contract_symbol": resolver.occ_symbol(underlying, expiry, strike,
                                                   option_type),
            "expiry": expiry, "strike": strike, "option_type": option_type,
        }]

    def get_option_quote(self, contract_symbol):
        self.calls.append(("quote", contract_symbol))
        ask = 0.60 if contract_symbol.startswith("QQQ") else 1.90
        return {"bid": round(ask - 0.05, 2), "ask": ask, "last": ask}

    def review_option_order(self, contract_symbol, side, qty, order_type,
                            limit_price=None):
        self.calls.append(("review", contract_symbol, side, qty, order_type,
                           limit_price))
        return {"ok": True, "simulated": True}

    def place_option_order(self, contract_symbol, side, qty, order_type,
                           limit_price=None):
        self.calls.append(("place", contract_symbol, side, qty, order_type,
                           limit_price))
        return {"dry_run": True, "order_id": "DRY-%s" % contract_symbol}

    def get_positions(self):
        return []


class TestEndToEnd(IsolatedTest):

    def test_14_two_entries_one_exit(self):
        """2 entry alerts fire via the real engine; the exit alert is ignored."""
        orig_client = fire_entries.MCPClient
        fire_entries.MCPClient = _FireFakeMCP
        self.addCleanup(setattr, fire_entries, "MCPClient", orig_client)

        alerts_path = self.tmp / "alerts.json"
        alerts_path.write_text(json.dumps({"alerts": [
            _alert("e1", "CassyTrades", "$SPY 759 PUTS 1.85"),
            _alert("e2", "clintoptions", "$QQQ 734c 0.56 0DTE",
                   posted_at="2026-09-21T09:36:00-07:00"),
            _alert("x1", "clintoptions", "out half +100%",
                   posted_at="2026-09-21T09:37:00-07:00", atype="exit"),
        ]}))

        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = fire_entries.main(["--alerts-json", str(alerts_path),
                                    "--mode", "dry_run"])
        self.assertEqual(rc, 0)
        summary = json.loads(buf.getvalue().strip().splitlines()[-1])

        # exactly 2 notifications queued, both fills; exit alert ignored
        notes = self.queued_notifications()
        self.assertEqual(len(notes), 2)
        self.assertEqual([n["kind"] for n in notes], ["fill", "fill"])
        self.assertTrue(all("contract_symbol" in n for n in notes))

        # summary counts
        self.assertEqual(summary["processed"], 2)
        self.assertEqual(summary["fired"], 2)
        self.assertEqual(summary["blocked"], 0)
        self.assertEqual(summary["rejected"], 0)
        self.assertEqual(summary["ignored"], 1)

        # ledger + positions reflect the two fired entries
        fills = self.entry_records("entry_fill")
        self.assertEqual(len(fills), 2)
        self.assertEqual(sorted(f["qty"] for f in fills), [3, 8])
        self.assertEqual(len(exits.open_positions()), 2)


# ===========================================================================
# D. WIRING CHECKS (no network)
# ===========================================================================

class TestWiring(IsolatedTest):

    def test_15_imports_clean_and_no_live_construction(self):
        """Every module imports; no MCPClient(mode="live") anywhere;
        no MCPClient( call sits at module top level (import-time construction)."""
        for name in ("config", "kill", "ledger", "mcp_client", "oauth_client",
                     "resolver", "entry_engine", "exits", "fire_entries",
                     "run_exits"):
            mod = sys.modules.get(name)
            self.assertIsNotNone(mod, "%s failed to import" % name)

        sources = {}
        for name in ("config", "kill", "ledger", "mcp_client", "oauth_client",
                     "resolver", "entry_engine", "exits", "fire_entries",
                     "run_exits"):
            sources[name] = Path(sys.modules[name].__file__).read_text()

        for name, src in sources.items():
            toks = list(tokenize.generate_tokens(io.StringIO(src).readline))
            i = 0
            while i < len(toks):
                t = toks[i]
                if (t.type == tokenize.NAME and t.string == "MCPClient"
                        and i + 1 < len(toks)
                        and toks[i + 1].type == tokenize.OP
                        and toks[i + 1].string == "("):
                    # no construction at import time: call must be indented
                    self.assertGreater(
                        t.start[1], 0,
                        "%s constructs MCPClient at module top level (line %d)"
                        % (name, t.start[0]))
                    # collect tokens until the matching close paren
                    depth = 0
                    j = i + 1
                    call_toks = []
                    while j < len(toks):
                        u = toks[j]
                        call_toks.append(u)
                        if u.type == tokenize.OP:
                            if u.string == "(":
                                depth += 1
                            elif u.string == ")":
                                depth -= 1
                                if depth == 0:
                                    break
                        j += 1
                    k = 0
                    while k < len(call_toks):
                        v = call_toks[k]
                        if (v.type == tokenize.NAME and v.string == "mode"
                                and k + 2 < len(call_toks)
                                and call_toks[k + 1].string == "="
                                and call_toks[k + 2].type == tokenize.STRING
                                and call_toks[k + 2].string.strip("'\"") == "live"):
                            self.fail(
                                "%s constructs MCPClient(mode='live') at line %d"
                                % (name, t.start[0]))
                        k += 1
                i += 1

        # dry_run is the default everywhere it matters
        sig = inspect.signature(mcp_client.MCPClient.__init__)
        self.assertEqual(sig.parameters["mode"].default, "dry_run")
        self.assertEqual(
            inspect.signature(entry_engine.process_entry)
            .parameters["mode"].default, "dry_run")
        self.assertEqual(
            inspect.signature(exits.ExitMonitor.__init__)
            .parameters["mode"].default, "dry_run")

    def test_16_notifier_json_valid(self):
        """notifier JSON parses and routes to the Trade Alerts side chat."""
        data = json.loads(NOTIFIER_JSON.read_text())
        self.assertTrue(data.get("enabled"))
        self.assertEqual(data["delivery"]["to"], NOTIFIER_CHAT_ID)
        self.assertEqual(data["delivery"]["surface"], "side_chat")

    def test_17_production_state_untouched(self):
        """Production ARMED/KILL/ledger/positions/notifications/tokens
        are byte-identical to the import-time snapshot."""
        self.assertEqual(_snapshot_production(), _PROD_SNAPSHOT,
                         "production state files changed during the suite")


if __name__ == "__main__":
    unittest.main(verbosity=2)
