# ForgeLoopBridge — ForgeLoop 1.5 Integration Update Plan

> **HISTORICAL / SUPERSEDED.** This plan records an earlier ForgeLoop 1.5-era
> synchronization stage and is not a current operating instruction.
> Implemented planning material remains useful for project history, but current
> compatibility guidance belongs to
> `FORGELOOPBRIDGE_CURRENT_FORGELOOP_SYNC_UPDATE_PLAN.md`.
> Superseded for current ForgeLoop integration work by
> `FORGELOOPBRIDGE_CURRENT_FORGELOOP_SYNC_UPDATE_PLAN.md`.

> **For agentic workers:** execute this plan task-by-task, preserving ForgeLoop as the only protocol authority. Prefer an official ForgeLoop structured integration when the active host exposes one; otherwise use the project-local ForgeLoop CLI. Do not manually synthesize ForgeLoop lifecycle, claim, recovery, ledger, ownership, or completion state.

**Goal:** Align ForgeLoopBridge with the current ForgeLoop 1.5.0 architecture, recovery/ownership model, structured integration surfaces, deterministic `next` control flow, and multi-task coordination model without turning ForgeLoopBridge into a second ForgeLoop implementation.

**Architecture:** ForgeLoopBridge remains the coordination and communication layer between Engineer and Worker. ForgeLoop remains the sole authority for protocol state under `.forgeloop/`; protocol operations flow through `@cassiomc1/forgeloop/integration`, the official MCP adapter when available, or the project-local CLI as fallback. The Bridge may carry task identifiers, message types, summaries, PR links, and status projections, but must never derive canonical lifecycle, ownership, recovery, or completion truth itself.

**Tech Stack:** Python 3, FastAPI, Pydantic, aiosqlite/SQLite, HTML/CSS/JavaScript, SSE, pytest/httpx, Docker, ForgeLoop core 1.5.x / protocol v1 / Integration API v1.

**Source requirements:** current `ForgeLoopBridge` `main` plus canonical ForgeLoop 1.5.0 documentation and public compatibility boundary: `README.md`, `AGENTS.md`, `PROTOCOL_INTEGRATION.md`, `docs/UNIVERSAL_INTEGRATION.md`, `docs/MCP.md`, `docs/CROSS_HARNESS_CONTINUITY.md`, `docs/GETTING_STARTED.md`, `docs/CLI_REFERENCE.md`, and `CHANGELOG.md` in `cassiomc1/forgeloop`.

## Global constraints

- ForgeLoop core compatibility target: `1.5.x`.
- ForgeLoop protocol remains `protocolVersion: 1`.
- ForgeLoop Integration API remains version `1`.
- Active task recovery state requires a recovery-aware ForgeLoop reader; never infer ownership from `task.json` or `recovery.json` alone.
- Prefer an official structured ForgeLoop integration when exposed by the host; use the project-local CLI otherwise.
- The Bridge must never directly create, edit, delete, repair, or reinterpret ForgeLoop-managed lifecycle, recovery, claim, lock, ledger, receipt, or execution artifacts.
- `acknowledgeRecovery`, Engineer approval, Worker approval, or a board message never creates `HOST_ATTESTED` authority.
- `forgeloop complete = VALID` is necessary for protocol-verified completion but the Worker must also query `forgeloop next` and follow it until terminal state or an explicit blocker.
- Preserve the current security properties of the Bridge: mandatory separate tokens, authenticated message reads, timing-safe comparison, Markdown sanitization, rate limiting, WAL/busy timeout, non-root Docker execution, and SSE with polling fallback.
- Do not add a ForgeLoop runtime dependency to the Python server merely to mirror protocol state. The Bridge remains transport/coordination infrastructure.
- Existing boards/databases must remain readable after the update.
- All new fields added to messages must be backward-compatible and optional for old clients.

---

## Scope and priority

### P0 — Required for ForgeLoop 1.5 correctness

1. Rewrite the Engineer prompt around read-only canonical verification instead of passive trust in Worker summaries.
2. Rewrite the Worker prompt around compatibility handshake, task discovery, canonical `next`, continuity/recovery, structured integration preference, and terminal confirmation.
3. Update the autonomy contract so Engineer↔Worker agreement cannot fabricate ForgeLoop authority.
4. Document canonical recovery/ownership semantics: `RESUME_RECOVERED_TASK`, `task-resume`, `RESOLVE_RECOVERY_INCONSISTENCY`, validated claim projection, and fail-closed behavior.
5. Update the architecture and terminology so Bridge = coordination, ForgeLoop = protocol authority, Integration API/MCP/CLI = protocol execution surfaces.
6. Require `protocol-info --json` compatibility inspection before creating or resuming protocol state.
7. Require `next` after implementation/completion before publishing final status.

### P1 — Recommended Bridge evolution for first-class multi-task operation

1. Add optional `task_id` and `message_type` fields to Bridge messages.
2. Migrate existing SQLite databases safely without deleting or rewriting message history.
3. Add task-aware filtering to GET requests while retaining the current unfiltered board behavior.
4. Add task/message metadata to SSE payloads and the browser UI.
5. Update the Worker polling example to preserve and forward task context.
6. Extend tests for migration, validation, filtering, SSE payload compatibility, and old-client behavior.

### P2 — Quality and anti-drift hardening

1. Add a small compatibility/version statement for ForgeLoop 1.5.x / protocol v1 / Integration API v1.
2. Add documentation checks that fail when legacy ForgeLoop workflow phrases return.
3. Archive or rewrite `improves.md` so it does not present an old audit as the current project roadmap.
4. Perform the repository-wide documentation review defined at the end of this plan.

