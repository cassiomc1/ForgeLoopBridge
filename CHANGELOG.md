# Changelog

## Unreleased

- Align the current ForgeLoop compatibility baseline with published package
  1.8.1 while preserving Protocol v1, Integration API v1, and capability-first
  authority decisions.

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
