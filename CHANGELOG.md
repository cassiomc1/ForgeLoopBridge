# Changelog

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
