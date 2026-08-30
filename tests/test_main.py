import asyncio
import json
import os
import tempfile
from pathlib import Path

import httpx
import pytest

os.environ.setdefault("ENGINEER_TOKEN", "test_engineer_token_1234567890")
os.environ.setdefault("WORKER_TOKEN", "test_worker_token_0987654321")

_tmpdir = tempfile.mkdtemp()
os.environ["FORGEBRIDGE_DB"] = str(Path(_tmpdir) / "test.db")

import main  # noqa: E402  (import after env vars are set)
from main import ENGINEER_TOKEN, WORKER_TOKEN, app  # noqa: E402

HEADERS_ENGINEER = {"Authorization": f"Bearer {ENGINEER_TOKEN}"}
HEADERS_WORKER = {"Authorization": f"Bearer {WORKER_TOKEN}"}


@pytest.fixture(autouse=True)
async def clean_db():
    await main.init_db()
    async with main.connect_db() as db:
        await db.execute("DELETE FROM messages")
    main._post_timestamps.clear()
    main._sse_ticket_timestamps.clear()
    main._subscribers.clear()
    main._sse_tickets.clear()
    yield


@pytest.fixture
async def client():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def post(client, content, headers):
    return await client.post("/api/messages", json={"token": "", "content": content}, headers=headers)


async def seed(client, n=3):
    for i in range(n):
        r = await post(client, f"msg {i}", HEADERS_ENGINEER)
        assert r.status_code == 200
    return [await get_ids(client)]


async def get_ids(client):
    r = await client.get("/api/messages", headers=HEADERS_ENGINEER)
    return [m["id"] for m in r.json()]


# ─── Auth ─────────────────────────────────────────────────────────────────────


async def test_post_requires_valid_token(client):
    r = await client.post("/api/messages", json={"token": "wrong", "content": "hi"})
    assert r.status_code == 401


async def test_read_requires_token(client):
    r = await client.get("/api/messages")
    assert r.status_code == 401


async def test_read_with_query_token(client):
    r = await client.get("/api/messages", params={"token": WORKER_TOKEN})
    assert r.status_code == 200


async def test_whoami(client):
    r = await client.get("/api/whoami", headers=HEADERS_ENGINEER)
    assert r.json()["role"] == "engineer"
    r = await client.get("/api/whoami", headers=HEADERS_WORKER)
    assert r.json()["role"] == "worker"
    r = await client.get("/api/whoami")
    assert r.status_code == 401


# ─── Post / Get ───────────────────────────────────────────────────────────────


async def test_post_and_get_roundtrip(client):
    r = await post(client, "# Task\n- do x", HEADERS_ENGINEER)
    assert r.status_code == 200
    body = r.json()
    assert body["role"] == "engineer"

    r = await client.get("/api/messages", headers=HEADERS_WORKER)
    msgs = r.json()
    assert len(msgs) == 1
    assert msgs[0]["content"] == "# Task\n- do x"


async def test_worker_role_assigned_from_token(client):
    r = await client.post(
        "/api/messages",
        json={"token": os.environ["WORKER_TOKEN"], "content": "status"},
    )
    assert r.json()["role"] == "worker"


async def test_empty_content_rejected(client):
    r = await client.post("/api/messages", json={"token": "", "content": "   "}, headers=HEADERS_ENGINEER)
    assert r.status_code == 400


async def test_pagination_after_id(client):
    await seed(client, 3)
    ids = await get_ids(client)
    r = await client.get(
        "/api/messages", params={"after_id": ids[0]}, headers=HEADERS_ENGINEER
    )
    returned = [m["id"] for m in r.json()]
    assert returned == ids[1:]


async def test_pagination_before_id_returns_last_page_asc(client):
    await seed(client, 5)
    ids = await get_ids(client)
    r = await client.get(
        "/api/messages",
        params={"before_id": ids[-1], "limit": 2},
        headers=HEADERS_ENGINEER,
    )
    returned = [m["id"] for m in r.json()]
    assert returned == ids[2:4]  # the two before the last, in ASC order


async def test_limit_cap(client):
    await seed(client, 5)
    r = await client.get(
        "/api/messages", params={"limit": 3}, headers=HEADERS_ENGINEER
    )
    assert len(r.json()) == 3


# ─── Delete ───────────────────────────────────────────────────────────────────


