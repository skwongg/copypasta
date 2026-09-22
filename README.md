# Copy Trader — Operator Manual

Copies options entries from the three tailed X accounts
(@CassyTrades, @clintoptions, @capricekayem) into Silas's Robinhood account
via the Robinhood agent MCP. Exits stay manual for now: `run_exits.py`
(when built) will run on a ~2-minute schedule and report rungs through the
same notification path.

Nothing here places an order unless the trader is ARMED *and* the run mode
is `live`. Dry-run is the default everywhere.

## Architecture

```
watch.py (every ~2 min, market hours)
  └─ JSON: {"status":"alert","alerts":[{id,handle,text,posted_at,url,type},...]}
      │
trade-post-watcher.sh (~/hooks/scripts/, poll 120s)
  ├─ wakes Trade Alerts side chat (entries + exits, human-readable)   [existing]
  └─ alert) branch → [ARMED file exists?]
          │
          │ YES                              NO → nothing fires
          ▼
fire_entries.py --alerts-json /dev/stdin --mode live
  ├─ for each alert with type == "entry":   (exits ignored — manual)
  │    entry_engine.process_entry(alert, mcp, mode)
  │      ├─ MCPClient(token_provider=oauth.get_valid_token, mode)
  │      ├─ action=fire → places order via MCP → fill price → notification
  │      └─ action=block/reject/error → notification with reason
  ├─ appends result.notification as one JSON line to notifications.jsonl
  └─ prints {"processed":N,"fired":n,"blocked":n,"rejected":n,"ignored":n}
      │
      ├── ledger.jsonl (per-order audit record, from entry_engine)
      └── positions.json (open positions, from entry_engine/run_exits.py)
      │
copy-trader-notifier hook (~/hooks/definitions/copy-trader-notifier.json,
poll 60s) — NO arming needed; it only relays text
  └─ notifier/copy-trader-notifier.sh
       drains notifications.jsonl → wakes Trade Alerts side chat
       (35a06c41-4e5c-458d-b2ab-6170fa3e92dd) with one short message
       per notification; then truncates the queue
```

## File inventory

| File | What it does |
|---|---|
| `fire_entries.py` | CLI bridge: entry alerts → `entry_engine.process_entry` → queues `notifications.jsonl`; prints a summary JSON. Entries only fire when ARMED + `--mode live`. |
| `entry_engine.py` | Entry engine: resolves the alert to a contract, sizes, limit-prices, and places (or blocks) the order. Returns `EntryResult`. |
| `mcp_client.py` | Robinhood agent MCP client; `MCPClient(token_provider, mode)`; `check_options_support()` answers the tools/list capability question. |
| `oauth_client.py` | OAuth flow: `auth-url` prints the approval URL; `get_valid_token()` supplies/refreshes the bearer token. |
| `kill.py` | Kill switch CLI: `--arm/--disarm/--halt/--reset/--status`. |
| `resolver.py` | Alert text → contract resolution (used by entry_engine). |
| `ledger.py` | Audit ledger helpers (append/query `ledger.jsonl`). |
| `config.py` | Shared constants/paths. |
| `notifier/copy-trader-notifier.sh` | Hook script: drains `notifications.jsonl`, wakes the worker, truncates the queue. |
| `notifier/copy-trader-notifier.json` | Hook definition registering the above (60s poll → Trade Alerts side chat). |
| `tests/test_fire_entries.py` | Self-test for `fire_entries.py` (stubbed engine/client, dry_run only). |
| `README.md` | This manual. |

## Setup sequence

1. **Get the OAuth authorization URL** (operator, parent-supervised):
   ```bash
   cd ~/workspace/copy-trader && python oauth_client.py auth-url
   ```
   Silas opens the URL in his desktop browser, approves, and pastes the
   authorization code back **via the parent agent — never in chat logs**.

2. **Exchange the code** under parent supervision (see `oauth_client.py`
   usage; the token lands in `.tokens.json`, mode 0600).

3. **Verify the capability question** against the live MCP:
   ```python
   from mcp_client import MCPClient
   from oauth_client import get_valid_token
   MCPClient(token_provider=get_valid_token, mode="dry_run").check_options_support()
   ```
   This answers: *"Does the Robinhood agent MCP expose the tools needed
   to place options orders?"* — it binds the stable capability keys
   (`option_quote`, `find_contracts`, `review_option_order`,
   `place_option_order`, `cancel_order`, `positions`, `orders`) to the
   real tool names returned by `tools/list`, and reports
   `{"options_orders": bool, "conditional_orders": bool, ...}`. Do not
   proceed to arming until `options_orders` is `true`.

4. **Register the notifier hook** (parent does this):
   ```bash
   cp ~/workspace/copy-trader/notifier/copy-trader-notifier.json ~/hooks/definitions/
   ```
   The runtime picks up hook definitions from `~/hooks/definitions/`; the
   60-second poll starts relaying notifications immediately. This hook is a
   pure relay — no arming, no orders.

5. **Wire the watcher into the entry engine.** Add this 4-line snippet to
   the `alert)` branch of `~/hooks/scripts/trade-post-watcher.sh`
   (**parent must apply — do not run unreviewed**):
   ```bash
   if [ -f "$HOME/workspace/copy-trader/ARMED" ]; then
     printf '%s' "$RESULT" | "$HOME/workspace/copy-trader/fire_entries.py" --alerts-json /dev/stdin --mode live >>"$HOME/workspace/copy-trader/fire.log" 2>&1 || true
   fi
   ```
   How it works: when the watcher finds new posts, the same `$RESULT` JSON
   that wakes the Trade Alerts chat is also piped into `fire_entries.py`.
   Entries only auto-fire while the ARMED file exists; the
   `|| true` keeps a firing failure from breaking the alert wake. The
   notifier hook needs no arming because it only relays text.

## Arming / disarming

```bash
python kill.py --arm      # create ARMED — entries may auto-fire in live mode
python kill.py --disarm   # remove ARMED — watcher alerts become text-only again
python kill.py --halt     # engage KILL — halts ALL order placement immediately
python kill.py --reset    # clear KILL, back to prior arming state
python kill.py --status   # show armed / halted state
```

Armed requires `ARMED` present AND `KILL` absent. When in doubt, `--halt`.

## Dry-run vs live

- `fire_entries.py --mode dry_run` (default): builds a dry_run
  `MCPClient` — no order-mutating tool calls are allowed, and OAuth
  tokens are not required. Use for all testing.
- `fire_entries.py --mode live`: requires OAuth to be completed
  (`get_valid_token` available), otherwise it exits non-zero with
  "OAuth not completed; cannot fire live."

**Standing rule:** while the market is closed, live testing waits for
market hours *and* Silas's explicit go-ahead. No exceptions.

## State files

| File | Contents | Notes |
|---|---|---|
| `ledger.jsonl` | One audit record per order attempt (entry + exit) | append-only; never delete |
| `positions.json` | Open tailed positions the engine is managing | read/written by entry_engine / run_exits.py |
| `notifications.jsonl` | Queue of chat-ready notifications, one JSON object per line | drained + truncated by the notifier hook every ~60s |
| `.tokens.json` | OAuth client registration + tokens | **0600**, never paste contents into chat |
| `ARMED` / `KILL` | Empty marker files | arming state; see `kill.py` |
| `fire.log` | fire_entries.py stdout from the watcher pipe | rotation: keep small, inspect on failures |
