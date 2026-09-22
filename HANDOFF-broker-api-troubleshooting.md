# Copy-Trader Troubleshooting Handoff — Broker API Returns Empty Option Data

**Date:** 2026-09-22 ~11:15 AM PT
**Repo:** https://github.com/skwongg/copypasta (main @ 99027fc)
**Status:** System is ARMED for live trading but has never fired. Zero positions, zero fills.

## What's Working

1. **Watcher** — Polls 4 X accounts every 60s during market hours, detects entries/exits in ~7-30s. Alerts relay to Trade Alerts side chat correctly.

2. **Hook trigger fix** (commit 99027fc) — `hooks/scripts/trade-post-watcher.sh` now runs the sign-and-fire pipeline BEFORE `wake` (wake doesn't return, so anything after it was dead code). Also exports `COPYTRADER_CONFIG` so `load_policy()` reads `policy.json` sources (was falling back to None defaults, refusing all live entries with "policy.sources is not configured").

3. **Pipeline logic** (proven in dry-run/paper mode):
   - `sign_alerts.py` signs correctly (HMAC-SHA256)
   - Admission accepts valid signatures
   - Freshness gate correctly rejects stale alerts (>300s)
   - Arm check correctly blocks when disarmed
   - All 122 tests pass, 4 pre-existing skips

## What's Broken

**The broker's MCP API returns empty option data.** Specifically:

- `mcp.call_tool('get_option_chains', {'underlying_symbol': 'QQQ'})` → 0 chains
- `mcp.call_tool('get_option_chains', {'underlying_symbol': 'SPY'})` → 0 chains
- `mcp.call_tool('get_option_instruments', {'chain_symbol': 'QQQ', 'expiration_dates': '2026-09-22', 'type': 'call', 'tradability': 'tradable'})` → 0 instruments

**But the connection itself works:**
- `MCPClient(mode='live', account_number='DISCOVERY')` connects fine
- `list_tools()` returns 75 tools
- `discover_agentic_account()` finds the agentic account (4921****)
- No auth errors

## Impact

Without option chain/instrument data, the resolver (`resolver.py`) cannot map a trade signal (e.g. "$QQQ 734C 0DTE") to a concrete `option_id`. Without an `option_id`, `entry_engine.py` cannot construct an order. **This is why nothing has fired all day** — not the hook ordering (fixed), not the policy config (fixed), but the market data API returning empty.

Two genuine signals were caught today and would have fired if the API worked:
- 10:50 PT: @CassyTrades $AMZN 260 CALLS @ 1.22, exp Sep 25 (fire.log shows "policy.sources not configured" — that was before the config fix)
- 10:56 PT: @CassyTrades $GOOG 360 CALLS @ 1.14, exp Sep 25 (same blocker)

## What To Investigate

1. **Why are `get_option_chains` / `get_option_instruments` returning empty?**
   - Is this an account permissions issue? (Does the agentic account 4921**** have options trading approved?)
   - Is it a market-hours issue? (Tested at ~11:15 AM PT on a Tuesday — market should be open)
   - Is it an API parameter issue? (Tried `underlying_symbol`, `chain_symbol`, various expirations — all empty)
   - Is the MCP broker adapter (Robinhood?) having an outage or returning empty for this endpoint?

2. **Check `mcp_client.py`:**
   - `_tool_for("chains")` → `"get_option_chains"`
   - `_tool_for("find_contracts")` → `"get_option_instruments"`
   - How does `entry_engine.py` call these? Does it pass different parameters than what was tested manually?
   - Is there a working code path in the test suite that mocks these? (See `tests/test_dryrun.py` `fixture()` — it uses hardcoded instrument data, bypassing the API entirely)

3. **Verify account state:**
   - Can you pull account info / balances via the MCP tools?
   - Is there an `option_level` on the agentic account? (Discovery checks for it — it passed, so probably yes)
   - Are there any open orders or positions? (Should be zero)

## Key Files

- `mcp_client.py` — MCP broker adapter (lines 292+ for MCPClient class, 552+ for call_tool)
- `resolver.py` — Maps trade text → option_id (needs chain/instrument data)
- `entry_engine.py` — `process_entry()` — the firing logic
- `fire_entries.py` — CLI entry point; `live_main()` at line ~73
- `config.py` — `load_policy()` reads `COPYTRADER_CONFIG` env var
- `hooks/scripts/trade-post-watcher.sh` — The watcher → fire bridge (fixed in 99027fc)

## Safety Constraints (do not weaken)

- Never touch Silas's main Robinhood account — agentic account (4921****) only
- Never fabricate alert signing
- 300s freshness gate, HMAC signature, $500 target / $5k exposure / $2.5k daily loss, 10% chase cap, -60% stop, +50/+200/+300 ladder — all must stay
- "Stop copy trading" = create KILL marker + disarm

## Question to Answer

**Why does the live MCP broker API return zero option chains and zero instruments for QQQ/SPY during market hours, when the connection, auth, and account discovery all work?**
