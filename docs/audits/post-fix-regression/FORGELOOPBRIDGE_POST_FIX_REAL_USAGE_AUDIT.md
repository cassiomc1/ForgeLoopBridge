# ForgeLoopBridge Post-Fix Real-World Usage Audit

## 1. Executive Summary

    Overall: PASS WITH ISSUES
    Previous loop regression: FIXED
    Overall score: 9/10

A real multi-agent workflow was executed end-to-end: Engineer (OpenCode)
posted a TaskVault build task through ForgeLoopBridge; five fresh,
ephemeral Codex CLI sessions (one killed by a harness timeout, one by a
provider quota error) each discovered work exclusively via Bridge +
canonical ForgeLoop state, each posted at most one terminal coordination
message, and each exited on its own. No repeated-diff loop, no infinite
polling, no duplicate WAITING spam, no cursor reprocessing, and no
Engineer blocking occurred at any point. Canonical completion
(taskStatus=COMPLETE, verificationStatus=VALID, next=NONE, terminal=true)
was independently verified. Two genuine blockers appeared during the run
— one Worker protocol-skipping fault and one macOS path-canonicalization
friction — both were diagnosed from evidence and classified below; neither
is a Bridge defect.

## 2. Tested Revision

- Branch: `main`
- SHA: `137b6ce743f807f2f005916cfbbbad481b2f1fcb`
  (`feat: bound the ephemeral Worker turn in examples/worker_poll.py (#28)`)
- ForgeLoopBridge version: 2.1.3 (`pyproject.toml`, `BRIDGE_API_VERSION`)
- Bridge API version: 2.1.3 (`main.py:61`)
- Typed schema version: v1 (`SUPPORTED_TYPED_SCHEMA_VERSIONS = (1,)`)
- Bounded-correction presence: VERIFIED — `examples/worker_poll.py`
  exposes `--run-mode daemon|once|bounded`, `--max-idle-polls`,
  `ONE_SHOT_COMPLETE` / `IDLE_BOUND_REACHED` exit markers, and documents
  `WAITING_FOR_ENGINEER` (README, AUTONOMY.md, CHANGELOG, tests).
- ForgeLoop: package 1.10.0, protocolVersion 1 (reads [1], writes [1]).
- GitHub CI on tested SHA: success (CI + CodeQL, 2026-09-03).

## 3. Environment

- OS: macOS 26.6.2 (darwin, arm64)
- Python: 3.14.6 (Bridge), target used stdlib + pytest only
- OpenCode: Engineer / orchestrator (this session)
- Codex CLI: 0.147.0, model `gpt-5.6-luna` (user-configured model;
  spec asked for GPT-4-family worker — recorded actual)
- ForgeLoop: 1.10.0, `/Users/cassio/Documents/github/forgeloop`
- Bridge runtime: uvicorn `main:app`, isolated DBs
  (`/tmp/flb_test.db` experiment, `/tmp/flb_neg.db` negatives),
  ports 8000 (experiment) / 8002 (negatives).

## 4. Architecture Used

    OpenCode Engineer
           ↓ (typed + plain Bridge messages)
    ForgeLoopBridge (persistent coordination transport, :8000)
           ↓ (fresh `codex exec --ephemeral` per turn, bootstrap-only prompt)
    Fresh Codex Worker (no conversation resume, no assignment in prompt)
           ↓ (canonical ops only: task-create/route/preflight/advance/run-check/complete)
    ForgeLoop (sole authority, /tmp/taskvault/.forgeloop/)
           ↓
    Target project (/tmp/taskvault, git repo, TaskVault CLI)

Communication isolation held: the Codex startup prompt contained only
role, workspace path, Bridge URL, Worker token, discovery instructions,
ForgeLoop-canonical-use instructions, bounded-turn behavior, and safety
notes. Task, acceptance criteria, design approval, repair directive, and
final approval all arrived via Bridge messages 1, 3, 5, 7.

## 5. Target Task

