# Changelog

## Unreleased

Future changes will be recorded here.

## 2.2.0 - 2026-09-09

### Added

- Added an optional, read-only Live Execution Observer integration for
  `shell.online`, disabled by default and isolated from Bridge and ForgeLoop
  authority boundary.
- Added `--run-mode once` and `--run-mode bounded [--max-idle-polls N]` to the
  Worker example for finite Engineer-launched turns; daemon mode remains the
  default.
- Added an executable ForgeLoop compatibility boundary that fails closed for
  unknown declared Protocol, Integration API, consumed schema, and task/context
  feature versions while keeping unrelated additive capabilities independent.
- Added a shadcn-inspired light/dark web interface theme with system detection,
  persisted preference, an accessible toggle, and visible keyboard focus states.

### Changed

- Synchronized the observed ForgeLoop baseline to package `1.11.1` while
  preserving Protocol v1, Integration API v1, Bridge API `2.2.0`, and Typed
  Message Schema v1. Package version remains informational rather than a
  ForgeLoop compatibility decision.
- Documented additive `repositoryIndex` v1, provider-neutral Repository Search,
  and the ForgeLoop Persistent Search Transport boundary. No Bridge API
  endpoint or request/response contract was removed or made incompatible; no
  tgrep dependency and no new Bridge authority were added.
- Clarified bounded Worker lifecycle, `WAITING_FOR_ENGINEER`, observer
  lifetime, ForgeLoop canonical authority, and the distinction between Bridge
  delivery retry and ForgeLoop action retry.
- Published the post-fix real-world regression audit and its bounded evidence
  manifest under `docs/audits/post-fix-regression/`.
- Retained the historical ForgeLoop `1.10.1` transaction/profile hardening and
  `1.10.2` Ripwire introduction as historical provenance only.

### Fixed

- Hardened observer startup, stderr handling, cleanup, and Worker exit
  semantics; normal exits no longer trigger false observer-stop failures, while
  Ctrl-C and SIGTERM return `130` and `143` respectively.
- Prevented observer security failures from rerunning a Worker command after
  it may already have modified the target project.
- Preserved at-least-once typed delivery by keeping failed handoffs and
  transient outbox failures pending, honoring bounded `Retry-After`/backoff,
  and quarantining only permanent failures.
- Added stable bounded Worker exit/error markers and counted the first-start
  bootstrap handoff in reported work.

### Security

- Observer integration never stores the shell.online E2EE password, accepts
  only HTTPS provider URLs, requires read-only E2EE sessions, uses shell-free
  bounded subprocesses, and performs targeted cleanup only.
- Typed outbox persistence rejects authentication material, uses bounded atomic
  storage with restricted file permissions where supported, and preserves
  credential-safe retry behavior.

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