async def test_delete_by_author(client):
    r = await post(client, "to delete", HEADERS_ENGINEER)
    mid = r.json()["id"]
    r = await client.delete(f"/api/messages/{mid}", headers=HEADERS_ENGINEER)
    assert r.status_code == 200
    assert await get_ids(client) == []


async def test_delete_forbidden_for_other_role(client):
    r = await post(client, "engineer msg", HEADERS_ENGINEER)
    mid = r.json()["id"]
    r = await client.delete(f"/api/messages/{mid}", headers=HEADERS_WORKER)
    assert r.status_code == 403


async def test_delete_missing_message(client):
    r = await client.delete("/api/messages/9999", headers=HEADERS_ENGINEER)
    assert r.status_code == 404


# ─── Status ───────────────────────────────────────────────────────────────────


async def test_status_public_no_auth_needed(client):
    await post(client, "x", HEADERS_ENGINEER)
    r = await client.get("/api/status")  # no token
    assert r.status_code == 200
    data = r.json()
    assert data["total_messages"] >= 1
    assert "content" not in str(data)


async def test_healthz_is_minimal_and_public(client):
    r = await client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


async def test_stream_ticket_requires_auth_and_is_role_bound(client):
    unauthorized = await client.post("/api/stream-ticket")
    assert unauthorized.status_code == 401

    r = await client.post("/api/stream-ticket", headers=HEADERS_WORKER)
    assert r.status_code == 200
    data = r.json()
    assert data["ticket"]
    assert data["expires_in"] == main.SSE_TICKET_TTL
    assert WORKER_TOKEN not in data["ticket"]
    assert await main.resolve_sse_ticket(data["ticket"]) == "worker"
    with pytest.raises(main.HTTPException) as exc_info:
        await main.resolve_sse_ticket(data["ticket"])
    assert exc_info.value.status_code == 401


async def test_stream_ticket_rate_limit_is_independent_from_post_limit(client, monkeypatch):
    monkeypatch.setattr(main, "SSE_TICKET_RATE_LIMIT", 1, raising=False)
    monkeypatch.setattr(main, "RATE_LIMIT_POSTS", 1)

    first_ticket = await client.post("/api/stream-ticket", headers=HEADERS_WORKER)
    second_ticket = await client.post("/api/stream-ticket", headers=HEADERS_WORKER)
    first_post = await post(client, "post budget is separate", HEADERS_WORKER)

    assert first_ticket.status_code == 200
    assert second_ticket.status_code == 429
    assert first_post.status_code == 200


async def test_stream_ticket_reports_fractional_ttl_exactly(client, monkeypatch):
    monkeypatch.setattr(main, "SSE_TICKET_TTL", 1.5)

    response = await client.post("/api/stream-ticket", headers=HEADERS_WORKER)

    assert response.status_code == 200
    assert response.json()["expires_in"] == 1.5


def test_sse_ticket_ttl_requires_at_least_one_second(monkeypatch):
    monkeypatch.setenv("SSE_TICKET_TTL", "0.5")
    with pytest.raises(RuntimeError, match="SSE_TICKET_TTL"):
        main._env_float("SSE_TICKET_TTL", 30, 1, 300, minimum_inclusive=True)


# ─── Rate limit ───────────────────────────────────────────────────────────────


async def test_rate_limit(client, monkeypatch):
    monkeypatch.setattr(main, "RATE_LIMIT_POSTS", 3)
    codes = []
    for _ in range(5):
        r = await post(client, "spam", HEADERS_WORKER)
        codes.append(r.status_code)
    assert codes[:3] == [200, 200, 200]
    assert codes[3:] == [429, 429]


# ─── Frontend ─────────────────────────────────────────────────────────────────


async def test_index_served(client):
    r = await client.get("/")
    assert r.status_code == 200
    assert "purify.min.js" in r.text


# ─── Task Metadata & Filtering (ForgeLoop 1.5) ───────────────────────────────


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

    r_get = await client.get("/api/messages", headers=HEADERS_WORKER)
    assert r_get.status_code == 200
    msgs = r_get.json()
    assert len(msgs) == 1
    assert msgs[0]["task_id"] == "auth-feature"
    assert msgs[0]["message_type"] == "TASK"


