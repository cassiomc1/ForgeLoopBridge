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

        # Post new message with metadata to migrated db
        r_post = await c.post(
            "/api/messages",
            headers=HEADERS_ENGINEER,
            json={"content": "new msg", "task_id": "task-x", "message_type": "TASK"},
        )
        assert r_post.status_code == 200
        assert r_post.json()["task_id"] == "task-x"
        assert r_post.json()["message_type"] == "TASK"

