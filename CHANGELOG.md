# Changelog

## Unreleased

- Declared the supported ForgeLoop version set in code and made the documented
  protocol-first handshake executable. `bridge_protocol.forgeloop_context` now
  exports `SUPPORTED_FORGELOOP_PROTOCOL_VERSIONS`,
  `SUPPORTED_FORGELOOP_INTEGRATION_API_VERSIONS`,
  `SUPPORTED_FORGELOOP_CONTEXT_SCHEMA_VERSIONS` and
  `SUPPORTED_FORGELOOP_CONTEXT_FEATURE_VERSIONS`, and `forgeloop_boundary_status()`
  fails closed on any declared protocol, schema, Integration API or consumed-feature
  version outside those sets — and on a host that advertises the canonical
  `task/context` capability without declaring a protocol version. Previously the
  only executable gate read capability booleans and no version at all, so a
  projection from an unknown ForgeLoop protocol generation was consumed as
  `CANONICAL`. The balanced compatibility fallback stays reserved for a host that
  advertises no adaptive capability; an unsupported boundary returns `UNAVAILABLE`
  with `fallback: NONE`. Package version is still never a compatibility decision.
- Stopped test-pinning the informational ForgeLoop package baseline string. The
  documentation tests now assert the normative Protocol and Integration API
  versions against the code constants, and check only the shape of the recorded
  observation, so the enforced number is the one that governs compatibility.

- Added an optional read-only Live Execution Observer integration for shell.online.
  The integration is disabled by default and does not change the Bridge API,
  Typed Message Schema v1, SQLite schema, or ForgeLoop authority boundary.
- Hardened the Live Execution Observer: ForgeLoopBridge never stores the
  E2EE password (operators use shell.online's owner-side `shell list`),
  provider stderr uses a single blocking reader, and post-start security
  violations fail closed with targeted cleanup and no Worker re-execution.
- Documented and fixed the observed-Worker launcher lifetime contract:
  task-bound sessions close naturally on a normal exit (no `shell kill`, no
  false `OBSERVER_STOP_FAILED`), while Ctrl-C exits `130` and `SIGTERM`
  exits `143` so an interrupt is never reported as a successful Worker turn.

- Publish the post-fix real-world regression audit under
  `docs/audits/post-fix-regression/` (report, improvement plan, evidence
  manifest, compact structured evidence; raw execution logs archived outside
  Git with SHA-256 manifest entries).
- Clarify agent-authored protocol inputs vs ForgeLoop-managed state in
  `examples/AUTONOMY.md`, and add operational guidance on harness timeout
  sizing, macOS path canonicalization, and best-effort provider token
  accounting.
- Note in `examples/worker_poll.py` that the bounded run mode is
  idle-bounded, not absolute-runtime-bounded.
- Add bounded run modes to `examples/worker_poll.py` (`--run-mode once` and
  `--run-mode bounded [--max-idle-polls N]`) so an Engineer-launched ephemeral
  CLI Worker can consume the current coordination and exit instead of polling
  forever; `--run-mode daemon` remains the default and unchanged.
- Print stable bounded exit markers (`WORKER_POLL_EXIT`, `WORKER_POLL_ERROR`)
  and exit non-zero on an unsafe cycle, leaving the cursor unadvanced so failed
  handoffs, `typed_integrity: INVALID` rows, and transport failures stay
  eligible for at-least-once redelivery.
- Count the first-start bootstrap handoff in the reported `handled=<n>` so a
  fresh bounded turn cannot look idle after consuming the open instruction.
- Document the bounded Worker turn, the `WAITING_FOR_ENGINEER` status
  convention, and the ephemeral CLI Worker pattern, keeping ForgeLoop the sole
  authority for lifecycle, verification, and completion.

## 2.1.3 - 2026-09-02

- Align the current ForgeLoop compatibility baseline with published package
  1.10.0 while preserving Protocol v1, Integration API v1, and capability-first
  authority decisions.
- Document `advisoryContextProviders` v1 as optional, lazy, opt-in,
  provider-neutral, Integration API-only context that Bridge never recalls,
  persists, executes, or treats as authoritative.
- Recognize `canonicalHandoffs` v2 statuses and acceptance reason codes while
  keeping `HANDOFF_NOTICE` separate from receiving-harness acceptance.
- Document `reconcile-continuity` as a read-only diagnostic whose lint warnings
  do not block Bridge coordination, verification, or completion.

## 2.1.2 - 2026-08-30

- Treat HTTP 408, 425, 429, and 5xx typed-message delivery failures as
  transient instead of quarantining them.
- Honor bounded `Retry-After` guidance and defer pending outbox retries without
  blocking normal Worker polling.
- Add `Retry-After` to server-generated posting and SSE-ticket rate-limit
  responses.
- Remove redundant Worker token fields from official status POST bodies; Bearer
  authentication remains the preferred delivery mechanism.
- Refresh documentation to distinguish Bridge transport backpressure, Bridge
  protocol errors, and canonical ForgeLoop blockers.

## 2.1.1 - 2026-08-30

- Hardened typed Worker outbox delivery: no credentials in persisted requests,
  atomic bounded storage, startup replay, transient retry retention, and
  permanent-failure quarantine.
- Added explicit persisted typed-integrity status and fail-closed Worker
  dispatch for malformed database representations.
- Added normalized typed-envelope size limits and stable 413 error handling.
- Added `DECISION_NOTICE`, decision option consistency checks, and strict reply
  expectation semantics.
- Distinguished requested and resolved verification scope while preserving the
  deprecated v1 `scope_mode` field.
- Advertised Bridge API 2.1.1 typed capabilities and added CI checks for the
  inline frontend JavaScript on supported Python platforms.