---

## Target architecture after the update

```text
                         ┌─────────────────────────────┐
                         │        ForgeLoopBridge      │
                         │ coordination / Markdown /   │
                         │ task refs / status / PRs    │
                         └──────────────┬──────────────┘
                                        │
                         board messages │ board messages
                                        │
             ┌──────────────────────────┴──────────────────────────┐
             │                                                     │
             ▼                                                     ▼
┌──────────────────────────┐                         ┌──────────────────────────┐
│         Engineer         │                         │          Worker          │
│ review / decisions /     │                         │ implementation / checks │
│ read-only verification   │                         │ PR / protocol execution │
└────────────┬─────────────┘                         └────────────┬─────────────┘
             │                                                    │
             │ official structured integration when available    │
             │ otherwise project-local CLI                       │
             └──────────────────────┬─────────────────────────────┘
                                    ▼
                    ┌──────────────────────────────────┐
                    │   ForgeLoop canonical authority  │
                    │ Integration API / MCP / CLI      │
                    │ writes canonical `.forgeloop/`  │
                    └──────────────────────────────────┘
```

### Non-negotiable boundary

ForgeLoopBridge may report or index:

- `task_id`;
- `message_type`;
- PR URL;
- Worker-provided completion summary;
- Worker/Engineer discussion;
- decision records;
- blocker summaries;
- a copied `nextAction` for convenience.

ForgeLoopBridge must not independently decide:

- effective or historical claims;
- current mutation ownership;
- whether recovery is valid;
- whether a lock is stale/live/unknown/corrupt;
- lifecycle phase truth;
- verification truth;
- whether completion is actually `VALID`;
- whether an external authority grant is trusted;
- whether a recovered task may mutate again.

Those answers come only from canonical ForgeLoop operations.

---

## Task 1 — Establish the ForgeLoop 1.5 compatibility contract

**Files:**
- Modify: `README.md`
- Modify: `examples/AUTONOMY.md`
- Create or update as part of documentation work: no additional runtime file required.

**Produces:**
- A single documented compatibility boundary used by both prompt templates and all examples.

- [ ] **Step 1: Add a compatibility section near the beginning of `README.md`**

The section must state exactly these semantics:

```markdown
## ForgeLoop compatibility

ForgeLoopBridge targets ForgeLoop core `1.5.x`, ForgeLoop protocol `1`, and
Integration API `1`.

Before creating or resuming ForgeLoop task state, the active execution host
must inspect the installed project's public compatibility boundary with
`forgeloop protocol-info --json` (or the equivalent official structured
integration capability call).

If the host exposes an official ForgeLoop structured integration, prefer it for
protocol operations. Otherwise use the project-local ForgeLoop CLI. Never
manually synthesize ForgeLoop-managed lifecycle, claim, recovery, ledger,
ownership, or completion state.
```

- [ ] **Step 2: Document the version distinction**

Explicitly state that package version, protocol version, and Integration API version are different compatibility dimensions. Do not claim MCP/npm publication merely because the repository contains the implementation.

- [ ] **Step 3: Add recovery-awareness requirements**

Document that a project with active recovery state must be processed by a reader that supports validated claim projection. A reader that does not understand that projection must fail closed instead of inferring ownership from `task.json` alone.

- [ ] **Step 4: Review links to ForgeLoop**

Prefer links to canonical integration/continuity/MCP docs rather than only linking the ForgeLoop repository root.

- [ ] **Step 5: Verify no old statement contradicts the new compatibility section**

Search the repository for phrases equivalent to:

```text
never run ForgeLoop yourself
create or resume a ForgeLoop task
complete VALID then done
recovery.json determines ownership
Engineer approval authorizes recovery
```

Any occurrence must be rewritten or explicitly scoped so it cannot contradict ForgeLoop 1.5 semantics.

- [ ] **Step 6: Commit the compatibility-contract documentation change**

```bash
git add README.md examples/AUTONOMY.md
git commit -m "docs: align Bridge compatibility with ForgeLoop 1.5"
```

---

## Task 2 — Rewrite the Engineer prompt for canonical read-only verification

**Files:**
- Modify: `README.md` — Engineer system prompt and review workflow.

**Consumes:** ForgeLoop 1.5 compatibility contract from Task 1.

**Produces:** An Engineer role that remains non-implementing but can independently verify protocol truth through canonical read-only surfaces.

- [ ] **Step 1: Replace the absolute prohibition on running ForgeLoop**

Remove the current concept equivalent to:

```text
You never execute code or run ForgeLoop yourself.
```

Replace it with this responsibility boundary:

```text
You do not implement target-project code and you do not perform ForgeLoop
mutations for the Worker. You MAY use canonical read-only ForgeLoop operations
or a readonly official structured integration to independently verify protocol
compatibility, task status, audit results, ownership projection, continuity,
and completion state.
```

- [ ] **Step 2: Add a read-only verification sequence**

The Engineer prompt must instruct the Engineer to validate, when its host exposes the capability:

```text
1. protocol compatibility
2. task identity
3. task status
4. canonical ownership projection when claims/recovery matter
5. audit/completion result
6. terminal next action
7. PR contents and `.forgeloop/` publication policy
```

Example CLI fallback commands:

```bash
forgeloop protocol-info --json
forgeloop task-show --task <task-id> --json
forgeloop status --task <task-id> --json
forgeloop audit --task <task-id> --json
forgeloop next --task <task-id> --json
```

The prompt must state that structured integration equivalents are preferred when officially available in the Engineer host.

