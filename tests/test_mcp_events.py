import atexit
import base64
import hashlib
import hmac
import json
import os
import secrets
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

os.environ.setdefault("ENGINEER_TOKEN", "test_engineer_token_1234567890")
os.environ.setdefault("WORKER_TOKEN", "test_worker_token_0987654321")
_tmpdir = tempfile.mkdtemp()
atexit.register(shutil.rmtree, _tmpdir, ignore_errors=True)
os.environ.setdefault("FORGEBRIDGE_DB", str(Path(_tmpdir) / "test.db"))
os.environ.setdefault("IMPROVEMENTS_DIR", str(Path(_tmpdir) / "improvements"))

import main  # noqa: E402
from bridge_hub.mcp_events import (  # noqa: E402
    ERR_CALLBACK_ENDPOINT,
    ERR_FORBIDDEN,
    ERR_HEADER_MISMATCH,
    ERR_INVALID_PARAMS,
    ERR_NOT_FOUND,
    EVENTS_EXTENSION_ID,
)

HEADERS_MASTER = {"Authorization": f"Bearer {main.MASTER_TOKEN}"}
HEADERS_AGENT = {"Authorization": f"Bearer {main.AGENT_TOKEN}"}


def _make_whsec() -> tuple[str, bytes]:
    raw = secrets.token_bytes(32)
    return "whsec_" + base64.b64encode(raw).decode("ascii"), raw


def _verify_standard_webhook_sig(
    body_bytes: bytes,
    headers: dict[str, str],
    raw_secret: bytes,
) -> bool:
    wid = headers["webhook-id"]
    ts = headers["webhook-timestamp"]
    sig_header = headers["webhook-signature"]
    to_sign = f"{wid}.{ts}.".encode() + body_bytes
    expected = "v1," + base64.b64encode(hmac.new(raw_secret, to_sign, hashlib.sha256).digest()).decode("ascii")
    return expected in sig_header.split(" ")


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
            "mcp_event_subscriptions",
        ):
            await db.execute(f"DELETE FROM {tbl}")
        await db.execute("DELETE FROM conversations WHERE id <> 'general'")
        await db.execute(
            "DELETE FROM bridge_meta WHERE key IN ('retention_pruned_through_id', 'retention_pruned_through_at')"
        )
        await db.commit()
    main._post_timestamps.clear()
    main._mcp_event_webhooks._verified_endpoints.clear()
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


async def test_mcp_events_discovery_and_list(client):
    # 1. initialize advertises io.modelcontextprotocol/events
    init_res = await _mcp(client, "initialize", {"protocolVersion": "2026-07-28"})
    assert init_res.status_code == 200
    init_body = init_res.json()["result"]
    assert init_body["protocolVersion"] == "2026-07-28"
    assert EVENTS_EXTENSION_ID in init_body["capabilities"]["extensions"]

    # 2. server/discover returns complete resultType and extension capabilities
    disc_res = await _mcp(client, "server/discover")
    assert disc_res.status_code == 200
    disc_body = disc_res.json()["result"]
    assert disc_body["resultType"] == "complete"
    assert EVENTS_EXTENSION_ID in disc_body["capabilities"]["extensions"]

    # 3. events/list returns all 5 event types with inputSchema, payloadSchema, and delivery modes
    list_res = await _mcp(client, "events/list")
    assert list_res.status_code == 200
    list_body = list_res.json()["result"]
    assert list_body["resultType"] == "complete"
    names = {e["name"] for e in list_body["events"]}
    assert names == {
        "bridge.message.waking",
        "bridge.message.created",
        "bridge.order.pending",
        "bridge.execution.reported",
        "bridge.transfer.offered",
    }
    for ev in list_body["events"]:
        assert set(ev["delivery"]) == {"poll", "push", "webhook"}
        assert ev["inputSchema"]["type"] == "object"
        assert ev["payloadSchema"]["type"] == "object"