TaskVault MVP (`taskvault-mvp`, correlation `corr-taskvault-001`), 8 ACs:
AC1 add/list/complete e2e (`taskvault` argparse entry point); AC2 JSON
persistence across invocations; AC3 clear errors + non-zero exit on
empty title / unknown id; AC4 >=6 automated tests; AC5 README (install,
usage, storage design decision, tests); AC6 multi-file layout
(cli/store/models minimum); AC7 canonical ForgeLoop workflow, no manual
`.forgeloop` edits; AC8 pytest green. Plus 2 Engineer review constraints:
`TASKVAULT_STORE` env + `--store` flag override; corrupt JSON must error,
never silently reset.

## 6. Timeline

| time (UTC-3) | actor | Bridge ID | ForgeLoop state | action | result |
|---|---|---|---|---|---|
| 21:50 | Engineer | 1 (TASK_REQUEST, VALID) | — | post assignment (typed v1) | accepted |
| 21:50–21:54 | Worker #1 (fresh, 221s) | 2 (STATUS WAITING + WAITING_FOR_ENGINEER, VALID, reply 1) | task created, UNINITIALIZED, claims src/tests/README/pyproject | design proposal, no code yet | exit 0 |
| 21:54 | Engineer | 3 (REVIEW_RESULT APPROVED, VALID, reply 2) | — | approve design + 2 constraints | accepted |
| 21:54–21:58 | Worker #2 (fresh, 217s) | 4 (STATUS WAITING + WAITING_FOR_ENGINEER, VALID, reply 3) | RECEIVED, preflight E_PHASE_CHRONOLOGY_INVALID | exact reason code, no fabrication | exit 0 |
| 21:59 | Engineer | 5 (GENERAL repair directive + contract JSON) | — | canonical repair via Bridge | accepted |
| 21:59–22:09 | Worker #3 (fresh, killed at 600s harness timeout) | — | contract→route→preflight READY→EXECUTING→VERIFYING; full implementation | implementation complete, status post cut off | killed, productive |
| 22:09–22:14 | Worker #4 (fresh, 324s) | 6 (STATUS WAITING + WAITING_FOR_ENGINEER, VALID, reply 5) | REVIEWING, 8/8 verified | final-review request | exit 0 |
| 22:14 | Engineer | 7 (REVIEW_RESULT final APPROVAL, VALID, reply 6) | — | e2e verified, no changes | accepted |
| 22:15 | Worker #5 (fresh, 134s) | — | — | provider usage-limit error | exit 1, ENVIRONMENT |
| 22:18–22:27 | Worker #6 (fresh, 540s) | 8 (STATUS COMPLETE_REPORTED, VALID, reply 7) | COMPLETE, VALID/VALID, NONE, terminal | run-check provenance + complete | exit 0 |

## 7. Worker Turn #1

- Assignment discovery: Bridge GET only; consumed message 1 (typed
  TASK_REQUEST, correlation intact).
- Work performed: canonical `task-create` (claims src/tests/README/
  pyproject), protocol inspection; proposed `src/taskvault/` layout +
  JSON storage design instead of blind coding (legitimate design gate —
  the task required one design decision).
- WAITING behavior: exactly one STATUS_UPDATE/WAITING with
  WAITING_FOR_ENGINEER, `reply_to_id=1`, `expects_reply=true`,
  progress 1/8. No duplicates.
- Exit: code 0 at 221s, Engineer free immediately.
- Cursor: fresh-harness `pending` semantics; no reconsume observed.

## 8. Engineer Review

Two genuine reviews, both via Bridge: (a) msg 3 — design approval with
2 real constraints (env/flag storage override, corrupt-JSON behavior);
(b) msg 7 — final approval after independent e2e verification (add/
list/complete, persistence, empty-title exit 1, unknown-id exit 1,
corrupt-JSON exit 1 with file untouched, 8/8 pytest). Plus one repair
directive (msg 5, GENERAL) carrying the canonical contract JSON for the
ForgeLoop chronology fault. No auto-accept; first implementation was
verified, not trusted.

