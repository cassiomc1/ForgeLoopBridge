# ForgeLoopBridge Post-Fix Improvement Plan

> **Status:** PARTIALLY IMPLEMENTED / HISTORICAL FOLLOW-UP RECORD
> - P1-1: IMPLEMENTED by PR #29 cleanup (AUTONOMY.md agent-authored vs
>   managed-state section + doc test).
> - P2-1: IMPLEMENTED by PR #29 cleanup (AUTONOMY.md harness-timeout
>   guidance, non-normative).
> - P2-2: IMPLEMENTED by PR #29 cleanup (AUTONOMY.md macOS path
>   canonicalization note).
> - P3-1: OPEN (absolute-bound hardening, future defense-in-depth).
> - P3-2: OPEN (provider token-accounting guidance; audit reports
>   NOT MACHINE-AVAILABLE).

Derived exclusively from the 2026-09-02 real-usage audit
(`FORGELOOPBRIDGE_POST_FIX_REAL_USAGE_AUDIT.md`). No P0 items: no Bridge
correctness or safety defect was observed. Nothing below moves canonical
ForgeLoop responsibilities into Bridge.

## P1 — Reliability

### P1-1. Clarify agent-authored inputs vs managed state in Worker guidance

- Observed problem: Worker #1 skipped the documented contract-write step
  (task created without `contract.json`, auto-route recorded
  `ROUTE_VALIDATED` without prior `CONTRACT_VALIDATED`); Worker #2 then
  refused the documented repair, claiming "no supported command", costing
  one full Engineer repair turn (Bridge msg 5) plus a Worker turn.
- Evidence: archived `worker1.log` / `worker2.log` (see `evidence/EVIDENCE_MANIFEST.md`),
  (single `task-create` without `--contract-file`, no contract write),
  `worker2.log` ("no supported contract-write command is available"),
  ForgeLoop `E_PHASE_CHRONOLOGY_INVALID`, audit §19.1.
- Root cause: bootstrap/AUTONOMY wording ("do not manually edit
  ForgeLoop-managed state") does not distinguish Worker-authored inputs
  (`contract.json`, per ForgeLoop GETTING_STARTED Step 1) from
  ForgeLoop-managed outputs (events, work-state, receipts).
- Files affected: `examples/AUTONOMY.md`,
  `examples/worker_poll.py` (module docstring §§ boundary/contract),
  `README.md` (Worker section referencing `WAITING_FOR_ENGINEER`).
- Exact required behavior: guidance must state (a) `contract.json` is a
  Worker-authored input that MUST be written after `task-create` and
  before `route`; (b) canonical reason codes must still be reported
  exactly once with `WAITING_FOR_ENGINEER` when genuinely blocked;
  (c) nothing else under `.forgeloop/` is ever hand-edited.
- Implementation approach: docs-only edit + one `test_docs.py` assertion
  that the contract-step sentence exists in AUTONOMY.md.
- Tests required: `ruff check .`, `pytest -q` (incl. `test_docs.py`).
- Acceptance criteria: a fresh Worker given only the updated bootstrap
  writes `contract.json` before `route` on a scratch task; doc test passes.
- Compatibility impact: none (docs only).
- Risk: minimal.

## P2 — Efficiency / agent UX

### P2-1. Publish harness timeout and idle-bound sizing guidance

- Observed problem: a productive implementation turn (Worker #3: full
  TaskVault implementation, ruff/mypy/pytest green, advanced to
  VERIFYING) was killed by a 600s orchestrator timeout just before its
  terminal Bridge post, forcing an extra Worker #4 turn.
- Evidence: `worker3.log` (linear progress, last action `advance …
  --to VERIFYING`), audit §19.3.
- Root cause: no documented guidance on sizing orchestrator timeouts vs
  `--max-idle-polls` for implementation-scale turns.
- Files affected: `examples/AUTONOMY.md` (or README Worker section).
- Exact required behavior: documented recommendation — implementation
  turns: harness timeout ≥15 min; `--max-idle-polls 2–3` for review-gated
  turns; `once` for pure assignment-consumption turns.
- Implementation approach: docs-only.
- Tests required: `pytest -q` doc-consistency tests keep passing.
- Acceptance criteria: guidance present and referenced from run-mode docs.
- Compatibility impact: none. Risk: minimal.

### P2-2. Warn about macOS `/tmp` path canonicalization in Worker guidance

- Observed problem: `forgeloop complete`/`next` invoked with `--path
  /tmp/taskvault` vs `/private/tmp/taskvault` disagreed
  (INCOMPLETE/`E_EXECUTION_REF_INVALID` vs VALID/COMPLETE/NONE-terminal),
  costing one wasted completion attempt and Engineer re-verification.
- Evidence: `worker6.log` (both invocations), audit §19.2.
- Root cause: environment symlink duality interacting with ForgeLoop's
  canonical project-root binding; Workers inherit whichever spelling the
  harness used.
- Files affected: `examples/AUTONOMY.md` (one paragraph + bootstrap hint:
  resolve the workspace with `realpath` once and reuse the spelling).
- Exact required behavior: guidance only; no Bridge code change.
- Tests required: none beyond existing suite.
- Acceptance criteria: paragraph present.
- Compatibility impact: none. Risk: minimal.

## P3 — Developer experience

### P3-1. Absolute-bound hardening (`--max-polls` / `--max-runtime-seconds`)

- Observed problem: none — recorded as hardening only. `bounded` resets
  its idle window on new Engineer input, so adversarial/accidental
  message streams could extend one bounded process indefinitely.
- Evidence: code inspection (`worker_poll.py` bounded loop) + absence of
  any such event in this audit (all turns exited in 134–540s).
- Root cause: idle-bounded ≠ absolutely bounded by design.
- Files affected: `examples/worker_poll.py`, `tests/test_worker_poll.py`,
  README run-mode docs.
- Exact required behavior (if adopted): optional `--max-polls N` and/or
  `--max-runtime-seconds N`; on exhaustion print a stable exit marker
  (e.g. `POLL_BOUND_REACHED` / `RUNTIME_BOUND_REACHED`), exit 0 if the
  last handoff was safe (cursor persisted) else non-zero without
  advancing the cursor; defaults preserve current behavior.
- Implementation approach: extend argparse + bounded loop counters +
  monotonic deadline; unit tests for marker/exit/cursor semantics.
- Tests required: new `test_worker_poll.py` cases + full suite.
- Acceptance criteria: bounds trigger deterministically in tests;
  default behavior byte-identical to current.
- Compatibility impact: additive CLI flags only.
- Risk: low.

### P3-2. Token/context accounting guidance

- Observed problem: provider token counts for `codex exec` turns were not
  recoverable from logs (audit records NOT AVAILABLE rather than
  estimates).
- Evidence: `worker*.log` ("tokens used" UI label without numbers).
- Files affected: audit docs only (note the limitation where §19-style
  metrics are defined).
- Exact required behavior: one paragraph stating accounting is
  best-effort for non-interactive runs.
- Risk: none.