async def test_mcp_events_poll_cursors_max_age_truncation_and_header_validation(client):
    # Pre-existing message before subscription starts
    await client.post("/api/messages", headers=HEADERS_MASTER, json={"content": "Old message before poll"})

    # 1. Bootstrap poll with cursor=None ("start from now") returns empty events and fresh cursor
    p0 = await _mcp(
        client,
        "events/poll",
        {"name": "bridge.order.pending", "arguments": {}, "cursor": None},
        headers=HEADERS_AGENT,
    )
    res0 = p0.json()["result"]
    assert res0["resultType"] == "complete"
    assert res0["events"] == []
    assert res0["truncated"] is False
    assert res0["hasMore"] is False
    cursor0 = res0["cursor"]
    assert ":" in cursor0

    # 2. Post a regular chat message and an ORDER
    await client.post("/api/messages", headers=HEADERS_MASTER, json={"content": "General chat", "message_type": "GENERAL"})
    ord1 = (
        await client.post(
            "/api/orders",
            headers=HEADERS_MASTER,
            json={"order_id": "ORD-EVT-1", "content": "Deploy event pipeline", "priority": "URGENT"},
        )
    ).json()

    # 3. Poll with cursor0 returns only the matching ORDER occurrence
    p1 = await _mcp(
        client,
        "events/poll",
        {"name": "bridge.order.pending", "arguments": {"order_id": "ORD-EVT-1"}, "cursor": cursor0},
        headers=HEADERS_AGENT,
    )
    res1 = p1.json()["result"]
    assert len(res1["events"]) == 1
    occ = res1["events"][0]
    assert occ["eventId"] == f"msg_{ord1['id']}"
    assert occ["name"] == "bridge.order.pending"
    assert occ["data"]["orderId"] == "ORD-EVT-1"
    assert occ["data"]["priority"] == "URGENT"
    assert occ["data"]["waking"] is True
    cursor1 = res1["cursor"]
    assert cursor1 != cursor0

    # 4. Subsequent poll with cursor1 returns empty
    p2 = await _mcp(
        client,
        "events/poll",
        {"name": "bridge.order.pending", "arguments": {}, "cursor": cursor1},
        headers=HEADERS_AGENT,
    )
    assert p2.json()["result"]["events"] == []

    # 5. Stale db_generation cursor yields truncated=True and resets cursor cleanly
    p_stale_gen = await _mcp(
        client,
        "events/poll",
        {"name": "bridge.order.pending", "arguments": {}, "cursor": "deadbeef0000:1"},
        headers=HEADERS_AGENT,
    )
    res_stale = p_stale_gen.json()["result"]
    assert res_stale["truncated"] is True
    assert res_stale["events"] == []

    # 6. maxAgeMs skips old messages and flags truncated=True
    async with main.connect_db() as db:
        await db.execute("UPDATE messages SET created_at = ? WHERE id = ?", (time.time() - 600, ord1["id"]))
        await db.commit()
    p_max_age = await _mcp(
        client,
        "events/poll",
        {"name": "bridge.order.pending", "arguments": {}, "cursor": cursor0, "maxAgeMs": 5000},
        headers=HEADERS_AGENT,
    )
    res_age = p_max_age.json()["result"]
    assert res_age["truncated"] is True
    assert res_age["events"] == []

    # 7. Mcp-Name header mismatch returns HTTP 400 with -32020 HeaderMismatch
    bad_hdr = await client.post(
        "/mcp",
        headers={**HEADERS_AGENT, "Mcp-Name": "wrong.event"},
        json={
            "jsonrpc": "2.0",
            "id": 99,
            "method": "events/poll",
            "params": {"name": "bridge.order.pending", "arguments": {}, "cursor": None},
        },
    )
    assert bad_hdr.status_code == 400
    assert bad_hdr.json()["error"]["code"] == ERR_HEADER_MISMATCH

    # 8. Unknown event returns NotFound (-32023)
    not_found = await _mcp(
        client,
        "events/poll",
        {"name": "bridge.nonexistent", "arguments": {}, "cursor": None},
        headers=HEADERS_AGENT,
    )
    assert not_found.json()["error"]["code"] == ERR_NOT_FOUND


async def test_mcp_events_push_stream_sse(client):
    # Post an initial message and get a cursor before the next message
    async with main.connect_db() as db:
        gen = await main.get_db_generation(db)
    cursor_start = f"{gen}:0"

    await client.post(
        "/api/orders",
        headers=HEADERS_MASTER,
        json={"order_id": "STREAM-1", "content": "Streamed order payload"},
    )

    # Request events/stream with X-MCP-Test-Max-Events: 1 so stream closes cleanly after 1 occurrence
    stream_res = await client.post(
        "/mcp",
        headers={
            **HEADERS_AGENT,
            "Mcp-Name": "bridge.order.pending",
            "X-MCP-Test-Max-Events": "1",
        },
        json={
            "jsonrpc": "2.0",
            "id": 42,
            "method": "events/stream",
            "params": {
                "name": "bridge.order.pending",
                "arguments": {"order_id": "STREAM-1"},
                "cursor": cursor_start,
                "heartbeatMs": 5000,
            },
        },
    )
    assert stream_res.status_code == 200
    assert "text/event-stream" in stream_res.headers.get("content-type", "")

    frames = [
        json.loads(line[5:].strip())
        for line in stream_res.text.splitlines()
        if line.startswith("data:")
    ]
    assert len(frames) == 3
    # Frame 1: notifications/events/active
    assert frames[0]["method"] == "notifications/events/active"
    assert frames[0]["params"]["heartbeatMs"] == 5000
    assert frames[0]["params"]["_meta"]["io.modelcontextprotocol/subscriptionId"] == 42

    # Frame 2: notifications/events/event
    assert frames[1]["method"] == "notifications/events/event"
    assert frames[1]["params"]["name"] == "bridge.order.pending"
    assert frames[1]["params"]["data"]["orderId"] == "STREAM-1"
    assert frames[1]["params"]["cursor"].startswith(f"{gen}:")
    assert frames[1]["params"]["_meta"]["io.modelcontextprotocol/subscriptionId"] == 42

    # Frame 3: StreamEventsResult
    assert frames[2]["id"] == 42
    assert frames[2]["result"]["resultType"] == "complete"


