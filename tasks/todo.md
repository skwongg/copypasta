# Security audit remediation

Baseline: `bd90d8cc5b2d369c9a502bc9c6d82cfbb948d812`. Authorized scope: repair the repository audit findings and all three reported parser defects, verify without real credentials/network/broker calls, push a review branch. No deployment, arming, OAuth authorization, or trading.

- [x] Fix strict premium parsing, including date-before-price, leading-dot decimals, and unpriced 0DTE; add regression tests.
- [x] Move credentials outside checkout; reject untrusted destinations/redirects and validate OAuth callback state.
- [x] Replace heuristic/raw MCP dispatch with bounded exact tools, validated results, centralized mutation gates; disable unverified conditional orders.
- [x] Separate paper/live state; serialize decisions/state transitions; reserve alerts/order intents durably; halt on corrupt/unknown live state.
- [x] Track pending, partial, failed and unknown orders without fabricating fills; recheck stop state before each mutation.
- [x] Validate source admission/freshness/replay and enforce strict trade intent; make notifications safe without an LLM/tool wake.
- [x] Remove production side effects from tests and add an isolated, network-denied test runner and a reviewable CI definition.
- [x] Review fixes independently, verify regressions and update operator/security documentation with remaining external integration requirements.
- [x] Commit, push, open ready-for-review PR for Muse, and verify published head.

Design: explicit state context identifies mode and account; a process lock covers read/decision/order/update, with durable intent before external effects. Live state must be initialized and reconciled and unknown orders stop further mutation. Paper mode uses separate local state and no OAuth. Only exact supported MCP contracts are accepted; unsupported broker schemas remain blocked pending read-only verification. Notification delivery uses deterministic text with no agent tool authority. External watcher/Hatch runtime/agent permissions are not present and cannot be certified by this PR.

## Review and verification

- All **102** tests pass through `python3 run_tests.py` on Python 3.14.7/macOS. The runner creates a temporary HOME, clears inherited credential environment variables, uses `-I -S -B`, and guards network/subprocess and user credential-file access before discovery. It is an accidental-I/O guard for this reviewed stdlib suite, not an OS sandbox for hostile native code.
- All three Muse parser cases have parser and/or engine regressions: date before price, `.56`, and unpriced `0DTE`. The chase cap and budget floor are tested end-to-end.
- Independent implementation/integration review found and closed side/order-type mismatches, incomplete response identity, raw-dispatch contract bypass, malformed URL/source-ID batch failures, namespace symlink checks, sell fills below limit, and quote/alert/market freshness after delayed previews.
- Coverage includes cumulative partial fills, pending/rejected/unknown states, crash after durable intent, no blind retries, confirmed-fill-only holdings/P&L, paper/live/account isolation, cross-process lock contention, corrupted state, kill during preview/token retrieval, and deterministic injection-resistant notification output.
- Real adapter-to-paper-engine integration reaches a flat position through the full profit ladder with no broker mutation or credential access.
- Python source syntax, notifier shell syntax, `git diff --check`, and a high-confidence secret-pattern scan pass. Scan found no private-key/GitHub/OpenAI token patterns; this is not an exhaustive secret detector or malware attestation.
- Prepared `docs/security-tests.workflow.yml` with read-only permissions, a commit-pinned checkout action and no persisted GitHub credentials. GitHub rejected the initial push when this file was under `.github/workflows/` because the current OAuth credential lacks `workflow` scope. The definition is published as a review template; remote CI is not installed or claimed to have passed.
- Source/runtime limits: actual broker schema, provider idempotency and reconciliation semantics, producer authentication deployment, installed watcher/Hatch hooks, agent permissions, host provenance, and activation remain unverified. Live CLIs, live arming, and transport mutation are deliberately disabled. Existing external hooks were not changed.

Publication: [PR #1](https://github.com/skwongg/copypasta/pull/1) is open and ready for review from `fix/security-audit` into `main`. GitHub confirmed the implementation commit `ccc536f71ec7a5f630d4ce7ff726a37896856dd2`; no workflow is active and there are no remote CI results. The follow-up documentation commit only records this publication evidence. No merge, deployment, OAuth session, or broker access occurred.

# Live exits: reconcile, reprice, broker-as-truth (2026-09-24)

Live account check on 9/24: the bot's entries fired on 9/23 (7x SPY 768P) and 9/24 (15x SPY 769C), but every exit was placed by hand. Robinhood's order rows carry no `ref_id`, so any order that wasn't reported filled in the place response could never reconcile and the account halted.

- [x] Reconcile pending orders by broker order id; for a lost place response, fall back to the single unclaimed agentic order for the same contract/side/qty created after submission. `ref_id` is checked only when the broker echoes it.
- [x] Broker is the truth for holdings: hand closes/trims become `external_close` events and the rest stays managed. Holdings the bot never bought still block.
- [x] Each exit sweep cancels a resting exit sell whose limit is above the bid (>=45s old) and re-places it at the current bid, and cancels an entry buy still open after 2 minutes.
- [x] Arm/kill lives only in the live state dir; the hooks no longer read checkout `ARMED`/`KILL`/`positions.json`. Live CLIs refuse locally before any broker call when disarmed/halted. `kill.py --mode live` finds the account itself.
- [x] Live CLIs wait up to 30s for the shared state lock instead of dropping the alert.
- [x] Notifier drains live state; the watcher wake carries the copy-trader result for the chat relay.
- [x] `tests/test_live_pipeline.py` drives the real CLIs against `tests/fake_robinhood.py` (real Robinhood shapes). 9 of its tests fail on the previous main; 146/146 pass here.

Deploy: copy the checkout and `hooks/` to the host. On the first sweep, a halted `unknown_order_outcome` from a lost place response resolves by itself. `exit_not_completed` still needs a manual look.