- [ ] **Step 3: Separate verification from authority**

Add:

```text
Your APPROVED message is a project decision, not ForgeLoop host authority.
Never represent an Engineer/Worker board agreement as HOST_ATTESTED authority,
trusted installation authority, force-recovery authority, or any other
ForgeLoop authority class that requires an external trusted boundary.
```

- [ ] **Step 4: Tighten PR review completion criteria**

The Engineer must not approve only because the Worker posted `complete: VALID`. Approval should require available evidence that:

```text
- the task ID matches the requested work;
- canonical completion is VALID;
- `next` reports terminal state / no remaining action, or reports an explicitly understood non-terminal action;
- ownership/recovery is consistent when relevant;
- the PR matches the task contract and publication expectations.
```

- [ ] **Step 5: Update the example Engineer task message**

The example must instruct the Worker to discover existing tasks before creating a new one and to finish only after canonical `next` reaches terminal state or returns an explicit blocker.

- [ ] **Step 6: Commit**

```bash
git add README.md
git commit -m "docs: upgrade Engineer prompt for canonical verification"
```

---

## Task 3 — Rewrite the Worker prompt around `protocol-info`, task discovery, `next`, and structured integration

**Files:**
- Modify: `README.md` — Worker system prompt and recommended workflow.
- Modify: `examples/worker_poll.py` — explanatory comments/status example only in this task; runtime task metadata comes later.

**Produces:** A Worker flow aligned with ForgeLoop 1.5 rather than a fixed legacy command recipe.

- [ ] **Step 1: Make integration selection explicit**

The Worker prompt must begin protocol operations with:

```text
If your execution host exposes an official ForgeLoop structured integration,
prefer it. Otherwise resolve and use the project-local ForgeLoop CLI. Never
write ForgeLoop-owned state manually.
```

- [ ] **Step 2: Add compatibility handshake before mutation**

CLI fallback:

```bash
forgeloop protocol-info --json
```

The Worker must fail closed if the installed compatibility boundary cannot safely read/write the active project's protocol/recovery state.

- [ ] **Step 3: Replace “create or resume” with deterministic discovery**

Use this conceptual sequence:

```bash
forgeloop task-list --json
```

Then:

```text
- If the Engineer references an existing task, select that task.
- If one existing task clearly matches the requested work, inspect it rather than creating another task.
- If multiple candidates exist, use task identity and board context; do not silently merge tasks.
- For an existing task, query canonical `next` before mutation.
- Create a new task only when no existing task represents the requested work.
```

CLI fallback for an existing task:

```bash
forgeloop status --task <task-id> --json
forgeloop continuity --task <task-id> --json
forgeloop reconcile-continuity --task <task-id> --json
forgeloop inspect --task <task-id> --json
forgeloop next --task <task-id> --json
```

Continuity remains optional; absence of `continuity.json` must not be treated as task invalidity.

- [ ] **Step 4: Keep new-task creation canonical**

Only after discovery shows that a new task is appropriate:

```bash
forgeloop task-create --task <task-id> --claim <path> --json
```

Then write the task contract according to the canonical schema and route/preflight through ForgeLoop.

- [ ] **Step 5: Make `next` the control authority**

The Worker must follow the action returned by canonical ForgeLoop state rather than assuming a fixed sequence is always valid.

The README may still show a normal happy path, but it must say that `forgeloop next --task <task-id> --json` overrides a hard-coded recipe when ForgeLoop reports a different safe action.

- [ ] **Step 6: Update verification/completion flow**

The normal path should include:

```bash
forgeloop advance --task <task-id> --to VERIFYING
forgeloop prepare-completion --task <task-id> --json
forgeloop run-check --task <task-id> --id <check-id> --requirement "<requirement>" -- <exact argv>
forgeloop advance --task <task-id> --to REVIEWING
forgeloop audit --task <task-id> --json
forgeloop complete --task <task-id> --json
forgeloop next --task <task-id> --json
```

Do not document shell-string execution as an equivalent of exact-argv ForgeLoop execution.

- [ ] **Step 7: Update final Worker status format**

Replace a vague “Done” message with a structured Markdown status such as:

```markdown
### Status — <task-id>

**State:** COMPLETE
**PR:** <pull-request-url>

**ForgeLoop:**
- task: `<task-id>`
- complete: `VALID`
- terminal: `true`
- nextAction: `NONE`
- checks: <concise observed evidence summary>

No ForgeLoop-owned state was synthesized outside the canonical integration/CLI.
```

If terminal state has not been reached, the Worker must report the actual canonical blocker/action rather than `Done`.

- [ ] **Step 8: Update comments in `examples/worker_poll.py`**

The polling example must say that the placeholder integration point should hand the instruction to an agent that follows the Worker prompt above; it must not imply that receiving a board message itself creates a new ForgeLoop task.

- [ ] **Step 9: Commit**

```bash
git add README.md examples/worker_poll.py
git commit -m "docs: align Worker workflow with ForgeLoop 1.5"
```

---

## Task 4 — Add canonical recovery and ownership rules

**Files:**
- Modify: `README.md`
- Modify: `examples/AUTONOMY.md`

**Produces:** Explicit fail-closed behavior for recovered, inconsistent, or legacy recovery tasks.

- [ ] **Step 1: Add a dedicated recovery section to `README.md`**

Document these invariants:

```text
- Recovery is not completion.
- `recovery.json` alone is not ownership truth.
- Effective ownership comes from ForgeLoop's canonical validated claim-state projection.
- Historical claims remain reserved when ownership evidence is inconsistent.
- A recovered task rejects normal mutation until `task-resume` safely reacquires claims.
- Never create, edit, or delete `recovery.json` manually.
- Never force a new task merely to bypass recovery inconsistency or overlapping claims.
```