async def test_post_roundtrip_with_action_approval_metadata(client):
    r = await client.post(
        "/api/messages",
        headers=HEADERS_WORKER,
        json={
            "content": "Canonical approval is required.",
            "task_id": "  release-task  ",
            "message_type": "approval_required",
            "action_id": "  action-publish  ",
            "approval_id": "  approval-publish  ",
            "next_action": "  REQUEST_ACTION_APPROVAL  ",
            "reason_code": "  E_ACTION_APPROVAL_REQUIRED  ",
        },
    )
    assert r.status_code == 200
    assert r.json()["task_id"] == "release-task"
    assert r.json()["message_type"] == "APPROVAL_REQUIRED"
    assert r.json()["action_id"] == "action-publish"
    assert r.json()["approval_id"] == "approval-publish"
    assert r.json()["next_action"] == "REQUEST_ACTION_APPROVAL"
    assert r.json()["reason_code"] == "E_ACTION_APPROVAL_REQUIRED"

    r_get = await client.get(
        "/api/messages",
        params={"action_id": "action-publish"},
        headers=HEADERS_ENGINEER,
    )
    assert r_get.status_code == 200
    assert r_get.json()[0]["approval_id"] == "approval-publish"


async def test_new_message_types_are_accepted(client):
    for message_type in (
        "ACTION_REQUIRED",
        "APPROVAL_REQUIRED",
        "AUTHORITY_REQUIRED",
        "ACTION_RECONCILIATION_REQUIRED",
        "ACTION_RECONCILED",
        "DIAGNOSTIC",
        "POLICY_BLOCKED",
    ):
        r = await client.post(
            "/api/messages",
            headers=HEADERS_ENGINEER,
            json={"content": message_type, "message_type": message_type},
        )
        assert r.status_code == 200
        assert r.json()["message_type"] == message_type


async def test_reference_metadata_rejects_overlong_or_control_values(client):
    for field, size in (
        ("action_id", 201),
        ("approval_id", 201),
        ("next_action", 101),
        ("reason_code", 161),
    ):
        r = await client.post(
            "/api/messages",
            headers=HEADERS_ENGINEER,
            json={"content": "metadata", field: "x" * size},
        )
        assert r.status_code == 422

    r_control = await client.post(
        "/api/messages",
        headers=HEADERS_ENGINEER,
        json={"content": "metadata", "action_id": "action-1\nforged"},
    )
    assert r_control.status_code == 422


async def test_reference_filters_combine_with_task_and_pagination(client):
    for i in range(3):
        await client.post(
            "/api/messages",
            headers=HEADERS_ENGINEER,
            json={
                "content": f"approval-{i}",
                "task_id": "release",
                "message_type": "APPROVAL_REQUIRED",
                "action_id": "action-release",
                "approval_id": f"approval-{i}",
            },
        )
    await client.post(
        "/api/messages",
        headers=HEADERS_ENGINEER,
        json={
            "content": "different task",
            "task_id": "other",
            "message_type": "APPROVAL_REQUIRED",
            "action_id": "action-other",
            "approval_id": "approval-other",
        },
    )

    r = await client.get(
        "/api/messages",
        params={
            "task_id": "release",
            "message_type": "approval_required",
            "action_id": "action-release",
            "limit": 2,
        },
        headers=HEADERS_WORKER,
    )
    assert r.status_code == 200
    assert [message["content"] for message in r.json()] == ["approval-0", "approval-1"]

    r_latest = await client.get(
        "/api/messages",
        params={
            "task_id": "release",
            "approval_id": "approval-2",
            "latest": "true",
        },
        headers=HEADERS_WORKER,
    )
    assert [message["content"] for message in r_latest.json()] == ["approval-2"]


async def test_sse_broadcast_serializes_reference_metadata(client):
    queue = main.create_sse_queue()
    assert queue.maxsize == main.SSE_QUEUE_SIZE
    main._subscribers.add(queue)
    try:
        r = await client.post(
            "/api/messages",
            headers=HEADERS_ENGINEER,
            json={
                "content": "sse metadata",
                "message_type": "ACTION_RECONCILIATION_REQUIRED",
                "action_id": "action-sse",
                "approval_id": "approval-sse",
                "next_action": "RECONCILE_ACTION",
                "reason_code": "COMMIT_UNKNOWN",
            },
        )
        assert r.status_code == 200
        event = await asyncio.wait_for(queue.get(), timeout=1)
        payload = json.loads(event.model_dump_json())
        assert payload["action_id"] == "action-sse"
        assert payload["approval_id"] == "approval-sse"
        assert payload["next_action"] == "RECONCILE_ACTION"
        assert payload["reason_code"] == "COMMIT_UNKNOWN"
    finally:
        main._subscribers.discard(queue)


