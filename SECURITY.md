# Security status

**Live trading was enabled on 2026-09-22 after this audit (see README); the text below describes the audited offline revision.** The code changes address the repository defects identified in the September 21, 2026 audit of commit `bd90d8cc5b2d369c9a502bc9c6d82cfbb948d812`, including Muse's three parser reproductions. They do not certify the host, outside agents, deployed hooks, or a real broker integration.

The original repository review found no obvious malware or deliberate prompt-injection payload. That conclusion is limited to the inspected source/history. It does not establish absence of malware in the external runtime or make untrusted alert text authoritative.

## Implemented boundaries

- Paper operation uses explicit local fixtures without OAuth or networking. Paper/live/account state occupies separate namespaces, with namespace identity validated when reading and writing.
- The process lock covers the decision and order lifecycle, not just the final write. State corruption, unresolved orders, or unexplained broker holdings stop trading decisions.
- Durable intent IDs precede submission. Only validated cumulative broker fills affect live accounting. Pending responses, partial fills, errors, and ambiguous timeouts are not fabricated full fills. Unknown outcomes are reconciled rather than retried blindly.
- The transport's mutation boundary requires an explicitly live client, explicit account, current arm/kill eligibility, exact tool names, supported schemas, and the local order contract. Raw calls cannot bypass the same account, limit-order, quantity, price, or idempotency requirements. The separate source-level live gate is disabled.
- OAuth storage is external to Git checkouts, private, atomic, and rejects symlinks, hardlinks, and special files. Pinned endpoints refuse redirects. Callback state is validated, tied to the saved verifier, expires, and is consumed before exchange.
- Notifications are deterministic text with a fixed vocabulary. Untrusted text, remote descriptions, and exception bodies never become instructions to a notification agent. The repository notifier neither sources an external runtime nor wakes an LLM.

These controls depend on the integrity of the executor's code and host. A general agent that can read broker tokens, issue arbitrary HTTP requests, or edit executable code can bypass an in-process Python policy. Deployment must enforce that separation outside the model.

## Audit finding coverage

Regression tests are evidence of the specified offline behavior. They are not broker acceptance tests. Run them through `python3 run_tests.py`; do not substitute a credential-bearing deployed environment.

| Finding | Change | Regression coverage |
|---|---|---|
| **F1: tokens could enter public Git history** | Credential files moved outside Git checkouts; private atomic writes; token/credential artifacts ignored. No legacy token migration. | `tests/test_oauth_security.py`: storage location, permissions, symlink/hardlink refusal, special-file refusal. |
| **F2: paper and live shared positions/P&L** | State root is partitioned by mode and explicit account; mismatched state/client identity is refused. Paper simulation does not call broker mutation methods. | `tests/test_state_security.py`, `tests/test_engine_security.py`, `tests/test_dryrun.py`: namespace separation and paper round trips. |
| **F3: errors and pending orders were treated as fills** | Strict MCP result decoding and explicit preview approval; durable intents; identity-checked pending/partial/unknown reconciliation; holdings/P&L updated from confirmed fill deltas only. | `tests/test_transport_security.py`, `tests/test_engine_security.py`: tool errors, response IDs, required identity, pending/partial/rejected fills, timeout/crash recovery. |
| **F4: conditional helper bypassed safety gates** | Conditional helper always refuses. Exact tool allowlist and mutation checks cover typed and raw dispatch; unverified live access remains source-disabled. | `tests/test_transport_security.py`, `tests/test_engine_security.py`: raw dispatch, paper mutations, missing idempotency, conditional helper. |
| **F5: expiry/decimal parsing fabricated premiums** | Separate expiry and price parsing; leading-dot prices supported; missing/ambiguous prices rejected; exact contract identity required. | `tests/test_resolver_security.py` and `tests/test_engine_security.py`: Muse examples and chase-cap consequences. |
| **F6: stale, replayed, or unverified-source alerts reached execution** | Strict schema/source/URL/age admission; transactional alert reservation; intent idempotency. Live admission additionally requires pinned source IDs and an authenticated producer signature. | `tests/test_engine_security.py`, `tests/test_fire_entries.py`: stale/future data, replay despite edited text, source/schema rejection and signature validation. The deployed producer is still absent. |
| **F7: concurrent workers lost state; corruption failed open** | Account process lock spans decisions and updates; private atomic state; explicit validation; missing/corrupt live state and unexpected broker holdings block execution. | `tests/test_state_security.py`, `tests/test_engine_security.py`: cross-process locks, corrupt/wrong-namespace data, atomic-write failure, unexpected positions/open orders. |
| **F8: tests touched deployed queues/credentials** | Old test implementations replaced; imports no longer delete queues or inspect real token files. Isolated runner sets temporary paths and blocks networking before discovery. | `run_tests.py`; rewritten `tests/test_fire_entries.py` and `tests/test_dryrun.py`; all suites use dummy data and isolated state. |
| **F9: KILL could become stale before a submission** | Arm/kill checks occur again at submission and immediately before outbound dispatch, after token retrieval. Marker checks reject invalid permissions and symlinked namespaces. | `tests/test_transport_security.py`, `tests/test_engine_security.py`, `tests/test_dryrun.py`: kill during token retrieval/preview, raw dispatch and marker integrity. |

