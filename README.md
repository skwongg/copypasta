# Copypasta

Copypasta parses allowlisted trade alerts and simulates options entries and an exit ladder using explicitly supplied local market data. This revision repairs defects found in the September 2026 audit and is ready for code review and offline testing.

**Live trading is disabled. Do not connect this revision to a trading-capable agent or deploy the old watcher instructions.** The broker adapter and external deployment have not been verified. Passing the offline tests is not authorization or evidence that real orders are safe. See [SECURITY.md](SECURITY.md) for the remaining gates.

## What runs now

```text
Local alert JSON + local market fixture
  → strict admission and price/contract parser
  → paper entry/exit engine
  → locked, account-and-mode-scoped state
  → deterministic terminal notifications
```

Paper execution does not obtain OAuth tokens or contact Robinhood. The CLI requires an explicit market fixture. Paper orders are simulated inside the engine; `PaperClient.place_option_order()` refuses broker submission. Simulation assumes fills at the chosen price and does not model broker acceptance, liquidity, slippage, fees, or actual settlement.

The entry engine checks alert age/source policy, replay reservations, exact contract identity, fresh quotes, a 10% maximum chase cap, a per-trade budget, open exposure, and daily realized loss. The parser treats unsupported or ambiguous text as requiring review. An alert marked `type: "entry"` does not bypass these checks.

The exit monitor uses the quote's executable bid. It attempts half of remaining contracts at +50%, half of the then-remaining contracts at +200%, and the remainder at +300%; fractional contracts are rounded down. A bid at or below 40% of the entry fill triggers the remaining-position stop. Exits use limits rounded down to cents. These are polling decisions, **not broker-native protective orders**; a stop trigger or submitted limit does not guarantee execution.

## Run the isolated tests

Python 3.10 or newer and a Unix-like host with `fcntl`/directory-descriptor support are required. The project uses the Python standard library; no package installation is needed.

```bash
python3 run_tests.py
```

The runner establishes temporary home/state/credential directories and blocks network access before discovering the tests. Its guards protect this reviewed suite against accidental I/O; they are not an operating-system sandbox for arbitrary hostile code. Use it as the verification entry point. Tests use dummy credentials, fake transports, explicit clocks, and local fixtures; they do not prove Robinhood's real schemas or behavior. The former tests that touched deployed home-directory files have been replaced.

The proposed Linux CI definition is in `docs/security-tests.workflow.yml`. It is a review template, not an installed workflow: the publishing credential lacks GitHub `workflow` scope. An authorized maintainer can review and install it under `.github/workflows/`. No remote CI result is claimed.

## Paper CLI inputs

A market fixture has exactly two top-level keys, `contracts` and `quotes`. Contract symbols use the 21-character OCC form: a six-character space-padded underlying, six date digits, `C` or `P`, and an eight-digit strike in thousandths.

Example `market.json`:

```json
{
  "contracts": [
    {
      "underlying": "SPY",
      "expiry": "2026-09-25",
      "strike": 700.0,
      "option_type": "call",
      "contract_symbol": "SPY   260925C00700000"
    }
  ],
  "quotes": {
    "SPY   260925C00700000": {
      "contract_symbol": "SPY   260925C00700000",
      "bid": 0.95,
      "ask": 1.0,
      "as_of": "2026-09-21T15:00:00+00:00"
    }
  }
}
```

Example `alerts.json`:

```json
{
  "alerts": [
    {
      "id": "100",
      "handle": "cassytrades",
      "source_id": "paper-fixture-source",
      "type": "entry",
      "text": "$SPY 700c 9/25 1.00 entry",
      "posted_at": "2026-09-21T15:00:00+00:00",
      "url": "https://x.com/cassytrades/status/100"
    }
  ]
}
```

These are format examples with historical timestamps. The CLI uses the current clock: update timestamps and all corresponding contract/expiry fields for a paper exercise during supported market hours. Quote age is limited to 30 seconds; alert age defaults to five minutes. Stale fixtures are rejected. Reusing a reserved alert ID will not trade again, even if its text changes. The automated tests provide reproducible examples with explicit clocks without relaxing these checks.

Use a dedicated namespace for the exercise:

```bash
export COPYTRADER_STATE_DIR="$HOME/.local/state/copypasta-paper-demo"
python3 kill.py --arm --mode dry_run --account demo
python3 fire_entries.py --mode dry_run --account demo --market-json market.json --alerts-json alerts.json
python3 run_exits.py --mode dry_run --account demo --market-json market.json
python3 notifications.py --mode dry_run --account demo
python3 kill.py --disarm --mode dry_run --account demo
```