async def test_sse_overflow_drops_slow_subscriber_and_rest_reconciles(client):
    queue = main.create_sse_queue()
    main._subscribers.add(queue)
    for _ in range(queue.maxsize):
        queue.put_nowait(main.MessageOut(id=0, role="worker", content="buffered", created_at=0))

    r_post = await post(client, "recover me", HEADERS_ENGINEER)
    assert r_post.status_code == 200
    assert queue not in main._subscribers

    recovered = await client.get(
        "/api/messages",
        params={"after_id": 0},
        headers=HEADERS_WORKER,
    )
    assert recovered.status_code == 200
    assert recovered.json()[-1]["content"] == "recover me"


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
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(anext(generator), timeout=1)
    await generator.aclose()


@pytest.mark.parametrize(
    ("name", "raw", "default", "minimum", "maximum"),
    (
        ("PORT", "0", 8000, 1, 65535),
        ("PORT", "65536", 8000, 1, 65535),
        ("RATE_LIMIT_POSTS", "0", 30, 1, None),
        ("DEFAULT_PAGE_SIZE", "1001", 200, 1, main.MAX_PAGE_SIZE),
        ("SSE_QUEUE_SIZE", "15", 256, 16, 10000),
    ),
)
def test_integer_environment_bounds(monkeypatch, name, raw, default, minimum, maximum):
    monkeypatch.setenv(name, raw)
    with pytest.raises(RuntimeError, match=name):
        main._env_int(name, default, minimum, maximum)


@pytest.mark.parametrize("raw", ("0", "nan", "inf", "not-a-number"))
def test_float_environment_must_be_positive_and_finite(monkeypatch, raw):
    monkeypatch.setenv("RATE_LIMIT_WINDOW", raw)
    with pytest.raises(RuntimeError, match="RATE_LIMIT_WINDOW"):
        main._env_float("RATE_LIMIT_WINDOW", 60, 0)


async def test_legacy_post_without_task_metadata_still_works(client):
    r = await post(client, "legacy message", HEADERS_ENGINEER)
    assert r.status_code == 200
    body = r.json()
    assert body["task_id"] is None
    assert body["message_type"] is None


async def test_message_type_normalized_and_validated(client):
    # Valid lower-case should normalize to upper
    r = await client.post(
        "/api/messages",
        headers=HEADERS_WORKER,
        json={"content": "Done", "task_id": "t1", "message_type": "status"},
    )
    assert r.status_code == 200
    assert r.json()["message_type"] == "STATUS"

    # Invalid message_type should be rejected
    r_bad = await client.post(
        "/api/messages",
        headers=HEADERS_WORKER,
        json={"content": "Done", "message_type": "UNKNOWN_CUSTOM_TYPE"},
    )
    assert r_bad.status_code == 422


async def test_filter_messages_by_task_id(client):
    await client.post(
        "/api/messages",
        headers=HEADERS_ENGINEER,
        json={"content": "Task A msg 1", "task_id": "task-a", "message_type": "TASK"},
    )
    await client.post(
        "/api/messages",
        headers=HEADERS_ENGINEER,
        json={"content": "Task B msg 1", "task_id": "task-b", "message_type": "TASK"},
    )
    await client.post(
        "/api/messages",
        headers=HEADERS_WORKER,
        json={"content": "Task A status", "task_id": "task-a", "message_type": "STATUS"},
    )
    await post(client, "Unscoped global message", HEADERS_ENGINEER)

    # Filter task-a
    r_a = await client.get("/api/messages", params={"task_id": "task-a"}, headers=HEADERS_WORKER)
    assert r_a.status_code == 200
    assert [m["content"] for m in r_a.json()] == ["Task A msg 1", "Task A status"]

    # Filter task-b
    r_b = await client.get("/api/messages", params={"task_id": "task-b"}, headers=HEADERS_WORKER)
    assert r_b.status_code == 200
    assert [m["content"] for m in r_b.json()] == ["Task B msg 1"]

    # Unfiltered gets all
    r_all = await client.get("/api/messages", headers=HEADERS_WORKER)
    assert len(r_all.json()) == 4


