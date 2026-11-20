import asyncio
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
    main._notifier.clear()
    yield


@pytest.fixture
async def client():
    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_long_polling_wakes_immediately_on_new_message(client):
    async def wait_for_msg():
        return await client.get(
            "/api/updates",
            params={"after_id": 0, "wait_seconds": 5.0},
            headers=HEADERS_AGENT,
        )

    task = asyncio.create_task(wait_for_msg())
    await asyncio.sleep(0.08)
    posted = await client.post(
        "/api/messages",
        headers=HEADERS_MASTER,
        json={"order_id": "LP-1", "message_type": "ORDER", "content": "Run checks"},
    )
    assert posted.status_code == 200

    res = await asyncio.wait_for(task, timeout=2.0)
    assert res.status_code == 200
    data = res.json()
    assert len(data["messages"]) == 1
    assert data["messages"][0]["order_id"] == "LP-1"
    assert data["waking_count"] == 1


async def test_generation_mismatch_and_stuck_readers_detection(client, monkeypatch):
    r1 = await client.get("/api/messages", headers=HEADERS_AGENT)
    assert r1.status_code == 200
    gen = r1.headers.get("x-bridge-generation")
    assert gen

    mismatch = await client.get(
        "/api/messages",
        params={"generation": "stale-gen-id"},
        headers=HEADERS_AGENT,
    )
    assert mismatch.status_code == 409
    assert mismatch.json()["error"]["code"] == "E_BRIDGE_GENERATION_MISMATCH"
    assert mismatch.json()["error"]["current_generation"] == gen

    monkeypatch.setattr(main._stuck_readers, "threshold", 3)
    await client.post("/api/messages", headers=HEADERS_MASTER, json={"content": "Wait for pickup"})

    for _ in range(3):
        upd = await client.get(
            "/api/updates",
            params={"after_id": 0, "plugin": "claude-code@0.3.5"},
            headers=HEADERS_AGENT,
        )
        assert upd.status_code == 200

    health = await client.get("/api/health/readers", headers=HEADERS_MASTER)
    assert health.status_code == 200
    stuck = health.json()["stuck_readers"]
    assert len(stuck) == 1
    assert stuck[0]["polls"] == 3
    assert stuck[0]["plugin"] == "claude-code@0.3.5"

    # Advancing checkpoint clears stuck reader
    last_id = upd.json()["after_id"]
    await client.get("/api/updates", params={"after_id": last_id}, headers=HEADERS_AGENT)
    health_after = await client.get("/api/health/readers", headers=HEADERS_MASTER)
    assert health_after.json()["stuck_readers"] == []


async def test_signal_links_counts_only_and_ownership(client):
    p1 = (await client.post("/api/participants", headers=HEADERS_MASTER, json={"name": "boss", "role": "master"})).json()
    p2 = (await client.post("/api/participants", headers=HEADERS_AGENT, json={"name": "agent-a", "role": "agent"})).json()
    p3 = (await client.post("/api/participants", headers=HEADERS_AGENT, json={"name": "agent-b", "role": "agent"})).json()

    t1 = {"Authorization": f"Bearer {p1['token']}"}
    t2 = {"Authorization": f"Bearer {p2['token']}"}
    t3 = {"Authorization": f"Bearer {p3['token']}"}

    # Create channel random-chat
    ch = (await client.post("/api/conversations/channels", headers=t1, json={"name": "random-chat"})).json()

    sig = await client.post(
        "/api/signals",
        headers=t2,
        json={"conversation_ids": ["general"], "include_direct": True},
    )
    assert sig.status_code == 200
    sig_data = sig.json()
    sig_url = sig_data["url"]
    sig_secret = sig_data["secret"]

    # Reject missing bearer secret (even with ?token= query param)
    assert (await client.get(sig_url, params={"token": sig_secret})).status_code == 401

    # Non-waking chatter in General -> pending=1, waking=0, urgent=0
    await client.post(
        "/api/messages",
        headers=t1,
        json={"content": "Secret chatter text XYZ", "message_type": "MESSAGE", "conversation_id": "general"},
    )
    # Urgent DM -> pending=2, waking=1, urgent=1
    await client.post(
        "/api/messages",
        headers=t1,
        json={"content": "Secret urgent DM ABC", "to_participant_id": p2["participant"]["id"], "priority": "URGENT"},
    )
    # Channel @mention from unsubscribed channel -> pending=3, waking=2, urgent=1
    await client.post(
        "/api/messages",
        headers=t1,
        json={"content": "Hey @agent-a look here", "conversation_id": ch["id"]},
    )

    counts_res = await client.get(sig_url, headers={"Authorization": f"Bearer {sig_secret}"})
    assert counts_res.status_code == 200
    counts = counts_res.json()
    assert counts["pending"] == 3
    assert counts["waking"] == 2
    assert counts["urgent"] == 1
    assert "Secret" not in counts_res.text

    # agent-b cannot revoke agent-a's signal link
    assert (
        await client.delete(
            "/api/signals",
            params={"participant_id": p2["participant"]["id"]},
            headers=t3,
        )
    ).status_code == 403
    assert (await client.delete(f"/api/signals/{sig_data['signal_id']}", headers=t3)).status_code == 403

    # agent-a ack advances watermark
    ack = await client.post(
        f"{sig_url}/ack",
        headers={"Authorization": f"Bearer {sig_secret}"},
        json={"after_id": counts["after_id"]},
    )
    assert ack.status_code == 200
    counts_after = (await client.get(sig_url, headers={"Authorization": f"Bearer {sig_secret}"})).json()
    assert counts_after["pending"] == 0

    # agent-a revokes its own signal link
    rev = await client.delete(f"/api/signals/{sig_data['signal_id']}", headers=t2)
    assert rev.status_code == 200 and rev.json()["removed"] == 1
    assert (await client.get(sig_url, headers={"Authorization": f"Bearer {sig_secret}"})).status_code == 404


async def test_query_token_deprecation_and_disable_flag(client, monkeypatch):
    dep = await client.get("/api/messages", params={"token": main.WORKER_TOKEN})
    assert dep.status_code == 200
    assert dep.headers.get("deprecation") == "true"

    monkeypatch.setattr(main, "ALLOW_QUERY_TOKEN", False)
    rejected = await client.get("/api/messages", params={"token": main.WORKER_TOKEN})
    assert rejected.status_code == 401