## 9. Worker Turn #2 (and later turns)

- Fresh-session proof: every turn used `codex exec --ephemeral` (no
  session persistence, no `--resume`, identical bootstrap containing no
  assignment); Worker #5's quota death and Worker #6's success on the
  same bootstrap further prove no cross-turn memory.
- Context reconstruction: Worker #2 replied to msg 3 only; Worker #4
  replied to msg 5 only; Worker #6 replied to msg 7 only — each rebuilt
  task + review + canonical state from Bridge + `.forgeloop/`.
- Cursor behavior: `reply_to` chain 1→2→3→4→5→6→7→8; original
  TASK_REQUEST never reprocessed (expected shape from spec §14 held).
- Changes: W3 implemented everything (contract, route, preflight READY,
  ruff/mypy/pytest, advance to VERIFYING); W4 verified 8/8; W6 fixed
  provenance via `run-check` and completed canonically.

## 10. Anti-Loop Regression

| Behavior | Previous run | Current run |
|---|---|---|
| Same diff repeatedly | loop | none (git-diff mentions per turn: 1/3/2/7/9, each with state change) |
| Infinite polling | loop | none (all turns exited; bounded IDLE_BOUND_REACHED proven) |
| Engineer blocked | blocked on foreground Worker | never (max turn 540s, all exits autonomous) |
| Duplicate WAITING | spam | 0 duplicates (one terminal post per turn) |
| Cursor reprocessing | re-reads | 0 (reply chain strictly forward) |
| Fresh Worker resume | impossible (memory-bound) | 3 successful fresh resumes (W2, W4, W6) |

Instrumentation: `duplicate_waiting_status_count = 0`,
`bridge_cursor_reprocessing_count = 0`.
Six fresh ephemeral Codex invocations were launched:

- four completed bounded/productive turns successfully;
- one productive implementation turn was terminated by the harness timeout;
- one invocation terminated because of provider quota.

No Codex conversation was resumed between turns.
Successful bounded/productive completions: 4. Harness-timeout
terminations: 1. Provider-quota terminations: 1. Fresh-session resumes: 3.
Worker process retries/restarts: 0 (never needed).

## 11. Worker Run-Mode Evaluation

- daemon: 9/10 — stayed alive across poll intervals, `Monitoring…`
  banner, no exit marker, SIGTERM-terminated as intended. (−1: stdout
  buffering hides liveness when redirected; run with `-u`.)
- once: 10/10 — `ONE_SHOT_COMPLETE`, consumed delta (handled=1), second
  run handled=0 (no reconsume), clean exit.
- bounded: 10/10 — new Engineer input reset idle window (handled=1),
  then 2 idle polls (~10s), `IDLE_BOUND_REACHED`, clean exit.

## 12. Resume & Recovery

9/10 — three fresh-process resumes with zero reprocessing and correct
`reply_to` chaining; restart-safe cursor files; at-least-once redelivery
on failure. (−1: cursor files are local to the harness host; a fresh
host has only `pending/history/now` bootstrap policies — documented and
sufficient here.)

## 13. Bridge Restart

Server killed and restarted against the same DB: all 7 experiment
messages survived with stable IDs, message_keys, correlation IDs, and
typed VALID states. Worker #6 continued and completed after the restart
without loss or duplication. PASS.

## 14. ForgeLoop Authority Boundary

Preserved throughout: `WAITING` never treated as lifecycle state
(Workers kept canonical phase via `next`); `COMPLETE_REPORTED` (msg 8)
posted only after `complete` returned VALID/COMPLETE/VALID and `next`
returned NONE/terminal; Bridge approvals never substituted for canonical
gates (Worker #2 refused to invent a contract; Worker #6 re-ran checks
via `run-check` for provenance instead of trusting direct pytest runs).
Two apparent "completions" confirm the boundary's value: a `complete`
invocation under a non-canonical path spelling (`/tmp` vs
`/private/tmp`) correctly returned INCOMPLETE/E_EXECUTION_REF_INVALID.

