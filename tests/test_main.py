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
    assert data["expires_in"] == int(main.SSE_TICKET_TTL)
    assert WORKER_TOKEN not in data["ticket"]
    assert await main.resolve_sse_ticket(data["ticket"]) == "worker"


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