- [ ] **Step 2: Document `RESUME_RECOVERED_TASK`**

When canonical `next` returns `RESUME_RECOVERED_TASK`, the Worker should inspect task/ownership state and then use the canonical resume operation.

CLI fallback:

```bash
forgeloop task-show --task <task-id> --json
forgeloop task-resume --task <task-id> --json
```

If `task-resume` fails due to scope conflict, lock inconsistency, dirty checkout, or another canonical guard, preserve recovery state and report the blocker.

- [ ] **Step 3: Document `RESOLVE_RECOVERY_INCONSISTENCY`**

When returned by `next`, the Worker must stop normal mutation and follow ForgeLoop's repair/validation guidance. It must not infer that claims are released simply because one artifact is absent or appears terminal.

- [ ] **Step 4: Document recovery acknowledgement semantics**

State clearly:

```text
`--acknowledge-recovery` / `acknowledgeRecovery` records caller acknowledgement.
It does not manufacture host attestation and does not let an actor self-issue
trusted recovery authority.
```

- [ ] **Step 5: Update the autonomy contract**

Add a section named `ForgeLoop authority boundary` with rules equivalent to:

```markdown
## ForgeLoop authority boundary

Engineer and Worker may negotiate project decisions through the board, but
neither agent may convert that agreement into ForgeLoop authority that the
canonical protocol requires to originate outside actor-controlled state.

Examples include trusted install-capable execution grants, host-attested
recovery authority, force/destructive recovery authority, and other operations
whose canonical risk class requires an external trusted capability.

If the required authority is not already supplied by the execution host, post
`BLOCKED` with the exact ForgeLoop error/action and continue polling. Do not
fabricate an approval token, edit authority/recovery state, or reinterpret a
board `APPROVED` message as host attestation.
```

- [ ] **Step 6: Preserve autonomy for reversible non-blocking product decisions**

Do not accidentally turn every decision into a human approval gate. The Bridge autonomy contract should continue to allow Engineer↔Worker negotiation and safe reversible defaults while respecting ForgeLoop's distinct authority boundary.

- [ ] **Step 7: Commit**

```bash
git add README.md examples/AUTONOMY.md
git commit -m "docs: define recovery ownership and authority boundaries"
```

---

## Task 5 — Make completion reporting terminal-state aware

**Files:**
- Modify: `README.md`
- Modify: `examples/worker_poll.py`

**Produces:** Completion language that distinguishes validator-backed completion, publication, PR state, and remaining canonical actions.

- [ ] **Step 1: Replace “VALID means done” wording**

Every workflow description should use the following rule:

```text
`forgeloop complete` returning `VALID` proves protocol completion validation.
Before posting the final Bridge status, query canonical `next` and follow it
until `terminal: true` / `nextAction: NONE` or an explicit blocker/action is
returned.
```

- [ ] **Step 2: Separate protocol completion from PR/publication state**

A status should independently describe:

```text
protocol verification
terminal next action
PR created/not created
PR merged/not merged when relevant
publication/deployment state when relevant
```

Do not imply that ForgeLoop `VALID` itself opens, approves, merges, deploys, or publishes a PR.

- [ ] **Step 3: Update all example status messages**

Ensure examples cannot teach agents to post a fabricated `VALID`, `terminal`, or `nextAction` value. Each example must say the values are copied from canonical ForgeLoop output.

- [ ] **Step 4: Commit**

```bash
git add README.md examples/worker_poll.py
git commit -m "docs: make completion reporting terminal-state aware"
```

---

## Task 6 — Add optional task-aware message metadata to the Bridge

> This is a P1 product evolution. It improves ForgeLoop multi-task coordination but does not make the Bridge a protocol authority.

**Files:**
- Modify: `main.py`
- Modify: `tests/test_main.py`
- Modify: `static/index.html`
- Modify: `examples/worker_poll.py`
- Modify: `README.md`

**Produces:** Backward-compatible `task_id` and `message_type` metadata on Bridge messages.

### Interface contract

`MessageCreate` gains optional fields:

```python
task_id: str | None = Field(default=None, min_length=1, max_length=200)
message_type: str | None = Field(default=None, min_length=1, max_length=40)
```

`MessageOut` gains:

```python
task_id: str | None
message_type: str | None
```

Canonical normalized message types supported by the Bridge:

```text
TASK
STATUS
DECISION_NEEDED
DECISION_RESOLVED
DECISION_TAKEN
BLOCKED
REVIEW
GENERAL
```

The Bridge treats these as communication metadata only. It does not infer ForgeLoop lifecycle from them.

- [ ] **Step 1: Write failing API tests for optional metadata**

Add tests equivalent to:

```python
async def test_post_roundtrip_with_task_metadata(client):
    r = await client.post(
        "/api/messages",
        headers=HEADERS_ENGINEER,
        json={
            "content": "## Task auth-feature",
            "task_id": "auth-feature",
            "message_type": "TASK",
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["task_id"] == "auth-feature"
    assert body["message_type"] == "TASK"


async def test_legacy_post_without_task_metadata_still_works(client):
    r = await post(client, "legacy message", HEADERS_ENGINEER)
    assert r.status_code == 200
    assert r.json()["task_id"] is None
    assert r.json()["message_type"] is None
```

- [ ] **Step 2: Run the focused tests and verify they fail before implementation**

```bash
pytest tests/test_main.py -k "task_metadata or legacy_post" -v
```