## 15. Negative Tests

| Test | Expected | Actual | Result |
|---|---|---|---|
| A. missing auth | reject | 401 | PASS |
| B. invalid Worker token | reject | 401 | PASS |
| C. invalid typed payload (empty goal) | reject, no corrupt state | 422 E_BRIDGE_TYPED_PAYLOAD_INVALID | PASS |
| D. schema_version 99 | fail closed | 422 | PASS |
| E. duplicate POST, same message_key+body | idempotent | same id=1 returned | PASS |
| F. same message_key, different body | conflict | 409 E_BRIDGE_IDEMPOTENCY_CONFLICT | PASS |
| G. persisted typed_integrity INVALID | bounded worker exits non-zero, cursor held | exit 1, no cursor file, E_BRIDGE_PERSISTED_TYPED_INVALID | PASS |
| H. handoff failure | cursor stays, redelivery eligible | 2nd run fails identically, cursor never advances | PASS |

## 16. Usage Metrics

| Metric | Value |
|---|---:|
| Total runtime (assign→COMPLETE_REPORTED) | ~37 min |
| Worker invocations | 6 |
| Successful bounded/productive completions | 4 |
| Harness-timeout terminations | 1 |
| Provider-quota terminations | 1 |
| Fresh-session resumes | 3 |
| Worker process retries/restarts | 0 |
| Engineer messages | 4 (ids 1,3,5,7) |
| Worker messages | 4 (ids 2,4,6,8) |
| Typed messages | 7/8 (all but repair directive) |
| WAITING messages | 3 |
| Duplicate WAITING messages | 0 |
| Bridge reads (worker-initiated, approx) | <10 total across turns |
| Idle polls (transport test) | 2 (bounded), 0 in worker turns |
| Cursor reprocessing | 0 |
| Worker restarts | 0 |
| Review cycles | 2 + 1 repair directive |
| Input/output/total tokens | Provider token telemetry: NOT MACHINE-AVAILABLE (provider UI only; never estimated) |

## 17. Token & Context Efficiency

Factual, non-numeric: each turn consumed only the Bridge delta newer
than its bootstrap (pending/latest) plus canonical `next`/protocol
reads; no turn re-fetched full history, re-printed a stale diff, or
re-posted WAITING. The killed Worker #3's 600s turn still produced the
entire implementation with linear log progress (contract→route→
preflight→code→lint→tests→advance) — evidence that cost went to coding,
not coordination. Provider token counters were not exposed in
non-interactive logs, so no totals are reported.

## 18. What Worked Well

1. Bounded turns + `WAITING_FOR_ENGINEER`-once + autonomous exit held
   across 4 independent fresh sessions — the exact previous failure mode.
2. Typed schema v1 carried task/review/completion with VALID integrity
   on all 7 typed messages; idempotency (same key) and conflict
   (same key, new body) behaved as documented.
3. Unsafe-handoff semantics are real: INVALID persisted state exits
   non-zero without advancing the cursor, twice in a row.
4. Restart persistence is exact (IDs, keys, correlation).
5. The transport never became the authority: every Worker treated
   ForgeLoop `next`/reason codes as truth, including refusing to fake a
   contract and re-running checks for provenance.

## 19. Remaining Friction

1. W1 skipped the documented contract-write step (GETTING_STARTED),
   causing E_PHASE_CHRONOLOGY_INVALID; W2 over-conservatively declared
   "no supported command" instead of following the documented
   agent-authored contract step. Evidence: worker1/2 logs, task state
   RECEIVED without contract.json. Impact: one extra Engineer repair
   turn. Actor: worker model. Likely cause: bootstrap says "do not
   manually edit ForgeLoop-managed state" without distinguishing
   agent-authored inputs (contract.json) from managed outputs.
2. macOS `/tmp`→`/private/tmp` symlink duality: `complete`/`next` under
   different spellings disagree (INCOMPLETE/E_EXECUTION_REF_INVALID vs
   VALID/COMPLETE/NONE-terminal). Evidence: worker6 log + Engineer
   verification. Impact: confusion, one wasted complete attempt. Actor:
   environment + ForgeLoop path binding. Not a Bridge defect.
