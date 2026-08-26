# Post-PR #12 Final Reliability Corrections Implementation Plan

> For agentic workers: use the executing-plans workflow to implement this plan task-by-task. Steps use checkbox syntax for tracking.

Goal: Close the remaining SSE delivery and first-run Worker reliability gaps, add the requested small security hardening, and document actual ForgeLoopBridge behavior.

Architecture: Keep SQLite as the durable message journal and SSE as a bounded low-latency hint. A slow subscriber receives an explicit disconnect sentinel so the browser's existing error/polling path can reconcile from after_id. The Worker keeps its local cursor as a transport checkpoint and uses an explicit first-start policy, with pending delivering the latest Engineer message before advancing the cursor.

Tech stack: Python 3.12+, FastAPI, asyncio, aiosqlite, Pydantic, pytest, Ruff, vendored browser JavaScript, and GitHub Actions.

Spec: /Users/cassio/Downloads/FORGELOOPBRIDGE_POST_PR12_FINAL_CORRECTIONS.md

## Global constraints

- Preserve capability-first ForgeLoop compatibility and the boundary that ForgeLoop remains canonical.
- Keep task_id, message_type, action_id, approval_id, next_action, and reason_code as opaque reported references.
- Keep bounded SSE queues, REST after_id recovery, backward-compatible SQLite history, and post-handoff cursor advancement.
- Do not parse .forgeloop/ or add ForgeLoop lifecycle, action, approval, authority, recovery, or completion mutation endpoints.
- Use an independent SSE-ticket limiter; ticket issuance must not consume the message-post limiter.
- Keep the default SSE ticket lifetime at 30 seconds and reject configured lifetimes below one second.
- Make the default Worker startup policy pending; skipping existing board messages requires --start-mode now.
- Run a red-green TDD cycle for each behavior change, then run the complete validation matrix.

## File map

- main.py: SSE termination, independent ticket limits, and precise TTL validation/response.
- examples/worker_poll.py: first-run modes and atomic cursor persistence.
- tests/test_main.py: SSE termination, ticket-rate isolation, and TTL coverage.
- tests/test_worker_poll.py: first-start delivery, explicit now behavior, and cursor coverage.
- tests/test_docs.py: synchronization, deployment, authentication, and Worker documentation contracts.
- FORGELOOPBRIDGE_CURRENT_FORGELOOP_SYNC_UPDATE_PLAN.md: explicit stream closure and REST recovery wording.
- README.md: deployment topology, authentication preference, ticket limits, public status, cursor, and start modes.
- .env.example: independent ticket limiter settings.
- docs/superpowers/plans/2026-08-26-post-pr12-final-reliability-corrections.md: this implementation record.

---

### Task 1: Terminate an active SSE stream after overflow

Files:
- Modify main.py around the subscriber helpers and stream route.
- Modify tests/test_main.py next to the existing SSE overflow test.

Interfaces:
- SSE_DISCONNECT: unique in-process sentinel.
- disconnect_slow_subscriber(queue: asyncio.Queue) -> None.
- event_stream(request: Request, queue: asyncio.Queue): the route's async generator.

- [ ] Step 1: Write a failing behavior test.

Use a request double whose is_disconnected method always returns False. Fill a queue to capacity, start event_stream, call broadcast with one more MessageOut, assert the queue leaves _subscribers, drain the maxsize minus one buffered events, and assert the next anext(generator) raises StopAsyncIteration.

    class ConnectedRequest:
        async def is_disconnected(self):
            return False

    async def test_sse_overflow_terminates_active_stream():
        queue = main.create_sse_queue()
        main._subscribers.add(queue)
        for _ in range(queue.maxsize):
            queue.put_nowait(main.MessageOut(id=0, role="worker", content="buffered", created_at=0))

        generator = main.event_stream(ConnectedRequest(), queue)
        main.broadcast(main.MessageOut(id=1, role="engineer", content="overflow", created_at=1))

        assert queue not in main._subscribers
        for _ in range(queue.maxsize - 1):
            await asyncio.wait_for(anext(generator), timeout=1)
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(anext(generator), timeout=1)
        await generator.aclose()

- [ ] Step 2: Run the focused test and verify it fails for the missing/incorrect termination behavior.

    ./.venv/bin/python -m pytest -q tests/test_main.py::test_sse_overflow_terminates_active_stream

- [ ] Step 3: Implement the minimal sentinel flow.

Add SSE_DISCONNECT. Make disconnect_slow_subscriber discard the queue, remove one item with get_nowait when needed, and enqueue the sentinel with put_nowait. Use an explicit except asyncio.QueueFull branch followed by an except Exception branch, and route both through this helper. Move the nested route generator to event_stream, break when the dequeued item is SSE_DISCONNECT, preserve keepalives and request disconnect checks, and pass event_stream(request, queue) to StreamingResponse.