Expected before implementation: failure because the response model/database does not expose the new fields.

- [ ] **Step 3: Add safe SQLite schema migration**

After the existing `CREATE TABLE IF NOT EXISTS messages`, inspect the table schema:

```sql
PRAGMA table_info(messages)
```

If `task_id` is absent, execute:

```sql
ALTER TABLE messages ADD COLUMN task_id TEXT
```

If `message_type` is absent, execute:

```sql
ALTER TABLE messages ADD COLUMN message_type TEXT
```

Do not rebuild or truncate the table. Existing rows remain valid with NULL metadata.

- [ ] **Step 4: Update database reads/writes**

All message SELECT statements and the INSERT statement must include the new columns.

The INSERT shape becomes conceptually:

```sql
INSERT INTO messages (role, content, created_at, task_id, message_type)
VALUES (?, ?, ?, ?, ?)
```

- [ ] **Step 5: Validate message types without protocol inference**

Use a fixed Bridge-level allowlist and normalize input to uppercase. Reject unsupported values with HTTP 422/Pydantic validation or a deterministic 400 error. Do not map message types to ForgeLoop phases.

- [ ] **Step 6: Run focused tests**

```bash
pytest tests/test_main.py -k "task_metadata or legacy_post" -v
```

Expected: PASS.

- [ ] **Step 7: Commit backend metadata support**

```bash
git add main.py tests/test_main.py
git commit -m "feat: add task-aware Bridge message metadata"
```

---

## Task 7 — Add task-aware filtering and UI rendering

**Files:**
- Modify: `main.py`
- Modify: `tests/test_main.py`
- Modify: `static/index.html`
- Modify: `README.md`

**Consumes:** Optional `task_id`/`message_type` from Task 6.

**Produces:** A single board that can filter messages by ForgeLoop task identity without changing protocol state.

- [ ] **Step 1: Write failing filter tests**

Add:

```python
async def test_filter_messages_by_task_id(client):
    await client.post(
        "/api/messages",
        headers=HEADERS_ENGINEER,
        json={"content": "A", "task_id": "task-a", "message_type": "TASK"},
    )
    await client.post(
        "/api/messages",
        headers=HEADERS_ENGINEER,
        json={"content": "B", "task_id": "task-b", "message_type": "TASK"},
    )

    r = await client.get(
        "/api/messages",
        params={"task_id": "task-a"},
        headers=HEADERS_WORKER,
    )
    assert r.status_code == 200
    assert [m["content"] for m in r.json()] == ["A"]
```

- [ ] **Step 2: Add optional `task_id` query parameter to GET `/api/messages`**

Append a parameterized SQL condition:

```sql
task_id = ?
```

Never interpolate the task ID into SQL text.

- [ ] **Step 3: Preserve `after_id`, `before_id`, and `limit` semantics under filtering**

Run tests combining task filters with pagination so filtering does not cause cursor skips or reordering.

- [ ] **Step 4: Add task/message metadata to message rendering**

In `static/index.html`, render escaped/sanitized metadata separately from Markdown content, for example:

```text
Engineer · task: auth-feature · STATUS · 23/08 15:20
```

Do not inject metadata via unsanitized HTML strings. Use DOM text assignment or explicit escaping for metadata values.

- [ ] **Step 5: Add a simple task filter control**

Requirements:

```text
- default = All tasks;
- filtering changes only Bridge display/API retrieval;
- it never changes the ForgeLoop selected task;
- clearing the filter returns to the current global board behavior;
- unscoped legacy messages remain visible in All tasks.
```

- [ ] **Step 6: Update composer with optional task ID and message type**

The current Markdown textarea remains primary. Add compact optional metadata controls; do not require users to supply a task ID for GENERAL messages.

- [ ] **Step 7: Test frontend/API backward compatibility**

At minimum:

```bash
pytest -v
ruff check .
```

Existing tests for auth, posting, pagination, delete, rate limiting, status, and frontend serving must continue to pass.

- [ ] **Step 8: Commit**

```bash
git add main.py tests/test_main.py static/index.html README.md
git commit -m "feat: add task-aware Bridge filtering"
```

---

## Task 8 — Update SSE and Worker polling for task context

**Files:**
- Modify: `examples/worker_poll.py`
- Modify: `README.md`
- Modify: `tests/test_main.py` if SSE test coverage is added.

**Produces:** Task-aware live messages while keeping existing SSE consumers valid.

- [ ] **Step 1: Preserve the existing SSE envelope**

SSE continues to emit serialized `MessageOut`. New fields are additive and nullable, so clients that only consume `id`, `role`, `content`, and `created_at` continue working.

- [ ] **Step 2: Update `worker_poll.py` to read integer message IDs consistently**

The existing persisted cursor should be represented as an integer because it stores message IDs, not timestamps.

Use:

```python
def load_last_seen() -> int:
    try:
        return int(STATE_FILE.read_text().strip())
    except (FileNotFoundError, ValueError):
        return 0


def save_last_seen(message_id: int):
    STATE_FILE.write_text(str(message_id))
```

- [ ] **Step 3: Print task context when present**

Example output:

```text
NEW INSTRUCTION FROM ENGINEER
Task: auth-feature
Type: TASK
```

If metadata is absent, preserve current behavior.

- [ ] **Step 4: Update example status posting**

Allow the Worker example to post the same `task_id` on acknowledgements/statuses and use `message_type: STATUS`.

- [ ] **Step 5: Do not make the polling script execute ForgeLoop itself by default**

