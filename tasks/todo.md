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
- [ ] Commit, push, open ready-for-review PR for Muse, and verify published head.

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