async def test_mcp_events_webhook_subscribe_verification_delivery_rotation_and_unsubscribe(client):
    wh_secret, raw_secret = _make_whsec()
    wh_secret_2, raw_secret_2 = _make_whsec()
    delivered_requests: list[tuple[str, dict[str, Any], str]] = []
    challenge_mode = {"echo_valid": True}

    async def fake_webhook_transport(
        url: str,
        body_obj: dict[str, Any],
        *,
        subscription_id: str,
        secret: str,
        secondary_secret: str | None = None,
        webhook_id: str | None = None,
        timeout: float = 5.0,
        allow_private: bool = False,
    ) -> tuple[int, str, bytes]:
        from bridge_hub.wakes import build_standard_webhook_headers

        body_bytes = json.dumps(body_obj, separators=(",", ":")).encode("utf-8")
        hdrs = build_standard_webhook_headers(
            body_bytes,
            secret,
            webhook_id=webhook_id,
            secondary_secret=secondary_secret,
        )
        hdrs["X-MCP-Subscription-Id"] = subscription_id
        delivered_requests.append((url, body_obj, json.dumps(hdrs)))

        if body_obj.get("type") == "verification":
            if not challenge_mode["echo_valid"]:
                return 200, "ok", b'{"challenge":"wrong"}'
            return 200, "ok", json.dumps({"challenge": body_obj["challenge"]}).encode("utf-8")
        return 200, "ok", b"{}"

    main._mcp_event_webhooks.set_transport(fake_webhook_transport)
    try:
        # 1. Non-https URL or invalid secret rejected with InvalidParams (-32602)
        bad_url = await _mcp(
            client,
            "events/subscribe",
            {
                "name": "bridge.message.waking",
                "arguments": {},
                "delivery": {"mode": "webhook", "url": "http://example.com/hook", "secret": wh_secret},
            },
            headers=HEADERS_AGENT,
        )
        assert bad_url.json()["error"]["code"] == ERR_INVALID_PARAMS

        bad_sec = await _mcp(
            client,
            "events/subscribe",
            {
                "name": "bridge.message.waking",
                "arguments": {},
                "delivery": {"mode": "webhook", "url": "https://example.com/hook", "secret": "not_whsec"},
            },
            headers=HEADERS_AGENT,
        )
        assert bad_sec.json()["error"]["code"] == ERR_INVALID_PARAMS

        # 2. Failed verification challenge returns CallbackEndpointError (-32027)
        challenge_mode["echo_valid"] = False
        fail_chal = await _mcp(
            client,
            "events/subscribe",
            {
                "name": "bridge.message.waking",
                "arguments": {},
                "delivery": {"mode": "webhook", "url": "https://example.com/hook", "secret": wh_secret},
            },
            headers=HEADERS_AGENT,
        )
        assert fail_chal.json()["error"]["code"] == ERR_CALLBACK_ENDPOINT
        assert fail_chal.json()["error"]["data"]["reason"] == "challenge_failed"

        # 3. Valid verification handshake succeeds and returns deterministic sub_id & refreshBefore
        challenge_mode["echo_valid"] = True
        delivered_requests.clear()
        sub_res = await _mcp(
            client,
            "events/subscribe",
            {
                "name": "bridge.message.waking",
                "arguments": {"priority": "URGENT"},
                "delivery": {"mode": "webhook", "url": "https://example.com/hook", "secret": wh_secret},
                "cursor": None,
                "ttlMs": 600000,
            },
            headers=HEADERS_AGENT,
        )
        sub_body = sub_res.json()["result"]
        assert sub_body["resultType"] == "complete"
        sub_id = sub_body["id"]
        assert sub_id.startswith("sub_")
        assert sub_body["refreshBefore"] is not None
        assert sub_body["truncated"] is False
        assert len(delivered_requests) == 1
        assert delivered_requests[0][1]["type"] == "verification"

        # 4. Post a matching URGENT waking message and dispatch webhook
        delivered_requests.clear()
        msg = (
            await client.post(
                "/api/messages",
                headers=HEADERS_MASTER,
                json={"content": "Critical production alert!", "priority": "URGENT"},
            )
        ).json()

        dispatched = await main._mcp_event_webhooks.dispatch_due()
        assert len(dispatched) == 1 and dispatched[0]["ok"] is True
        assert len(delivered_requests) == 1
        _, occ_body, hdrs_json = delivered_requests[0]
        hdrs = json.loads(hdrs_json)
        assert hdrs["X-MCP-Subscription-Id"] == sub_id
        raw_occ_bytes = json.dumps(occ_body, separators=(",", ":")).encode("utf-8")
        assert _verify_standard_webhook_sig(raw_occ_bytes, hdrs, raw_secret)
        assert occ_body["eventId"] == f"msg_{msg['id']}"
        assert occ_body["name"] == "bridge.message.waking"
        assert occ_body["cursor"] is not None

        # 5. Idempotent refresh with rotated secret dual-signs with both old and new secrets
        ref_res = await _mcp(
            client,
            "events/subscribe",
            {
                "name": "bridge.message.waking",
                "arguments": {"priority": "URGENT"},
                "delivery": {"mode": "webhook", "url": "https://example.com/hook", "secret": wh_secret_2},
                "cursor": occ_body["cursor"],
                "ttlMs": 600000,
            },
            headers=HEADERS_AGENT,
        )
        ref_body = ref_res.json()["result"]
        assert ref_body["id"] == sub_id
        assert ref_body["deliveryStatus"]["active"] is True
        assert ref_body["deliveryStatus"]["lastError"] is None

        delivered_requests.clear()
        await client.post(
            "/api/messages",
            headers=HEADERS_MASTER,
            json={"content": "Second urgent alert during secret rotation", "priority": "URGENT"},
        )
        await main._mcp_event_webhooks.dispatch_due()
        assert len(delivered_requests) == 1
        _, occ2_body, hdrs2_json = delivered_requests[0]
        hdrs2 = json.loads(hdrs2_json)
        raw_occ2_bytes = json.dumps(occ2_body, separators=(",", ":")).encode("utf-8")
        # Both new and old secrets verify during the rotation grace window!
        assert _verify_standard_webhook_sig(raw_occ2_bytes, hdrs2, raw_secret_2)
        assert _verify_standard_webhook_sig(raw_occ2_bytes, hdrs2, raw_secret)

        # 6. Cross-tenant isolation: Master cannot unsubscribe Agent's subscription with same tuple
        unsub_other = await _mcp(
            client,
            "events/unsubscribe",
            {
                "name": "bridge.message.waking",
                "arguments": {"priority": "URGENT"},
                "delivery": {"url": "https://example.com/hook"},
            },
            headers=HEADERS_MASTER,
        )
        assert unsub_other.json()["error"]["code"] == ERR_NOT_FOUND

        # 7. Agent unsubscribes its own subscription
        unsub_ok = await _mcp(
            client,
            "events/unsubscribe",
            {
                "name": "bridge.message.waking",
                "arguments": {"priority": "URGENT"},
                "delivery": {"url": "https://example.com/hook"},
            },
            headers=HEADERS_AGENT,
        )
        assert unsub_ok.json()["result"]["resultType"] == "complete"
    finally:
        main._mcp_event_webhooks.set_transport(None)