Keep the example as a transport adapter. Its comments should direct the caller to invoke its Worker agent/harness, which then follows the canonical integration preference and ForgeLoop lifecycle.

- [ ] **Step 6: Run tests/lint**

```bash
pytest -v
ruff check .
```

- [ ] **Step 7: Commit**

```bash
git add examples/worker_poll.py README.md tests/test_main.py
git commit -m "feat: propagate task context through Bridge clients"
```

---

## Task 9 — Update the README architecture and operational workflow

**Files:**
- Modify: `README.md`
- Optional visual asset update only if the current banner/diagram implies an obsolete protocol architecture.

**Produces:** One architecture story consistent with the actual code and ForgeLoop 1.5.

- [ ] **Step 1: Rewrite the architecture section**

The architecture must explicitly show:

```text
ForgeLoopBridge = coordination channel
Engineer = intent/review/read-only protocol verification
Worker = implementation/protocol execution
Integration API/MCP/CLI = execution surfaces
ForgeLoop `.forgeloop/` = protocol authority
Git/PR = implementation/publication surface
```

- [ ] **Step 2: Clarify MCP positioning**

Document MCP as optional and local-first, not required for ForgeLoop applicability. Do not instruct Bridge users to expose ForgeLoop MCP HTTP remotely.

- [ ] **Step 3: Clarify Bridge networking vs ForgeLoop MCP networking**

ForgeLoopBridge may be deployed behind HTTPS/reverse proxy according to its own security model. This must not be confused with ForgeLoop MCP HTTP, whose documented support boundary is loopback-only.

- [ ] **Step 4: Rewrite the recommended workflow**

Recommended flow:

```text
Engineer posts intent with task context
→ Worker inspects ForgeLoop compatibility
→ Worker discovers existing tasks
→ Worker selects existing task or creates one only when appropriate
→ Worker asks canonical `next`
→ Worker follows contract/routing/preflight/lifecycle
→ verification and audit
→ complete VALID
→ canonical next until terminal/blocker
→ PR/publication action
→ Worker posts structured status
→ Engineer independently verifies through readonly canonical surfaces when available
→ review decision on Bridge
```

- [ ] **Step 5: Add recovery branch to workflow**

```text
next = RESUME_RECOVERED_TASK
→ inspect canonical ownership
→ task-resume
→ continue only if resume succeeds

next = RESOLVE_RECOVERY_INCONSISTENCY
→ stop normal mutation
→ follow canonical repair/validation guidance
→ report BLOCKED when safe repair cannot proceed
```

- [ ] **Step 6: Commit**

```bash
git add README.md
git commit -m "docs: refresh Bridge architecture for ForgeLoop 1.5"
```

---

## Task 10 — Add anti-drift tests/checks for documented ForgeLoop commands

**Files:**
- Modify: `tests/test_main.py` only for Bridge runtime behavior.
- Create: `tests/test_docs.py` for local documentation invariants.
- Modify: `.github/workflows/ci.yml` if the current CI command does not automatically collect the new test.

**Produces:** Lightweight repository checks preventing the most important old ForgeLoop workflow from returning accidentally.

- [ ] **Step 1: Add documentation invariant tests**

Example:

```python
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
README = (ROOT / "README.md").read_text(encoding="utf-8")
AUTONOMY = (ROOT / "examples" / "AUTONOMY.md").read_text(encoding="utf-8")


def test_readme_requires_protocol_handshake():
    assert "protocol-info --json" in README


def test_readme_mentions_structured_integration_preference():
    assert "structured integration" in README.lower()


def test_readme_requires_terminal_next_check():
    assert "nextAction" in README
    assert "terminal" in README


def test_autonomy_denies_self_issued_host_authority():
    text = AUTONOMY.lower()
    assert "host_attested" in text or "host-attested" in text
    assert "blocked" in text
```

Tests should enforce durable semantics, not exact paragraphs, so legitimate editorial improvements do not create unnecessary brittleness.

- [ ] **Step 2: Add a test preventing the strongest obsolete rule**

```python
def test_readme_does_not_forbid_all_engineer_forgeloop_reads():
    assert "You never execute code or run ForgeLoop yourself." not in README
```

- [ ] **Step 3: Run**

```bash
pytest -v
ruff check .
```

- [ ] **Step 4: Commit**

```bash
git add tests/test_docs.py .github/workflows/ci.yml
git commit -m "test: guard ForgeLoop 1.5 documentation invariants"
```

---

## Task 11 — Full runtime regression before documentation closure

**Files:** No intended source changes unless failures reveal a real regression.

- [ ] **Step 1: Run Python lint**

```bash
ruff check .
```

Expected: PASS.

- [ ] **Step 2: Run complete pytest suite**

```bash
pytest -v
```

Expected: all tests PASS.

- [ ] **Step 3: Validate existing database migration path**

Use a temporary SQLite database with the old `messages(id, role, content, created_at)` schema, start the updated application initialization, and verify:

```text
old messages remain present
new columns exist
old rows return task_id = null
old rows return message_type = null
new writes succeed
```

- [ ] **Step 4: Validate auth/security regressions**

Confirm:

```text
unauthenticated GET /api/messages = rejected
invalid token POST = rejected
roles still derive from token
cross-role delete = rejected
rate limiting still applies
Markdown is still sanitized by DOMPurify
SSE still requires authentication
/api/status still exposes no message content
```

- [ ] **Step 5: Validate multi-task behavior**

Confirm:

```text
unfiltered board returns all messages
filter task-a returns only task-a scoped messages
legacy unscoped messages remain accessible in All tasks
pagination works with filtering
SSE includes nullable metadata
posting without metadata remains valid
```