Keep `--account` and `COPYTRADER_STATE_DIR` consistent across commands. `fire_entries.py` prints deterministic result messages and a JSON summary. The notification command renders committed fill/status events from state; it can repeat information already printed by the originating CLI. There is no live scheduler or watcher installed by these commands.

## State and operational controls

Default state lives outside the checkout under:

```text
~/.local/state/copypasta/<mode>/<account>/
  state.json
  .lock
  ARMED
  KILL
```

`COPYTRADER_STATE_DIR` selects another root. The default paper account is `paper`; live state requires an explicit account identifier. The state envelope records mode and account. One process lock covers the entire read/check/intent/submission/update transaction; writes are atomic. Invalid, corrupt, or missing live state is never treated as an empty account.

Order intents are saved before submission. Pending, partial, and unknown outcomes reserve their identity and stop further submissions until reconciled. Holdings, exit latches, and realized P&L change only from confirmed cumulative fills; a timeout is not a rejection or a reason to resubmit. Normalized broker responses must preserve the order's contract, side, quantity, and client idempotency identifier.

`--halt` creates `KILL`; `--disarm` removes `ARMED`. Both stop new entries **and automated exits**. They do not cancel orders already submitted, liquidate positions, or ensure flat holdings. Existing orders and holdings need manual monitoring. `--reset` only clears the kill marker: it does not resolve unknown orders or clear durable reconciliation halts. If the arm marker remains, clearing `KILL` restores eligibility for otherwise valid paper work.

Legacy `positions.json`, `ledger.jsonl`, notification queues, and checkout-level markers are never automatically imported. Do not copy mixed paper/live history into the new state. Any eventual live migration needs separate broker reconciliation and review.

## Notifications and external installations

Notifications use fixed message codes, bounded numeric fields, and validated contract symbols. Alert text, arbitrary handles/URLs, broker descriptions, and exception bodies are not forwarded to an LLM. The notifier shell script only invokes the deterministic renderer; it does not source Hatch or wake an agent. Its repository hook definition is disabled.

**Updating this repository does not disable an older hook already installed elsewhere.** Manually disable the old external notifier/watcher/scheduler configuration before reviewing any replacement deployment. The X watcher, `trade-post-watcher.sh`, Hatch runtime, recipient-agent permissions, and host scheduler are absent from this repository. Their safety and installed state remain unknown.

## Credentials and broker integration

No credentials are needed for paper runs or tests. OAuth files now belong in `~/.local/share/copypasta/credentials`, or an explicit absolute `COPYTRADER_CREDENTIAL_DIR` outside any Git checkout. The directory must be private (`0700`), files private (`0600`), and paths must not traverse symlinks. Writes are atomic. Legacy credentials beside the code are not imported.

The OAuth implementation accepts only pinned HTTPS endpoints, refuses redirects, and binds a full loopback callback URL to a saved PKCE verifier and single-use, age-limited state. Its exchange CLI accepts the callback through stdin, not an argument. Do not paste callback URLs, codes, tokens, or verifiers into agent conversations or shell arguments. OAuth authorization is a separate reviewed operation, not a paper setup step.

The MCP adapter allows exact named tools and validates a conservative schema subset. Unknown tools, unsupported schemas, tool errors, response-ID mismatches, and malformed fills fail closed. Its normalized argument/result shapes are **offline contracts, not verified Robinhood account schemas**. An advertised tool name alone does not establish compatibility or permission.

Live execution has independent blocks in the entry/exit CLIs, live arming, and the transport's source-level `LIVE_TRADING_ENABLED = False` gate. There is no environment override or supported live-enable command. Removing one check is not a rollout procedure. Conditional-order support remains unavailable.

## Files

| Area | Files |
|---|---|
| Admission, parser, policy | `admission.py`, `resolver.py`, `config.py` |
| Trading decisions and confirmed fills | `entry_engine.py`, `exits.py`, `order_state.py` |
| State, controls, accounting | `trade_state.py`, `kill.py`, `ledger.py` |
| Offline CLI/data | `paper_client.py`, `fire_entries.py`, `run_exits.py` |
| Broker boundary and credentials | `mcp_client.py`, `oauth_client.py` |
| Deterministic notifications | `notifications.py`, `notifier/` |
| Verification | `run_tests.py`, `tests/` |

The market calendar supports only 2026–2027 and is not a complete instrument-specific trading calendar. P&L excludes fees. See [SECURITY.md](SECURITY.md) before considering any live integration.