async def test_filter_combined_with_pagination(client):
    for i in range(5):
        await client.post(
            "/api/messages",
            headers=HEADERS_ENGINEER,
            json={"content": f"A-{i}", "task_id": "task-a", "message_type": "TASK"},
        )
        await client.post(
            "/api/messages",
            headers=HEADERS_ENGINEER,
            json={"content": f"B-{i}", "task_id": "task-b", "message_type": "TASK"},
        )

    r_paged = await client.get(
        "/api/messages",
        params={"task_id": "task-a", "limit": 2},
        headers=HEADERS_WORKER,
    )
    assert r_paged.status_code == 200
    msgs = r_paged.json()
    assert len(msgs) == 2
    assert [m["content"] for m in msgs] == ["A-0", "A-1"]

    after_id = msgs[-1]["id"]
    r_after = await client.get(
        "/api/messages",
        params={"task_id": "task-a", "after_id": after_id, "limit": 2},
        headers=HEADERS_WORKER,
    )
    assert [m["content"] for m in r_after.json()] == ["A-2", "A-3"]


async def test_database_migration_from_legacy_schema(tmp_path, monkeypatch):
    import aiosqlite

    legacy_db_file = tmp_path / "legacy.db"
    # Create legacy table without task_id or message_type
    async with aiosqlite.connect(legacy_db_file) as db:
        await db.execute("""
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at REAL NOT NULL
            )
        """)
        await db.execute(
            "INSERT INTO messages (role, content, created_at) VALUES ('engineer', 'legacy msg', 123456.0)"
        )
        await db.commit()

    # Point app DB_PATH to legacy_db_file and run init_db
    monkeypatch.setattr(main, "DB_PATH", legacy_db_file)
    await main.init_db()

    # Connect with test client to verify reads and writes
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get("/api/messages", headers=HEADERS_WORKER)
        assert r.status_code == 200
        msgs = r.json()
        assert len(msgs) == 1
        assert msgs[0]["content"] == "legacy msg"
        assert msgs[0]["task_id"] is None
        assert msgs[0]["message_type"] is None
        assert msgs[0]["action_id"] is None
        assert msgs[0]["approval_id"] is None
        assert msgs[0]["next_action"] is None
        assert msgs[0]["reason_code"] is None

        # Post new message with metadata to migrated db
        r_post = await c.post(
            "/api/messages",
            headers=HEADERS_ENGINEER,
            json={
                "content": "new msg",
                "task_id": "task-x",
                "message_type": "ACTION_REQUIRED",
                "action_id": "action-x",
                "approval_id": "approval-x",
                "next_action": "AUTHORIZE_ACTION",
                "reason_code": "E_ACTION_AUTHORITY_REQUIRED",
            },
        )
        assert r_post.status_code == 200
        assert r_post.json()["task_id"] == "task-x"
        assert r_post.json()["message_type"] == "ACTION_REQUIRED"
        assert r_post.json()["action_id"] == "action-x"
        assert r_post.json()["approval_id"] == "approval-x"
        assert r_post.json()["next_action"] == "AUTHORIZE_ACTION"
        assert r_post.json()["reason_code"] == "E_ACTION_AUTHORITY_REQUIRED"


# ─── Latest Page Pagination ───────────────────────────────────────────────────


async def test_latest_returns_newest_messages_in_ascending_order(client):
    for i in range(5):
        r = await client.post(
            "/api/messages",
            headers=HEADERS_ENGINEER,
            json={"content": f"message-{i}"},
        )
        assert r.status_code == 200

    r = await client.get(
        "/api/messages",
        params={"latest": "true", "limit": 2},
        headers=HEADERS_WORKER,
    )

    assert r.status_code == 200
    assert [m["content"] for m in r.json()] == ["message-3", "message-4"]


async def test_latest_rejects_cursor_combination(client):
    r_after = await client.get(
        "/api/messages",
        params={"latest": "true", "after_id": 1},
        headers=HEADERS_WORKER,
    )
    assert r_after.status_code == 400
    assert "latest" in r_after.json()["detail"].lower()

    r_before = await client.get(
        "/api/messages",
        params={"latest": "true", "before_id": 10},
        headers=HEADERS_WORKER,
    )
    assert r_before.status_code == 400
    assert "latest" in r_before.json()["detail"].lower()


async def test_latest_combines_with_task_id(client):
    for i in range(4):
        await client.post(
            "/api/messages",
            headers=HEADERS_ENGINEER,
            json={"content": f"auth-{i}", "task_id": "auth"},
        )
        await client.post(
            "/api/messages",
            headers=HEADERS_ENGINEER,
            json={"content": f"billing-{i}", "task_id": "billing"},
        )

    r = await client.get(
        "/api/messages",
        params={"task_id": "auth", "latest": "true", "limit": 2},
        headers=HEADERS_WORKER,
    )
    assert r.status_code == 200
    assert [m["content"] for m in r.json()] == ["auth-2", "auth-3"]


