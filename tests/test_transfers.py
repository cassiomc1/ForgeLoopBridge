import atexit
import hashlib
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
async def clean_hub(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "UPLOADS_DIR", tmp_path / "uploads")
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
    main._transfer_tickets.clear()
    yield


@pytest.fixture
async def client():
    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_transfer_prepare_upload_complete_ticket_download_and_receipts(client):
    pack = await client.get("/api/transfers/pack-command", params={"folder": "dist"}, headers=HEADERS_AGENT)
    assert pack.status_code == 200
    assert "tar -czf" in pack.json()["command"]

    payload = b"hello transfer binary content \x00\x01\x02"
    sha = hashlib.sha256(payload).hexdigest()

    prep = await client.post(
        "/api/transfers/prepare",
        headers=HEADERS_AGENT,
        json={"name": "artifact.bin", "kind": "file", "size_bytes": len(payload), "sha256": sha},
    )
    assert prep.status_code == 200
    tid = prep.json()["transfer_id"]

    # Wrong SHA-256 rejected on upload
    bad_up = await client.put(
        f"/api/transfers/{tid}/upload",
        headers=HEADERS_AGENT,
        content=b"wrong" + payload[5:],
    )
    assert bad_up.status_code == 400

    # Valid upload succeeds
    ok_up = await client.put(
        f"/api/transfers/{tid}/upload",
        headers=HEADERS_AGENT,
        content=payload,
    )
    assert ok_up.status_code == 200
    assert ok_up.json()["status"] == "UPLOADED"

    # Complete posts message with transfer reference
    comp = await client.post(
        f"/api/transfers/{tid}/complete",
        headers=HEADERS_AGENT,
        json={"content": "Build artifact attached"},
    )
    assert comp.status_code == 200
    comp_data = comp.json()
    assert comp_data["transfer"]["status"] == "COMPLETED"
    assert comp_data["message"]["transfer_id"] == tid

    # Idempotent complete returns same message
    comp_retry = await client.post(
        f"/api/transfers/{tid}/complete",
        headers=HEADERS_AGENT,
        json={"content": "Build artifact attached"},
    )
    assert comp_retry.status_code == 200
    assert comp_retry.json()["message"]["id"] == comp_data["message"]["id"]

    # Download ticket records receipt and downloads exact bytes without auth header
    ticket_res = await client.post(
        f"/api/transfers/{tid}/download-ticket",
        headers=HEADERS_MASTER,
        json={},
    )
    assert ticket_res.status_code == 200
    download_url = ticket_res.json()["download_url"]

    dl = await client.get(download_url)
    assert dl.status_code == 200
    assert dl.content == payload
    assert dl.headers["x-transfer-sha256"] == sha

    details = await client.get(f"/api/transfers/{tid}", headers=HEADERS_MASTER)
    assert details.status_code == 200
    assert len(details.json()["receipts"]) == 1
    assert details.json()["receipts"][0]["fetch_count"] == 1


async def test_transfer_size_limit_path_traversal_and_expiry_cleanup(client, monkeypatch):
    monkeypatch.setattr(main, "MAX_TRANSFER_BYTES", 2048)
    too_big = await client.post(
        "/api/transfers/prepare",
        headers=HEADERS_AGENT,
        json={"name": "big.bin", "kind": "file", "size_bytes": 4096, "sha256": "a" * 64},
    )
    assert too_big.status_code == 413

    traversal = await client.post(
        "/api/transfers/prepare",
        headers=HEADERS_AGENT,
        json={"name": "../evil.bin", "kind": "file", "size_bytes": 100, "sha256": "a" * 64},
    )
    assert traversal.status_code == 422

    payload = b"expiring payload"
    sha = hashlib.sha256(payload).hexdigest()
    prep = await client.post(
        "/api/transfers/prepare",
        headers=HEADERS_AGENT,
        json={"name": "expire.txt", "kind": "file", "size_bytes": len(payload), "sha256": sha},
    )
    tid = prep.json()["transfer_id"]
    await client.put(f"/api/transfers/{tid}/upload", headers=HEADERS_AGENT, content=payload)
    await client.post(f"/api/transfers/{tid}/complete", headers=HEADERS_AGENT, json={})

    blob = main.UPLOADS_DIR / f"{tid}.bin"
    assert blob.is_file()

    # Force expiry and trigger cleanup
    async with main.connect_db() as db:
        await db.execute("UPDATE file_transfers SET expires_at = ? WHERE id = ?", (time.time() - 5, tid))
        await db.commit()

    clean = await client.post("/api/transfers/cleanup", headers=HEADERS_MASTER)
    assert clean.status_code == 200
    assert clean.json()["cleaned"] == 1
    assert not blob.exists()
    assert (await client.get(f"/api/transfers/{tid}/download", headers=HEADERS_MASTER)).status_code == 410