- [ ] Step 4: Run the focused test and the existing REST recovery test.

    ./.venv/bin/python -m pytest -q tests/test_main.py -k 'sse_overflow or sse_broadcast'

The new test and the existing persisted-message after_id test must both pass.

- [ ] Step 5: Commit the focused correction.

    git add main.py tests/test_main.py
    git commit -m "fix: terminate overflowed SSE subscribers"

---

### Task 2: Preserve the first existing Engineer instruction on Worker startup

Files:
- Modify examples/worker_poll.py usage text, fetch helpers, cursor bootstrap, and main.
- Modify tests/test_worker_poll.py next to the cursor/handoff tests.

Interfaces:
- START_MODES = ("pending", "now", "history").
- fetch_latest_messages(limit: int = 200) -> list[dict].
- fetch_latest_message_id() -> int, implemented through fetch_latest_messages(limit=1).
- initialize_first_start(start_mode: str, auto_ack: bool = False) -> int.

- [ ] Step 1: Write failing tests for pending and explicit now.

For pending, patch the latest page with worker messages and two Engineer messages, patch handoff_message, assert only the newest Engineer message is handed off and the cursor is saved only after it. For now, patch the same transport, assert handoff_message is not called, and assert the newest board ID is saved.

    def test_first_start_pending_hands_off_latest_engineer_instruction(tmp_path, monkeypatch):
        monkeypatch.setattr(worker_poll, "STATE_FILE", tmp_path / "last-seen")
        messages = [
            {"id": 40, "role": "worker", "content": "old receipt"},
            {"id": 41, "role": "engineer", "message_type": "TASK", "content": "old task"},
            {"id": 42, "role": "worker", "content": "old status"},
            {"id": 43, "role": "engineer", "message_type": "TASK", "content": "current task"},
        ]
        handed_off = []
        monkeypatch.setattr(worker_poll, "fetch_latest_messages", lambda limit=200: messages)
        monkeypatch.setattr(
            worker_poll,
            "handoff_message",
            lambda message, auto_ack=False: handed_off.append(message),
        )

        assert worker_poll.initialize_first_start("pending") == 43
        assert [message["id"] for message in handed_off] == [43]
        assert worker_poll.load_last_seen() == 43

    def test_first_start_now_skips_existing_messages_explicitly(tmp_path, monkeypatch):
        monkeypatch.setattr(worker_poll, "STATE_FILE", tmp_path / "last-seen")
        monkeypatch.setattr(
            worker_poll,
            "fetch_latest_messages",
            lambda limit=200: [
                {"id": 42, "role": "engineer", "message_type": "TASK", "content": "existing"}
            ],
        )
        handed_off = []
        monkeypatch.setattr(
            worker_poll,
            "handoff_message",
            lambda message, auto_ack=False: handed_off.append(message),
        )

        assert worker_poll.initialize_first_start("now") == 42
        assert handed_off == []
        assert worker_poll.load_last_seen() == 42

- [ ] Step 2: Run the focused tests and verify the expected red failure.

    ./.venv/bin/python -m pytest -q tests/test_worker_poll.py -k 'first_start'

- [ ] Step 3: Implement latest-page bootstrap and explicit modes.

Implement fetch_latest_messages with GET /api/messages and params latest=true, limit=200. Keep fetch_latest_message_id as a limit-one wrapper. Implement initialize_first_start so history returns zero without skipping via a pre-saved cursor, now saves the newest ID without handoff, pending hands off the newest Engineer-authored message through process_polled_messages, and an empty/no-Engineer page saves the newest page ID. Add argparse choices pending, now, and history, default pending. Use STATE_FILE.exists() in main to distinguish a missing cursor from a saved numeric zero. Keep normal polling and post-handoff cursor semantics unchanged.

- [ ] Step 4: Add the atomic cursor write and its test.

Write the cursor to STATE_FILE.with_name(f"{STATE_FILE.name}.tmp") with UTF-8 encoding, then replace STATE_FILE. Extend the cursor test to assert the value is readable and the sibling temporary file does not remain.

- [ ] Step 5: Run all Worker tests.

    ./.venv/bin/python -m pytest -q tests/test_worker_poll.py

- [ ] Step 6: Commit the Worker correction.

    git add examples/worker_poll.py tests/test_worker_poll.py
    git commit -m "fix: preserve first-run Worker instructions"

---

### Task 3: Bound SSE ticket issuance and make TTL reporting exact

Files:
- Modify main.py configuration, rate limiting, ticket issuance, and stream-ticket endpoint.
- Modify tests/test_main.py ticket tests and fixture cleanup.
- Modify .env.example.

Interfaces:
- SSE_TICKET_RATE_LIMIT and SSE_TICKET_RATE_WINDOW configuration values.
- Independent _sse_ticket_timestamps store and check_sse_ticket_rate_limit(role: str) -> None.
- issue_sse_ticket(role: str) -> tuple[str, float].

