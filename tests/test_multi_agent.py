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
    yield


@pytest.fixture
async def client():
    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_participant_lifecycle_tokens_rename_remove_restore(client):
    # Agent token cannot register a master or person participant
    forbidden = await client.post(
        "/api/participants",
        headers=HEADERS_AGENT,
        json={"name": "fake-master", "role": "master"},
    )
    assert forbidden.status_code == 403

    reg = await client.post(
        "/api/participants",
        headers=HEADERS_AGENT,
        json={"name": "codex-runner", "agent_type": "codex", "request_key": "req-1"},
    )
    assert reg.status_code == 200
    data = reg.json()
    pid = data["participant"]["id"]
    ptoken = data["token"]
    assert ptoken.startswith("flb_")

    # Idempotent replay with same request_key
    replay = await client.post(
        "/api/participants",
        headers=HEADERS_AGENT,
        json={"name": "codex-runner", "agent_type": "codex", "request_key": "req-1"},
    )
    assert replay.status_code == 200
    assert replay.json()["created"] is False

    # Whoami distinguishes global vs participant tokens
    global_who = await client.get("/api/whoami", headers=HEADERS_MASTER)
    assert global_who.json() == {"role": "master"}
    part_who = await client.get("/api/whoami", headers={"Authorization": f"Bearer {ptoken}"})
    assert part_who.status_code == 200
    assert part_who.json()["participant"]["name"] == "codex-runner"

    # Rename participant and verify rename history appears in /api/updates
    ren = await client.patch(
        f"/api/participants/{pid}",
        headers={"Authorization": f"Bearer {ptoken}"},
        json={"name": "codex-main", "plugin_name": "hooks", "plugin_version": "0.4.1"},
    )
    assert ren.status_code == 200
    assert ren.json()["name"] == "codex-main"

    updates = await client.get("/api/updates", headers=HEADERS_MASTER)
    assert updates.status_code == 200
    assert updates.json()["renames"][0]["previous_name"] == "codex-runner"
    assert updates.json()["renames"][0]["new_name"] == "codex-main"

    # Soft-remove blocks token auth, restore re-enables it
    rem = await client.post(f"/api/participants/{pid}/remove", headers=HEADERS_MASTER)
    assert rem.status_code == 200
    assert rem.json()["removed_at"] is not None
    blocked = await client.get("/api/whoami", headers={"Authorization": f"Bearer {ptoken}"})
    assert blocked.status_code == 403

    rest = await client.post(f"/api/participants/{pid}/restore", headers=HEADERS_MASTER)
    assert rest.status_code == 200
    assert rest.json()["removed_at"] is None
    ok_again = await client.get("/api/whoami", headers={"Authorization": f"Bearer {ptoken}"})
    assert ok_again.status_code == 200

    # Token rotation invalidates old token
    rot = await client.post(
        f"/api/participants/{pid}/rotate-token",
        headers={"Authorization": f"Bearer {ptoken}"},
    )
    assert rot.status_code == 200
    new_token = rot.json()["token"]
    assert new_token != ptoken
    assert (await client.get("/api/whoami", headers={"Authorization": f"Bearer {ptoken}"})).status_code == 401
    assert (await client.get("/api/whoami", headers={"Authorization": f"Bearer {new_token}"})).status_code == 200


async def test_channels_general_notice_archive_and_direct_chats(client):
    p1 = (await client.post("/api/participants", headers=HEADERS_MASTER, json={"name": "architect", "role": "master"})).json()
    p2 = (await client.post("/api/participants", headers=HEADERS_AGENT, json={"name": "builder", "role": "agent"})).json()
    p3 = (await client.post("/api/participants", headers=HEADERS_AGENT, json={"name": "reviewer", "role": "agent"})).json()

    t1 = {"Authorization": f"Bearer {p1['token']}"}
    t2 = {"Authorization": f"Bearer {p2['token']}"}
    t3 = {"Authorization": f"Bearer {p3['token']}"}

    # Create custom channel -> posts system notice into General
    ch_res = await client.post(
        "/api/conversations/channels",
        headers=t1,
        json={"name": "backend-api", "description": "API design discussion"},
    )
    assert ch_res.status_code == 200
    ch = ch_res.json()
    assert ch["name"] == "backend-api"

    gen_msgs = (await client.get("/api/messages", params={"conversation_id": "general"}, headers=t2)).json()
    assert len(gen_msgs) == 1
    assert gen_msgs[0]["is_system_notice"] is True
    assert "backend-api" in gen_msgs[0]["content"]

    # Cannot archive General
    arch_gen = await client.post(
        "/api/conversations/general/archive",
        headers=t1,
        json={"archived": True},
    )
    assert arch_gen.status_code == 400

    # Optimistic revision conflict on archive
    arch_conflict = await client.post(
        f"/api/conversations/{ch['id']}/archive",
        headers=t1,
        json={"archived": True, "expected_revision": 99},
    )
    assert arch_conflict.status_code == 409

    arch_ok = await client.post(
        f"/api/conversations/{ch['id']}/archive",
        headers=t1,
        json={"archived": True, "expected_revision": 0},
    )
    assert arch_ok.status_code == 200
    assert arch_ok.json()["archived_at"] is not None

    # Posting to archived channel fails
    archived_post = await client.post(
        "/api/messages",
        headers=t2,
        json={"content": "Hello?", "conversation_id": ch["id"]},
    )
    assert archived_post.status_code == 409

    # Direct chat: pair uniqueness, participant-only sending, public audit readability
    dm1 = await client.post(
        "/api/conversations/direct",
        headers=t1,
        json={"target_participant_id": p2["participant"]["id"]},
    )
    assert dm1.status_code == 200 and dm1.json()["created"] is True
    dm_id = dm1.json()["conversation"]["id"]

    dm_reuse = await client.post(
        "/api/conversations/direct",
        headers=t2,
        json={"target_participant_id": p1["participant"]["id"]},
    )
    assert dm_reuse.status_code == 200 and dm_reuse.json()["created"] is False
    assert dm_reuse.json()["conversation"]["id"] == dm_id

    # Third participant CANNOT post into someone else's DM
    intrude = await client.post(
        "/api/messages",
        headers=t3,
        json={"content": "Intruding", "conversation_id": dm_id},
    )
    assert intrude.status_code == 403

    # Participant 1 sends urgent DM via to_participant_id, Participant 3 can audit-read it
    sent_dm = await client.post(
        "/api/messages",
        headers=t1,
        json={"content": "Urgent check", "to_participant_id": p2["participant"]["id"], "priority": "URGENT"},
    )
    assert sent_dm.status_code == 200
    assert sent_dm.json()["conversation_id"] == dm_id
    assert sent_dm.json()["priority"] == "URGENT"

    audit = await client.get("/api/messages", params={"conversation_id": dm_id}, headers=t3)
    assert audit.status_code == 200
    assert len(audit.json()) == 1
    assert audit.json()[0]["content"] == "Urgent check"
