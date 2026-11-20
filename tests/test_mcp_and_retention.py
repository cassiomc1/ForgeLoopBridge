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
    monkeypatch.setattr(main, "IMPROVEMENTS_DIR", tmp_path / "improvements")
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


async def _mcp(client, method, params=None, *, req_id=1, headers=HEADERS_MASTER):
    body = {"jsonrpc": "2.0", "method": method, "params": params or {}}
    if req_id is not None:
        body["id"] = req_id
    return await client.post("/mcp", headers=headers, json=body)


async def test_instructions_and_llms_txt(client):
    instr = await client.get("/instructions")
    assert instr.status_code == 200
    assert "/mcp" in instr.text
    assert "get_context" in instr.text

    llms = await client.get("/llms.txt")
    assert llms.status_code == 200
    assert "/instructions" in llms.text


async def test_mcp_full_protocol_and_tools_end_to_end(client):
    unauth = await client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert unauth.status_code == 401
    assert "Bearer" in unauth.headers.get("www-authenticate", "")
    assert (await client.get("/mcp")).status_code == 405
    assert (await client.delete("/mcp")).status_code == 204

    init_res = await _mcp(client, "initialize", {"protocolVersion": "2025-03-26"})
    assert init_res.status_code == 200
    assert init_res.json()["result"]["serverInfo"]["name"] == "forgeloopbridge"

    notif = await _mcp(client, "notifications/initialized", req_id=None)
    assert notif.status_code == 202

    ping = await _mcp(client, "ping", req_id=2)
    assert ping.json()["result"] == {}

    tools = await _mcp(client, "tools/list", req_id=3)
    tool_names = {t["name"] for t in tools.json()["result"]["tools"]}
    assert len(tool_names) == 21
    assert {"get_context", "register_participant", "get_updates", "prepare_transfer", "create_signal"} <= tool_names

    # Register participant, create channel, post order, report execution, search, signal, improvement
    reg = await _mcp(
        client,
        "tools/call",
        {"name": "register_participant", "arguments": {"name": "mcp-worker", "role": "agent"}},
        headers=HEADERS_AGENT,
        req_id=4,
    )
    reg_data = reg.json()["result"]["structuredContent"]
    pid = reg_data["participant"]["id"]
    p_headers = {"Authorization": f"Bearer {reg_data['token']}"}

    ctx = await _mcp(client, "tools/call", {"name": "get_context", "arguments": {}}, headers=p_headers, req_id=5)
    assert ctx.json()["result"]["structuredContent"]["self"]["name"] == "mcp-worker"

    ch = await _mcp(
        client,
        "tools/call",
        {"name": "create_channel", "arguments": {"name": "mcp-ops"}},
        headers=HEADERS_MASTER,
        req_id=6,
    )
    ch_id = ch.json()["result"]["structuredContent"]["id"]

    ord_res = await _mcp(
        client,
        "tools/call",
        {
            "name": "post_order",
            "arguments": {"order_id": "MCP-1", "content": "Execute MCP test task", "conversation_id": ch_id},
        },
        headers=HEADERS_MASTER,
        req_id=7,
    )
    assert ord_res.json()["result"]["isError"] is False

    upd = await _mcp(
        client,
        "tools/call",
        {"name": "get_updates", "arguments": {"after_id": 0, "conversation_ids": ["general", ch_id]}},
        headers=p_headers,
        req_id=8,
    )
    assert upd.json()["result"]["structuredContent"]["waking_count"] >= 1

    exec_res = await _mcp(
        client,
        "tools/call",
        {
            "name": "report_execution",
            "arguments": {"order_id": "MCP-1", "content": "Completed MCP-1 cleanly", "status": "COMPLETED"},
        },
        headers=p_headers,
        req_id=9,
    )
    assert exec_res.json()["result"]["isError"] is False

    search_res = await _mcp(
        client,
        "tools/call",
        {"name": "read_messages", "arguments": {"query": "Completed"}},
        headers=p_headers,
        req_id=10,
    )
    assert len(search_res.json()["result"]["structuredContent"]["messages"]) == 1

    # Transfer via MCP tools
    blob = b"mcp file payload"
    sha = hashlib.sha256(blob).hexdigest()
    prep = await _mcp(
        client,
        "tools/call",
        {"name": "prepare_transfer", "arguments": {"name": "note.txt", "kind": "file", "size_bytes": len(blob), "sha256": sha}},
        headers=p_headers,
        req_id=11,
    )
    tid = prep.json()["result"]["structuredContent"]["transfer_id"]
    await client.put(f"/api/transfers/{tid}/upload", headers=p_headers, content=blob)
    comp = await _mcp(
        client,
        "tools/call",
        {"name": "complete_transfer", "arguments": {"transfer_id": tid}},
        headers=p_headers,
        req_id=12,
    )
    assert comp.json()["result"]["structuredContent"]["transfer"]["status"] == "COMPLETED"
    fetch = await _mcp(
        client,
        "tools/call",
        {"name": "fetch_transfer", "arguments": {"transfer_id": tid}},
        headers=HEADERS_MASTER,
        req_id=13,
    )
    assert fetch.json()["result"]["structuredContent"]["ticket"]

    # Signal + Improvement + Resources + Prompts
    sig = await _mcp(
        client,
        "tools/call",
        {"name": "create_signal", "arguments": {"participant_id": pid}},
        headers=p_headers,
        req_id=14,
    )
    assert sig.json()["result"]["structuredContent"]["secret"].startswith("sig_")
    rev = await _mcp(
        client,
        "tools/call",
        {"name": "revoke_signal", "arguments": {"participant_id": pid}},
        headers=p_headers,
        req_id=15,
    )
    assert rev.json()["result"]["structuredContent"]["removed"] == 1

    imp = await _mcp(
        client,
        "tools/call",
        {"name": "record_improvement", "arguments": {"task_name": "MCP-1", "summary": "All good"}},
        headers=p_headers,
        req_id=16,
    )
    assert imp.json()["result"]["structuredContent"]["status"] == "ok"

    res_read = await _mcp(client, "resources/read", {"uri": "bridge://instructions"}, req_id=17)
    assert "/mcp" in res_read.json()["result"]["contents"][0]["text"]
    prompt_get = await _mcp(client, "prompts/get", {"name": "master_orchestrator"}, req_id=18)
    assert "Master" in prompt_get.json()["result"]["messages"][0]["content"]["text"]