Additional coverage checks redirect refusal, OAuth endpoint substitution, state-bound callback replay/expiry, error redaction, notification injection strings, executable-bid exits, and an actual MCP adapter → paper entry → paper exit integration. Tool schemas used in tests are explicitly fixtures.

### Muse's three reproductions

| Alert form | Correct behavior |
|---|---|
| `$SPX 150C 9/18 2.20 entry` or an `exp 9/25` expiry before the price | Expiry is not a premium; a distinct `2.20` entry price remains `2.20`. An expiry-only post remains unpriced. |
| A premium written `.56` | Parsed as `0.56`, never `56.00`. |
| `$QQQ 734c 0DTE` with no price | Premium is explicitly absent; no order proceeds. It is not represented as a zero-priced entry. |

The resolver regressions reproduce all three. Engine regressions verify that erroneous oversized chase caps cannot reappear through those parser cases.

## Live rollout remains blocked

The following work is separate from this remediation and must be reviewed before any live activation:

1. **Verify the real broker contract through an authorized read-only inspection.** Confirm exact account-specific tool names, input schemas, output envelopes, account binding, permissions, quotes/timestamps, preview behavior, and supported order semantics. Enforce a read-only boundary outside the model during discovery. Tool-name discovery alone is insufficient.
2. **Prove order recovery and accounting against that contract.** Confirm that the broker supports an idempotency field with the required guarantees, preserves the correlation identifier in order snapshots, reports cumulative partial/terminal fills and execution prices, and permits complete order/position reconciliation. If that contract is unavailable, redesign and review the adapter; do not infer equivalence or silently invent fallback fields.
3. **Review the complete producer and runtime.** The X watcher, immutable source-ID mapping, `trade-post-watcher.sh`, Hatch runtime, scheduler, recipient-agent prompts/tool grants, and host provenance are not provided here. The live HMAC admission check has offline coverage, but its producer, key provisioning, replay boundary, and deployment are not installed or verified. A handle/URL or producer-supplied `entry` flag is not source authentication.
4. **Separate credentials from untrusted agents.** Only a constrained executor should hold the broker credential. Post-reading and notification agents must be unable to read it, send broker HTTP requests, alter executable code/risk policy, or arm live trading. Validate account and instrument permissions independently of LLM output.
5. **Disable and inspect legacy external installations.** A disabled JSON hook in this checkout does not modify copies already registered on a host. Manually disable old agent-wake notifier/watcher/scheduler configurations. Review installed hashes and permissions before replacing them. This change does not claim to have modified any external deployment.
6. **Reconcile migration and operations.** Old local position/P&L/queue/marker files may mix simulated and real events and are never automatically imported. Establish actual holdings and open orders from the broker before creating reviewed live state. Define handling for interruptions, unresolved submissions, partial cancellations, manual account activity, and missing market data.
7. **Complete a separate activation review.** Entry and exit CLIs refuse live mode; live arming refuses; `LIVE_TRADING_ENABLED` is false with no environment bypass. These deliberate independent blocks are not a checklist of switches to flip. Activation needs a reviewed implementation, exact revision/runtime evidence, appropriate integration verification, and the account owner's explicit decision.

## Operational limitations

- Halt/disarm prevents new submissions, including automated exits. It does not cancel orders already in flight or liquidate holdings. The user must monitor those orders and positions manually. There is an unavoidable distinction between checking a kill marker and an order already accepted by the broker.
- A stop threshold is a polling trigger. Bid-priced limit exits may never fill after a price move; there are no verified broker-native stop/OCO orders. Keep this separate from claims of guaranteed loss protection.
- The calendar covers 2026–2027 only, using [NYSE holiday and early-close dates](https://www.nyse.com/trade/hours-calendars) verified for this review. It does not establish every instrument's hours, expiration rules, settlement, or broker restrictions. Unsupported years fail closed.
- Paper fills are simplified assumptions. P&L excludes fees, and the model does not establish liquidity, achievable execution, buying power, taxes, or economic suitability.
- The notification drain records delivery after output succeeds. A crash between output and persistence can cause a duplicate message; it must never cause a duplicate order. Notification rendering is not a trading authority boundary by itself.
- Host binaries, certificate stores, proxies, external plugins, and installed runtime components are outside the code tests. No antivirus certification or exhaustive dependency/runtime attestation is implied.

## Sensitive information

Keep account credentials, authorization codes, verifier values, full OAuth callback URLs, and private account records out of Git, issues, pull-request comments, agent prompts, and logs. OAuth callback exchange accepts the complete URL through stdin and consumes its saved state once. Failed or expired exchange requires a new authorization flow.

The original audit found no committed trading token; this remediation does not claim that an undisclosed credential leak occurred. If a separate exposure review finds leaked credentials, use the broker's revocation/rotation process. Do not upload credentials as reproduction evidence.