async def test_mcp_events_participant_removal_terminates_subscription(client):
    wh_secret, _ = _make_whsec()
    delivered: list[dict[str, Any]] = []

    async def fake_transport(url, body_obj, **kwargs):
        delivered.append(body_obj)
        if body_obj.get("type") == "verification":
            return 200, "ok", json.dumps({"challenge": body_obj["challenge"]}).encode("utf-8")
        return 200, "ok", b"{}"

    main._mcp_event_webhooks.set_transport(fake_transport)
    try:
        reg = (
            await client.post(
                "/api/participants",
                headers=HEADERS_AGENT,
                json={"name": "temp-agent", "role": "agent"},
            )
        ).json()
        pid = reg["participant"]["id"]
        p_headers = {"Authorization": f"Bearer {reg['token']}"}

        sub = await _mcp(
            client,
            "events/subscribe",
            {
                "name": "bridge.message.created",
                "arguments": {"participant_id": pid},
                "delivery": {"mode": "webhook", "url": "https://example.com/p-hook", "secret": wh_secret},
            },
            headers=p_headers,
        )
        assert sub.json()["result"]["id"].startswith("sub_")

        # Remove participant and trigger a message
        await client.post(f"/api/participants/{pid}/remove", headers=HEADERS_MASTER)
        await client.post("/api/messages", headers=HEADERS_MASTER, json={"content": "After removal"})

        delivered.clear()
        res = await main._mcp_event_webhooks.dispatch_due()
        assert len(res) == 1 and res[0].get("terminated") is True
        assert len(delivered) == 1
        assert delivered[0]["type"] == "terminated"
        assert delivered[0]["error"]["code"] == ERR_FORBIDDEN
    finally:
        main._mcp_event_webhooks.set_transport(None)