async def test_blank_task_id_normalizes_to_none(client):
    r = await client.post(
        "/api/messages",
        headers=HEADERS_ENGINEER,
        json={"content": "hello", "task_id": "   "},
    )
    assert r.status_code == 200
    assert r.json()["task_id"] is None


async def test_task_id_is_trimmed(client):
    r = await client.post(
        "/api/messages",
        headers=HEADERS_ENGINEER,
        json={"content": "hello", "task_id": "  auth-feature  "},
    )
    assert r.status_code == 200
    assert r.json()["task_id"] == "auth-feature"


def typed_status(
    message_key: str = "worker-status-1",
    *,
    correlation_id: str | None = None,
    reply_to_id: int | None = None,
    state: str = "IN_PROGRESS",
) -> dict:
    return {
        "schema_version": 1,
        "kind": "STATUS_UPDATE",
        "message_key": message_key,
        "correlation_id": correlation_id,
        "reply_to_id": reply_to_id,
        "expects_reply": False,
        "payload": {"kind": "STATUS_UPDATE", "state": state, "summary": "Verification is running."},
        "canonical_refs": [{"kind": "TASK", "ref": "task-typed"}],
    }


async def test_typed_message_roundtrips_through_rest_and_sse(client):
    queue = main.create_sse_queue()
    main._subscribers.add(queue)
    try:
        response = await client.post(
            "/api/messages",
            headers=HEADERS_ENGINEER,
            json={
                "content": "Typed status",
                "message_type": "STATUS",
                "task_id": "task-typed",
                "typed": typed_status(correlation_id="verification-cycle-3"),
            },
        )
        assert response.status_code == 200
        created = response.json()
        assert created["typed"]["kind"] == "STATUS_UPDATE"
        assert created["typed"]["payload"]["summary"] == "Verification is running."
        assert created["typed_integrity"] == "VALID"
        assert created["typed_error"] is None

        event = await asyncio.wait_for(queue.get(), timeout=1)
        event_body = event.model_dump(mode="json")
        assert event_body["typed"] == created["typed"]
        assert event_body["typed_integrity"] == "VALID"
        assert event_body["typed_error"] is None

        restored = await client.get(
            "/api/messages",
            params={"typed_kind": "status_update", "correlation_id": "verification-cycle-3"},
            headers=HEADERS_WORKER,
        )
        assert restored.status_code == 200
        assert restored.json() == [created]
    finally:
        main._subscribers.discard(queue)


async def test_malformed_persisted_typed_data_fails_closed_but_keeps_markdown(client):
    created = await client.post(
        "/api/messages",
        headers=HEADERS_WORKER,
        json={"content": "Keep this visible", "typed": typed_status("worker-malformed-1")},
    )
    assert created.status_code == 200

    async with main.connect_db() as db:
        await db.execute(
            "UPDATE messages SET expects_reply = NULL WHERE id = ?",
            (created.json()["id"],),
        )
        await db.commit()

    restored = await client.get("/api/messages", headers=HEADERS_ENGINEER)

    assert restored.status_code == 200
    restored_message = restored.json()[0]
    assert restored_message["content"] == "Keep this visible"
    assert restored_message["typed"] is None
    assert restored_message["typed_integrity"] == "INVALID"
    assert restored_message["typed_error"]["code"] == "E_BRIDGE_PERSISTED_TYPED_INVALID"


async def test_corrupted_typed_row_cannot_satisfy_idempotent_retry(client):
    body = {
        "content": "A stable typed message",
        "message_type": "STATUS",
        "typed": typed_status(message_key="worker-corrupt-idempotency"),
    }
    created = await client.post("/api/messages", headers=HEADERS_WORKER, json=body)
    assert created.status_code == 200

    async with main.connect_db() as db:
        await db.execute(
            "UPDATE messages SET typed_payload_json = ? WHERE id = ?",
            ("{malformed", created.json()["id"]),
        )
        await db.commit()

    retry = await client.post("/api/messages", headers=HEADERS_WORKER, json=body)
    assert retry.status_code == 409
    assert retry.json()["error"]["code"] == "E_BRIDGE_IDEMPOTENCY_CONFLICT"