- [ ] Step 1: Write failing tests.

Set the ticket limit and post limit to one, issue two tickets, and post one message; assert statuses 200, 429, and 200 to prove independent budgets. Add a TTL parser test with 0.5 and an inclusive minimum of one second. Change the existing response assertion to compare expires_in with the configured float exactly.

- [ ] Step 2: Run the focused tests and verify the expected red failures.

    ./.venv/bin/python -m pytest -q tests/test_main.py -k 'ticket or ttl'

- [ ] Step 3: Implement the hardening.

Extend _env_float with minimum_inclusive=False, retaining strict behavior for existing callers and using an inclusive minimum for SSE_TICKET_TTL. Add SSE_TICKET_RATE_LIMIT=30 and SSE_TICKET_RATE_WINDOW=60, an independent deque/lock pair, and a limiter that raises HTTP 429. Clear that store in the test fixture and lifespan. Authenticate, rate-limit, then issue in /api/stream-ticket. Return the exact float TTL instead of int truncation.

- [ ] Step 4: Add the two variables to .env.example.

    SSE_TICKET_RATE_LIMIT=30
    SSE_TICKET_RATE_WINDOW=60

- [ ] Step 5: Run ticket-focused tests and the full suite.

    ./.venv/bin/python -m pytest -q tests/test_main.py -k 'ticket or ttl'
    ./.venv/bin/python -m pytest -q

- [ ] Step 6: Commit the ticket hardening.

    git add main.py tests/test_main.py .env.example
    git commit -m "fix: bound SSE ticket issuance"

---

### Task 4: Align synchronization and user-facing documentation

Files:
- Modify FORGELOOPBRIDGE_CURRENT_FORGELOOP_SYNC_UPDATE_PLAN.md.
- Modify README.md.
- Modify tests/test_docs.py.

- [ ] Step 1: Write failing documentation assertions.

Require the current record to contain explicit stream closure and a fresh SSE ticket. Require README to mention one application worker, shared broadcast backend, Authorization: Bearer, legacy query-token compatibility, .worker_last_seen, --start-mode now, and sse_ticket_rate_limit.

- [ ] Step 2: Run the focused doc tests and verify the expected red failure.

    ./.venv/bin/python -m pytest -q tests/test_docs.py -k 'stream_close or topology or start_policy'

- [ ] Step 3: Update the synchronization record.

State that every subscriber has bounded buffering, queue overflow explicitly closes the affected stream, and the browser falls back to REST after_id reconciliation and requests a fresh SSE ticket.

- [ ] Step 4: Update README.

Add the independent ticket limiter variables and TTL floor to the environment table; document one application worker for deterministic realtime SSE unless a shared broadcast backend exists; prefer Bearer headers and mark query-token authentication as legacy/deprecated; document intentional /api/status activity metadata; explain that .worker_last_seen is a local transport checkpoint, not canonical ForgeLoop state; and describe pending (default), now (explicit skip), and history (cursor-zero redelivery).

- [ ] Step 5: Run documentation tests.

    ./.venv/bin/python -m pytest -q tests/test_docs.py

- [ ] Step 6: Commit documentation.

    git add README.md FORGELOOPBRIDGE_CURRENT_FORGELOOP_SYNC_UPDATE_PLAN.md tests/test_docs.py
    git commit -m "docs: describe reliable Bridge recovery semantics"

---

### Task 5: Validate, review, publish, and merge

- [ ] Step 1: Run the complete local matrix from the isolated worktree.

    ./.venv/bin/python -m pytest -q
    ./.venv/bin/ruff check .
    ./.venv/bin/python -m compileall -q main.py examples tests
    node --check /tmp/forgeloopbridge-index-script.js
    git diff --check

Verify the diff preserves the ForgeLoop authority boundary and contains no .forgeloop parsing or mutation endpoint.

- [ ] Step 2: Review the requirements line by line.

Confirm the P0 SSE and Worker acceptance criteria, independent ticket limiter, TTL floor/exact response, single-process documentation, Bearer preference, public status disclosure, atomic cursor persistence, and all start-mode documentation.

- [ ] Step 3: Request code review before publishing.

Use the review workflow with the fork-point base SHA and branch-tip head SHA. Fix every Critical or Important finding, rerun the full matrix, and commit fixes.

- [ ] Step 4: Push and open a PR into main.

Use branch codex/post-pr12-final-corrections. Include the P0/P1/P2 summary and fresh validation evidence. Wait for all required GitHub Actions checks to finish; do not merge a pending or failing PR.

- [ ] Step 5: Merge with the exact verified head and synchronize locally.

Capture the fresh full PR headRefOid, merge with --match-head-commit, verify merged state, merge commit, and post-merge workflow terminal state. Fast-forward local main to origin/main and verify HEAD equals origin/main with clean status.
