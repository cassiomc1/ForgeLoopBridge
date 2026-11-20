import atexit
import os
import shutil
import tempfile
from pathlib import Path

import httpx
import pytest

os.environ.setdefault("ENGINEER_TOKEN", "test_engineer_token_1234567890")
os.environ.setdefault("WORKER_TOKEN", "test_worker_token_0987654321")
_tmpdir = tempfile.mkdtemp()
atexit.register(shutil.rmtree, _tmpdir, ignore_errors=True)
os.environ.setdefault("FORGEBRIDGE_DB", str(Path(_tmpdir) / "test.db"))
os.environ.setdefault("IMPROVEMENTS_DIR", str(Path(_tmpdir) / "improvements"))

import main  # noqa: E402
from bridge_hub.mentions import extract_mention_candidates  # noqa: E402
from bridge_hub.schema import sanitize_fts_query  # noqa: E402

HEADERS_MASTER = {"Authorization": f"Bearer {main.MASTER_TOKEN}"}
HEADERS_AGENT = {"Authorization": f"Bearer {main.AGENT_TOKEN}"}


@pytest.fixture(autouse=True)
async def clean_hub():
    await main.init_db()
    async with main.connect_db() as db:
        for tbl in (
            "messages",
            "participants",
            "participant_renames",
            "participant_signals",
            "file_transfers",
            "file_transfer_receipts",
            "wake_targets",
            "wake_sends",
        ):
            await db.execute(f"DELETE FROM {tbl}")
        await db.execute("DELETE FROM conversations WHERE id <> 'general'")
        await db.execute(
            "UPDATE conversations SET message_count = 0, last_message_id = NULL, last_message_at = NULL WHERE id = 'general'"
        )
        await db.commit()
    main._post_timestamps.clear()
    main._stuck_readers.clear()
    yield


@pytest.fixture
async def client():
    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def test_extract_mention_candidates_ignores_code_emails_and_scopes():
    text = (
        "Hey @Alice and @bob_1! Ignore ana@example.com and pkg/@scope.\n"
        "Also ignore `@inline_code` and:\n"
        "```python\n"
        "@fenced_code\n"
        "```\n"
        "Ping @Alice again and @charlie-."
    )
    candidates = extract_mention_candidates(text)
    assert candidates == [
        ["Alice"],
        ["bob_1"],
        ["charlie-", "charlie"],
    ]


def test_sanitize_fts_query_escapes_punctuation_and_operators():
    assert sanitize_fts_query("foo-bar OR test:123") == '"foo-bar" "OR" "test:123"'
    assert sanitize_fts_query('say "hello"') == '"say" """hello"""'
    assert sanitize_fts_query("   ") == ""
    assert sanitize_fts_query('"""') == ""


async def test_mentions_resolve_active_participants_only(client):
    reg_a = await client.post(
        "/api/participants",
        headers=HEADERS_MASTER,
        json={"name": "planner-mac", "role": "master"},
    )
    reg_b = await client.post(
        "/api/participants",
        headers=HEADERS_AGENT,
        json={"name": "worker_01", "role": "agent"},
    )
    pid_a = reg_a.json()["participant"]["id"]
    pid_b = reg_b.json()["participant"]["id"]

    await client.post(f"/api/participants/{pid_b}/remove", headers=HEADERS_MASTER)

    res = await client.post(
        "/api/messages",
        headers=HEADERS_AGENT,
        json={
            "content": "cc @PLANNER-MAC, ignore @worker_01 and `@planner-mac` and dev@example.com",
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["mentions"] == [{"id": pid_a, "name": "planner-mac"}]


async def test_fts5_search_endpoint_and_messages_q_filter(client):
    m1 = await client.post(
        "/api/messages",
        headers=HEADERS_MASTER,
        json={"order_id": "ORD-FTS-1", "content": "Migrate SQLite database-schema safely"},
    )
    m2 = await client.post(
        "/api/messages",
        headers=HEADERS_AGENT,
        json={"order_id": "ORD-FTS-2", "content": "Frontend CSS variables updated"},
    )
    assert m1.status_code == 200 and m2.status_code == 200
    id1 = m1.json()["id"]

    # Search with hyphenated token matches cleanly
    r1 = await client.get(
        "/api/messages/search",
        params={"q": "database-schema"},
        headers=HEADERS_AGENT,
    )
    assert r1.status_code == 200
    assert r1.json()["total"] == 1
    assert r1.json()["messages"][0]["id"] == id1

    # FTS keywords like OR are neutralized into literal terms (no syntax error, no boolean OR)
    r_literal = await client.get(
        "/api/messages/search",
        params={"q": "database-schema OR missing"},
        headers=HEADERS_AGENT,
    )
    assert r_literal.status_code == 200
    assert r_literal.json()["total"] == 0

    # Search via /api/messages?q= composes with order_id filter
    r2 = await client.get(
        "/api/messages",
        params={"q": "ORD-FTS-1"},
        headers=HEADERS_AGENT,
    )
    assert r2.status_code == 200
    assert [m["id"] for m in r2.json()] == [id1]

    # Punctuation-only query never crashes FTS5
    r3 = await client.get("/api/messages/search", params={"q": "*** ---"}, headers=HEADERS_AGENT)
    assert r3.status_code == 200
    assert r3.json()["total"] == 0

    # Deleting a message removes it from FTS5 index cleanly
    del_res = await client.delete(f"/api/messages/{id1}", headers=HEADERS_MASTER)
    assert del_res.status_code == 200
    r4 = await client.get("/api/messages/search", params={"q": "database-schema"}, headers=HEADERS_AGENT)
    assert r4.status_code == 200
    assert r4.json()["total"] == 0