async def test_corrupted_persisted_canonical_refs_are_marked_invalid(client):
    created = await client.post(
        "/api/messages",
        headers=HEADERS_WORKER,
        json={"content": "Keep the body visible", "typed": typed_status("worker-bad-refs")},
    )
    assert created.status_code == 200

    async with main.connect_db() as db:
        await db.execute(
            "UPDATE messages SET canonical_refs_json = ? WHERE id = ?",
            ("not-json", created.json()["id"]),
        )
        await db.commit()

    restored = await client.get("/api/messages", headers=HEADERS_ENGINEER)
    message = restored.json()[0]
    assert message["typed"] is None
    assert message["typed_integrity"] == "INVALID"
    assert message["typed_error"]["code"] == "E_BRIDGE_PERSISTED_TYPED_INVALID"


async def test_existing_database_migrates_typed_columns_without_losing_legacy_data(tmp_path, monkeypatch):
    import aiosqlite

    legacy_db_file = tmp_path / "legacy-typed.db"
    async with aiosqlite.connect(legacy_db_file) as db:
        await db.execute(
            """
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at REAL NOT NULL
            )
            """
        )
        await db.execute(
            "INSERT INTO messages (role, content, created_at) VALUES ('engineer', 'legacy', 1.0)"
        )
        await db.commit()

    monkeypatch.setattr(main, "DB_PATH", legacy_db_file)
    await main.init_db()

    async with main.connect_db() as db:
        columns = {row["name"] for row in await (await db.execute("PRAGMA table_info(messages)")).fetchall()}
    assert {
        "typed_schema_version",
        "typed_kind",
        "message_key",
        "correlation_id",
        "reply_to_id",
        "expects_reply",
        "typed_payload_json",
        "canonical_refs_json",
    } <= columns

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        legacy = await c.get("/api/messages", headers=HEADERS_WORKER)
        assert legacy.status_code == 200
        assert legacy.json()[0]["content"] == "legacy"
        typed = await c.post(
            "/api/messages",
            headers=HEADERS_WORKER,
            json={"content": "migrated typed", "typed": typed_status("worker-migrated-1")},
        )
        assert typed.status_code == 200
        assert typed.json()["typed"]["kind"] == "STATUS_UPDATE"


async def test_typed_idempotency_returns_original_and_rejects_conflict(client):
    body = {
        "content": "A stable typed message",
        "message_type": "STATUS",
        "typed": typed_status(message_key="worker-idempotent-1"),
    }
    first = await client.post("/api/messages", headers=HEADERS_WORKER, json=body)
    retry = await client.post("/api/messages", headers=HEADERS_WORKER, json=body)

    assert first.status_code == 200
    assert retry.status_code == 200
    assert retry.json() == first.json()
    assert len((await client.get("/api/messages", headers=HEADERS_ENGINEER)).json()) == 1

    conflict_body = {**body, "content": "A different body"}
    conflict = await client.post("/api/messages", headers=HEADERS_WORKER, json=conflict_body)
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "E_BRIDGE_IDEMPOTENCY_CONFLICT"

    other_role = await client.post("/api/messages", headers=HEADERS_ENGINEER, json=body)
    assert other_role.status_code == 200
    assert other_role.json()["id"] != first.json()["id"]


async def test_typed_reply_validation_and_filters(client):
    request_body = {
        "content": "Which storage engine?",
        "message_type": "DECISION_NEEDED",
        "typed": {
            "schema_version": 1,
            "kind": "DECISION_REQUEST",
            "message_key": "engineer-decision-1",
            "correlation_id": "decision-storage",
            "expects_reply": True,
            "payload": {
                "kind": "DECISION_REQUEST",
                "question": "Which storage engine?",
                "options": [{"id": "A", "label": "PostgreSQL"}],
            },
            "canonical_refs": [],
        },
    }
    request = await client.post("/api/messages", headers=HEADERS_ENGINEER, json=request_body)
    assert request.status_code == 200
    request_id = request.json()["id"]

    response_body = {
        "content": "Use PostgreSQL.",
        "message_type": "DECISION_RESOLVED",
        "typed": {
            "schema_version": 1,
            "kind": "DECISION_RESPONSE",
            "message_key": "worker-decision-1",
            "correlation_id": "decision-storage",
            "reply_to_id": request_id,
            "expects_reply": False,
            "payload": {
                "kind": "DECISION_RESPONSE",
                "decision": "A",
                "rationale": "Production durability.",
            },
            "canonical_refs": [],
        },
    }
    response = await client.post("/api/messages", headers=HEADERS_WORKER, json=response_body)
    assert response.status_code == 200
    reply_id = response.json()["id"]

    filtered = await client.get(
        "/api/messages",
        params={
            "typed_kind": "DECISION_RESPONSE",
            "correlation_id": "decision-storage",
            "reply_to_id": request_id,
            "limit": 1,
        },
        headers=HEADERS_ENGINEER,
    )
    assert [message["id"] for message in filtered.json()] == [reply_id]

    same_role = await client.post("/api/messages", headers=HEADERS_ENGINEER, json=response_body)
    assert same_role.status_code == 422
    assert same_role.json()["error"]["code"] == "E_BRIDGE_REPLY_ROLE_INVALID"