- [ ] **Step 6: Commit only if regression fixes were required**

Use a narrow fix commit describing the actual regression.

---

# FASE FINAL OBRIGATÓRIA — REVISÃO COMPLETA DA DOCUMENTAÇÃO

> Esta é a última fase do plano e deve ser executada somente depois que todas as alterações funcionais/documentais anteriores estiverem estabilizadas. Nenhum trabalho é considerado encerrado até esta revisão terminar sem drift conhecido.

## Objetivo da revisão

Revisar **toda a documentação e todo texto operacional do ForgeLoopBridge** contra o comportamento real do repositório e contra a arquitetura atual do ForgeLoop 1.5.0. A revisão deve remover instruções antigas, contradições, exemplos inválidos, termos que confundam autoridade de protocolo com coordenação do Bridge e referências que induzam agentes a ignorar `protocol-info`, `next`, ownership canônico, recovery seguro ou integração estruturada.

## Fontes canônicas que devem ser confrontadas

No repositório ForgeLoop, revisar pelo menos:

```text
README.md
AGENTS.md
LOOP_ENGINEERING.md
PROTOCOL_INTEGRATION.md
TERMINOLOGY.md
docs/GETTING_STARTED.md
docs/CLI_REFERENCE.md
docs/ARTIFACT_REFERENCE.md
docs/CROSS_HARNESS_CONTINUITY.md
docs/UNIVERSAL_INTEGRATION.md
docs/MCP.md
docs/RECIPES.md
docs/TROUBLESHOOTING.md
CHANGELOG.md
```

No ForgeLoopBridge, revisar **todos** os arquivos textuais/documentais relevantes, incluindo:

```text
README.md
examples/AUTONOMY.md
examples/worker_poll.py
improves.md
.env.example
Dockerfile
docker-compose.yml
main.py docstrings/comments
static/index.html visible copy/comments
requirements.txt
requirements-dev.txt
pyproject.toml
.github/workflows/*
tests/*.py
```

## Checklist de revisão completa

- [ ] **Compatibilidade e versões:** confirmar que toda menção a ForgeLoop diferencia corretamente core `1.5.x`, protocol `1` e Integration API `1`; remover qualquer inferência de compatibilidade baseada apenas em versão de pacote.

- [ ] **Integração estruturada:** confirmar que toda orientação operacional segue a regra “preferir integração estruturada oficial quando disponível; caso contrário usar CLI local”. Remover linguagem CLI-only quando ela for apresentada como requisito universal.

- [ ] **Autoridade única:** confirmar que nenhum documento sugere que ForgeLoopBridge, Engineer, Worker, Markdown do board, banco SQLite ou UI sejam fonte de lifecycle, ownership, recovery, completion ou authority do ForgeLoop.

- [ ] **Task discovery:** confirmar que todas as sequências de início/retomada mandam descobrir tarefas existentes antes de criar outra task.

- [ ] **`protocol-info`:** confirmar que os fluxos de bootstrap/retomada incluem o handshake público de compatibilidade ou equivalente estruturado.

- [ ] **`next`:** confirmar que o controle determinístico por `next` aparece antes de mutações em tarefas retomadas e novamente antes do status final; remover receitas que tratem uma lista fixa de comandos como autoridade superior ao estado canônico.

- [ ] **Continuity:** confirmar que `continuity.json` é descrito como contexto operacional opcional, não lifecycle truth e não verification evidence.

- [ ] **Recovery:** confirmar que recovery é descrito como suspensão/release/reacquisition de claims e nunca como completion.

- [ ] **Ownership:** confirmar que nenhum texto deriva claims efetivos de `task.json`, `work-state.json`, `recovery.json` ou fase `COMPLETE` isoladamente; sempre apontar para projeção canônica validada.

- [ ] **Recovered tasks:** confirmar documentação de `RESUME_RECOVERED_TASK` → `task-resume` e de `RESOLVE_RECOVERY_INCONSISTENCY` → fail closed/repair guidance.

- [ ] **Legacy recovery:** se mencionado, descrever migração/repair apenas por comandos canônicos; nunca instruir edição manual do ledger ou `recovery.json`.

- [ ] **Authority provenance:** confirmar que Engineer/Worker agreement, `APPROVED`, `acknowledgeRecovery`, variáveis de ambiente ou arquivos actor-controlled não são descritos como `HOST_ATTESTED`.

- [ ] **External execution:** confirmar que exemplos de verificação preservam exact-argv e provenance via `run-check`; não ensinar shell genérico como equivalente.

- [ ] **Completion:** substituir toda equivalência simples `complete VALID = Done` por `VALID + canonical next until terminal or explicit blocker`.

- [ ] **PR/publicação:** confirmar que PR, merge, release, deploy e publicação são dimensões externas; ForgeLoop completion não deve ser descrito como se executasse essas ações automaticamente.

- [ ] **Engineer role:** confirmar que o Engineer não implementa target code, mas pode realizar verificação canônica read-only quando seu host oferece capacidade adequada.

- [ ] **Worker role:** confirmar que o Worker não cria task automaticamente para cada mensagem e não recomeça lifecycle ao trocar de harness/sessão.

- [ ] **Autonomia:** confirmar que decisões reversíveis continuam podendo ser resolvidas entre agentes sem reintroduzir dependência humana desnecessária, mas operações que exigem autoridade externa permanecem `BLOCKED` quando a autoridade não existe.

- [ ] **Multi-task:** confirmar que `task_id` do Bridge é apenas índice de conversa e nunca substitui o task selector/identity canônico do ForgeLoop.