3. W3 (600s) was killed by the harness timeout mid-final-post despite
   linear productive progress. Evidence: worker3 log tail (advance to
   VERIFYING as last action), files complete. Impact: extra Worker #4
   turn. Actor: orchestration (my timeout). Not a Bridge defect.
4. Provider quota killed Worker #5 (exit 1, nothing posted). Impact:
   ~35 min delay. Actor: environment. Classification: ENVIRONMENT_FAILURE.
5. Token usage not observable from `codex exec` logs (only a UI label).
   Impact: §19/§20 precision limited to provider telemetry NOT MACHINE-AVAILABLE. Frequency:
   always. Likely cause: provider-side accounting only.

## 20. Bugs

Actual bugs: none found in ForgeLoopBridge during this run. All 8
negative tests, 3 run-modes, restart, resume, and completion behaved per
contract. Observations above are worker-model, environment, or
orchestration issues, plus documentation-hardening suggestions below.

## 21. Suggested Improvements From Real Usage

P1 reliability:
- Document the agent-authored vs managed-state distinction
  (contract.json is Worker-written input per GETTING_STARTED) in
  `examples/AUTONOMY.md` + worker bootstrap guidance, to prevent
  repeat of the W1-skip/W2-over-refusal pattern. Evidence: §19.1.
P2 efficiency/agent UX:
- Recommend `--max-idle-polls` values and harness timeouts ≥15 min for
  implementation turns (a 600s cap killed a productive turn). Evidence:
  §19.3.
- Note macOS `/tmp` symlink canonicalization for `--path` consistency
  in worker guidance. Evidence: §19.2.
P3 developer experience:
- Document how to read provider token usage for `exec` runs, or mark
  §19-style accounting explicitly best-effort. Evidence: §19.5.

No P0. Nothing here moves canonical ForgeLoop responsibilities into
Bridge.

## 22. Absolute Bound Hardening

Observation: `bounded` resets its idle window on any new Engineer input,
so a steady stream of Engineer messages could keep one bounded process
alive indefinitely. This run provides NO evidence of harm: all 4 worker
turns exited promptly (134–540s) because each turn ended with a terminal
post + exit, not with idle. Real Codex turns are additionally capped by
provider limits. Verdict: `ABSOLUTE_BOUND_HARDENING` recorded as
worthwhile future defense-in-depth (`--max-polls` /
`--max-runtime-seconds`), priority P3, NOT required by this test's
evidence. Do not add it as a fix for a defect that was not observed.

## 23. Scorecard

| Area | Score |
|---|---|
| Core coordination | 10/10 |
| Worker lifecycle | 9/10 |
| Anti-loop behavior | 10/10 |
| Fresh-session resume | 10/10 |
| Cursor correctness | 10/10 |
| At-least-once safety | 10/10 |
| ForgeLoop boundary preservation | 10/10 |
| Reliability | 8/10 |
| Token/context efficiency | 8/10 |
| Developer experience | 8/10 |
| Documentation | 9/10 |
| Observability | 7/10 |

    FINAL SCORE: 9/10

(Reliability −2 for external quota/timeout interruptions handled
gracefully; efficiency −2 for unobservable provider token counts;
observability −3 for buffered daemon output, log-only token UI, and the
path-spelling trap belonging to the environment.)

## 24. Final Verdict

    Was the previously observed loop fixed? YES
    Can OpenCode safely orchestrate ephemeral Codex Workers? YES
    Can a fresh Codex session resume without previous conversation memory? YES
    Is bounded Worker execution better than the previous permanent foreground Worker model? YES
    Does ForgeLoopBridge remain transport-only? YES
    Is the coordination/token overhead justified? YES
    (4 Engineer + 4 Worker messages carried a full
    design→implement→verify→complete cycle; coordination reads were a
    handful per turn; redundant-cost categories measured zero.)