async def test_decision_notice_is_a_unilateral_project_decision(client):
    response = await client.post(
        "/api/messages",
        headers=HEADERS_WORKER,
        json={
            "content": "The project selected option A.",
            "message_type": "DECISION_TAKEN",
            "typed": {
                "schema_version": 1,
                "kind": "DECISION_NOTICE",
                "message_key": "worker-decision-notice-1",
                "payload": {
                    "kind": "DECISION_NOTICE",
                    "decision": "A",
                    "rationale": "It keeps the implementation reversible.",
                    "decision_class": "REVERSIBLE",
                },
                "canonical_refs": [],
            },
        },
    )

    assert response.status_code == 200
    assert response.json()["typed"]["kind"] == "DECISION_NOTICE"
    assert response.json()["typed"]["expects_reply"] is False
    assert response.json()["typed_integrity"] == "VALID"


@pytest.mark.parametrize(
    ("typed", "code"),
    (
        (typed_status(message_key="worker-schema-2", state="IN_PROGRESS") | {"schema_version": 2}, "E_BRIDGE_TYPED_SCHEMA_UNSUPPORTED"),
        (typed_status(message_key="worker-extra") | {"payload": {"kind": "STATUS_UPDATE", "state": "IN_PROGRESS", "summary": "ok", "extra": True}}, "E_BRIDGE_TYPED_PAYLOAD_INVALID"),
    ),
)
async def test_typed_validation_errors_are_stable(client, typed, code):
    response = await client.post(
        "/api/messages",
        headers=HEADERS_ENGINEER,
        json={"content": "invalid typed", "typed": typed},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == code


async def test_typed_envelope_size_limit_is_exact_and_fail_closed(client, monkeypatch):
    typed = typed_status(message_key="worker-size-exact")
    envelope = main.parse_typed_envelope(typed)
    exact_size = main._typed_envelope_size(envelope)
    queue = main.create_sse_queue()
    main._subscribers.add(queue)
    try:
        monkeypatch.setattr(main, "MAX_TYPED_ENVELOPE_BYTES", exact_size)
        accepted = await client.post(
            "/api/messages",
            headers=HEADERS_WORKER,
            json={"content": "exactly at the typed limit", "typed": typed},
        )
        assert accepted.status_code == 200
        await asyncio.wait_for(queue.get(), timeout=1)

        monkeypatch.setattr(main, "MAX_TYPED_ENVELOPE_BYTES", exact_size - 1)
        rejected = await client.post(
            "/api/messages",
            headers=HEADERS_WORKER,
            json={"content": "one byte too large", "typed": typed_status("worker-size-too-large")},
        )
        assert rejected.status_code == 413
        assert rejected.json()["error"]["code"] == "E_BRIDGE_TYPED_PAYLOAD_TOO_LARGE"
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(queue.get(), timeout=0.05)
    finally:
        main._subscribers.discard(queue)

    messages = await client.get("/api/messages", headers=HEADERS_ENGINEER)
    assert [message["typed"]["message_key"] for message in messages.json()] == ["worker-size-exact"]


async def test_status_advertises_bridge_typed_schema(client):
    response = await client.get("/api/status")

    assert response.status_code == 200
    assert response.json()["bridge_api_version"] == "2.1.1"
    assert response.json()["typed_message_versions"] == [1]
    assert response.json()["typed_features"] == {
        "idempotency": True,
        "correlation": True,
        "reply_linkage": True,
        "canonical_refs": True,
        "outbox_safe_retry": True,
        "typed_integrity_status": True,
    }