- [ ] **Message types:** confirmar que `TASK`, `STATUS`, `BLOCKED`, `REVIEW`, `DECISION_*` etc. são categorias de comunicação e não aliases de lifecycle phases ForgeLoop.

- [ ] **API:** revisar todos os exemplos de `GET /api/messages`, `POST /api/messages`, DELETE, `/api/whoami`, SSE e `/api/status` contra o comportamento real de `main.py`.

- [ ] **Banco de dados:** documentar a compatibilidade com mensagens antigas e os novos campos opcionais somente se a implementação P1 tiver sido concluída.

- [ ] **SSE/polling:** confirmar que a documentação corresponde ao mecanismo real de SSE com fallback; remover exemplos antigos baseados em timestamps se o runtime usa IDs.

- [ ] **Worker poller:** revisar tipos, cursor persistente, auth, task metadata e comentários para garantir que o exemplo não reintroduza semântica ForgeLoop obsoleta.

- [ ] **Segurança do Bridge:** confirmar tokens obrigatórios, diferença Engineer/Worker, autenticação de leitura, timing-safe comparison, rate limit, sanitização, SQLite WAL/busy timeout, TLS/reverse proxy recomendado e container non-root.

- [ ] **Segurança MCP:** confirmar que qualquer menção ao ForgeLoop MCP HTTP respeita o boundary loopback-only e não se confunde com o fato de ForgeLoopBridge poder operar atrás de HTTPS remoto.

- [ ] **Terminologia:** padronizar `ForgeLoopBridge`, `ForgeLoop`, `Engineer`, `Worker`, `task`, `task ID`, `claim`, `ownership`, `recovery`, `continuity`, `Integration API`, `MCP adapter`, `CLI fallback`, `terminal`, `nextAction`, `VALID`, `BLOCKED` e `PARTIALLY VERIFIED`.

- [ ] **Links:** verificar todos os links internos e externos do README e documentos; remover referências quebradas, arquivos renomeados e âncoras obsoletas.

- [ ] **Diagramas/imagens:** conferir se banner e diagramas representam a arquitetura atual. Se um visual mostrar Bridge como proprietário de `.forgeloop/` ou omitir a camada Integration/MCP/CLI de maneira enganosa, atualizar o visual e seu alt text.

- [ ] **`improves.md`:** transformar em histórico de auditoria, atualizar o status/escopo ou substituir por documento claramente histórico. Ele não pode parecer a lista atual de pendências se todos aqueles itens já foram resolvidos e novas prioridades são ForgeLoop 1.5.

- [ ] **Comentários/docstrings:** revisar comentários em `main.py`, `worker_poll.py` e JavaScript; documentação técnica em comentário também pode causar drift para futuros agentes.

- [ ] **Exemplos executáveis:** executar ou validar sintaticamente todos os comandos ForgeLoop mostrados no README contra a CLI/docs atuais. Não manter comandos apenas porque “parecem corretos”.

- [ ] **Sem placeholders:** procurar `TODO`, `TBD`, `FIXME`, “later”, “future”, texto provisório ou instruções incompletas introduzidas durante a atualização; resolver ou remover antes do fechamento.

- [ ] **Sem contradições:** pesquisar o repositório por frases antigas e comparar cada ocorrência com o novo contrato arquitetural.

- [ ] **Qualidade linguística:** revisar inglês do README/prompts/API copy e consistência de capitalização/nomenclatura; manter documentação pública principal em inglês se essa continuar sendo a política do projeto.

- [ ] **Testes de documentação:** executar os testes anti-drift adicionados neste plano junto com toda a suíte.

- [ ] **CI:** confirmar que o workflow do GitHub executa lint e testes novos; inspecionar o resultado real do CI, não apenas o YAML.

- [ ] **Revisão do diff completo:** antes do merge, revisar o diff inteiro procurando instrução duplicada, seção antiga não removida, exemplo divergente e texto que atribua autoridade incorreta ao Bridge.

## Gates finais obrigatórios

Executar:

```bash
ruff check .
pytest -v
```

Se existirem checks adicionais de documentação no repositório após a implementação, executá-los também.

A revisão só pode ser considerada concluída quando todos os gates abaixo forem verdadeiros:

```text
[PASS] runtime e testes existentes continuam válidos
[PASS] novos testes de task metadata/filtering passam, se P1 foi implementado
[PASS] documentação não contém fluxo ForgeLoop pré-1.5 conflitante
[PASS] Engineer prompt respeita readonly verification + authority boundary
[PASS] Worker prompt usa compatibility discovery + canonical next
[PASS] recovery/ownership são sempre canônicos e fail-closed
[PASS] complete VALID não é tratado isoladamente como finalização operacional
[PASS] Bridge permanece coordenação, nunca segunda autoridade ForgeLoop
[PASS] API/examples/UI correspondem ao código real
[PASS] links, diagramas, comandos e terminologia foram revisados
[PASS] CI final está verde
```

## Commit de fechamento documental

Depois de corrigir todos os achados da revisão:

```bash
git add README.md examples/AUTONOMY.md examples/worker_poll.py improves.md static/index.html main.py tests .github .env.example Dockerfile docker-compose.yml pyproject.toml requirements.txt requirements-dev.txt
git commit -m "docs: complete ForgeLoop 1.5 Bridge documentation review"
```

O commit deve incluir somente arquivos realmente alterados; remova da linha de `git add` qualquer caminho sem mudança antes de executar. A revisão documental final deve ser a última etapa antes do PR/merge da atualização ForgeLoop 1.5.
