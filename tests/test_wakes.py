import atexit
import os
import shutil
import tempfile
import time
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
        await db.commit()
    main._post_timestamps.clear()
    yield


@pytest.fixture
async def client():
    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_wake_ssrf_guard_local_opt_in_and_secret_masking(client, monkeypatch):
    reg = await client.post(
        "/api/participants",
        headers=HEADERS_AGENT,
        json={"name": "wake-agent", "role": "agent"},
    )
    pid = reg.json()["participant"]["id"]

    # Private / localhost URL blocked by default
    blocked = await client.post(
        "/api/wakes",
        headers=HEADERS_MASTER,
        json={
            "participant_id": pid,
            "kind": "webhook",
            "url": "http://127.0.0.1:9999/hook",
            "secret": "supersecret9876",
        },
    )
    assert blocked.status_code == 422

    # Local commands disabled by default
    local_blocked = await client.post(
        "/api/wakes",
        headers=HEADERS_MASTER,
        json={
            "participant_id": pid,
            "kind": "local_command",
            "command": ["echo", "hi"],
        },
    )
    assert local_blocked.status_code == 403

    # Opt-in allows local_command & private URLs; secret is masked in reads
    monkeypatch.setattr(main, "ENABLE_LOCAL_WAKE_COMMANDS", True)
    monkeypatch.setattr(main, "WAKE_ALLOW_PRIVATE_URLS", True)
    created = await client.post(
        "/api/wakes",
        headers=HEADERS_MASTER,
        json={
            "participant_id": pid,
            "kind": "webhook",
            "url": "https://example.com/wake",
            "secret": "supersecret9876",
        },
    )
    assert created.status_code == 200
    assert created.json()["secret_hint"] == "9876"
    assert "supersecret" not in created.text

    listed = await client.get("/api/wakes", headers=HEADERS_MASTER)
    assert listed.status_code == 200
    assert "supersecret" not in listed.text
    assert listed.json()[0]["secret_hint"] == "9876"


async def test_wake_scheduling_debounce_urgent_bypass_budget_and_backoff(client, monkeypatch):
    sent_calls = []
    fail_mode = {"fail": False}

    async def fake_http_sender(url, payload, secret, timeout):
        sent_calls.append((url, payload, secret))
        if fail_mode["fail"]:
            return False, "http_500"
        return True, "ok"

    main._wake_scheduler.set_transports(http_sender=fake_http_sender)
    try:
        reg = await client.post(
            "/api/participants",
            headers=HEADERS_MASTER,
            json={"name": "routine-bot", "role": "agent", "wake_cadence": "every_minute"},
        )
        pid = reg.json()["participant"]["id"]

        wake = await client.post(
            "/api/wakes",
            headers=HEADERS_MASTER,
            json={
                "participant_id": pid,
                "kind": "claude_routine",
                "url": "https://example.com/routine",
                "secret": "tok_abcd",
                "channel_ids": ["general"],
                "debounce_seconds": 10.0,
                "min_interval_seconds": 60.0,
                "max_attempts": 2,
            },
        )
        assert wake.status_code == 200
        wid = wake.json()["id"]

        # Normal message schedules due_at in the future (debounce)
        await client.post(
            "/api/messages",
            headers=HEADERS_MASTER,
            json={"content": "Please review @routine-bot", "conversation_id": "general"},
        )
        state = (await client.get("/api/wakes", headers=HEADERS_MASTER)).json()[0]
        assert state["due_at"] is not None
        assert len(sent_calls) == 0

        # Dispatch at now+15 fires debounced wake
        res = await main._wake_scheduler.dispatch_due(now=time.time() + 15.0)
        assert len(res) == 1 and res[0]["ok"] is True
        assert len(sent_calls) == 1

        # URGENT message bypasses 60s cadence & 10s debounce when failures==0
        await client.post(
            "/api/messages",
            headers=HEADERS_MASTER,
            json={"content": "Stop deploy @routine-bot", "priority": "URGENT"},
        )
        res_urgent = await main._wake_scheduler.dispatch_due(now=time.time())
        assert len(res_urgent) == 1 and res_urgent[0]["ok"] is True
        assert len(sent_calls) == 2

        # Budget enforcement defers wake
        monkeypatch.setattr(main, "WAKE_HOURLY_BUDGET", 2)
        await client.post(
            "/api/messages",
            headers=HEADERS_MASTER,
            json={"content": "Third @routine-bot", "priority": "URGENT"},
        )
        res_budget = await main._wake_scheduler.dispatch_due(now=time.time())
        assert res_budget[0]["error"] == "budget"

        # Failures trigger backoff and auto-disable at max_attempts=2
        monkeypatch.setattr(main, "WAKE_HOURLY_BUDGET", 50)
        fail_mode["fail"] = True
        r1 = await main._wake_scheduler.dispatch_due(force_target_id=wid)
        assert r1[0]["ok"] is False and r1[0]["failures"] == 1
        r2 = await main._wake_scheduler.dispatch_due(force_target_id=wid)
        assert r2[0]["ok"] is False and r2[0]["failures"] == 2

        disabled = (await client.get("/api/wakes", headers=HEADERS_MASTER)).json()[0]
        assert disabled["disabled_at"] is not None
        assert "max_attempts" in disabled["disabled_reason"]
    finally:
        from bridge_hub import wakes as wakes_mod
        main._wake_scheduler.set_transports(
            http_sender=wakes_mod._default_http_sender,
            command_runner=wakes_mod._default_command_runner,
        )