async def test_append_only_soft_delete_and_retention_prune(client, monkeypatch):
    monkeypatch.setattr(main, "APPEND_ONLY_MODE", True)

    m1 = (await client.post("/api/messages", headers=HEADERS_MASTER, json={"content": "Old searchable message"})).json()
    m2 = (await client.post("/api/messages", headers=HEADERS_MASTER, json={"content": "Recent active message"})).json()

    # Soft delete m1
    d1 = await client.delete(f"/api/messages/{m1['id']}", headers=HEADERS_MASTER)
    assert d1.status_code == 200
    assert d1.json()["soft_deleted"] is True

    # Default read and FTS search hide soft-deleted message, include_deleted=true still returns it
    visible = (await client.get("/api/messages", headers=HEADERS_AGENT)).json()
    assert [m["id"] for m in visible] == [m2["id"]]
    with_deleted = (await client.get("/api/messages", params={"include_deleted": "true"}, headers=HEADERS_AGENT)).json()
    assert [m["id"] for m in with_deleted] == [m1["id"], m2["id"]]

    fts_deleted = (await client.get("/api/messages/search", params={"q": "Old"}, headers=HEADERS_AGENT)).json()
    assert fts_deleted["total"] == 0

    # Retention prune removes messages before m2.id physically and keeps FTS5 consistent
    async with main.connect_db() as db:
        await db.execute("UPDATE messages SET created_at = ? WHERE id = ?", (time.time() - 10 * 86400, m1["id"]))
        await db.commit()

    prune = await client.post(
        "/api/retention/prune",
        headers=HEADERS_MASTER,
        json={"older_than_days": 5},
    )
    assert prune.status_code == 200
    assert prune.json()["removed"] == 1
    assert prune.json()["pruned_through_id"] == m1["id"]

    ret = await client.get("/api/retention", headers=HEADERS_AGENT)
    assert ret.status_code == 200
    assert ret.json()["append_only_mode"] is True
    assert ret.json()["pruned_through_id"] == m1["id"]
