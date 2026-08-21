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
