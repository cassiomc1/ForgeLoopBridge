"""Native Streamable HTTP MCP (Model Context Protocol) JSON-RPC 2.0 server for ForgeLoopBridge."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException
from pydantic import ValidationError

from bridge_protocol.errors import BridgeProtocolError

from . import HUB_VERSION
from .instructions import render_instructions_markdown
from .mcp_events import EVENTS_CATALOG, EVENTS_EXTENSION_ID, MCPEventsError
from .service import HubServiceError

SUPPORTED_PROTOCOL_VERSIONS = ("2026-07-28", "2025-03-26", "2024-11-05")


@dataclass
class MCPServices:
    """Async callbacks injected from main.py to reuse REST business logic without circular imports."""

    get_context: Callable[[Any, str | None], Awaitable[dict[str, Any]]]
    register_participant: Callable[[Any, dict[str, Any]], Awaitable[dict[str, Any]]]
    rename_participant: Callable[[Any, str, str], Awaitable[dict[str, Any]]]
    get_updates: Callable[[Any, dict[str, Any]], Awaitable[dict[str, Any]]]
    read_messages: Callable[[Any, dict[str, Any]], Awaitable[list[dict[str, Any]]]]
    send_message: Callable[[Any, dict[str, Any]], Awaitable[dict[str, Any]]]
    list_pending_orders: Callable[[Any, dict[str, Any]], Awaitable[list[dict[str, Any]]]]
    post_order: Callable[[Any, dict[str, Any]], Awaitable[dict[str, Any]]]
    report_execution: Callable[[Any, dict[str, Any]], Awaitable[dict[str, Any]]]
    create_channel: Callable[[Any, dict[str, Any]], Awaitable[dict[str, Any]]]
    set_channel_archived: Callable[[Any, dict[str, Any]], Awaitable[dict[str, Any]]]
    get_pack_command: Callable[[str], dict[str, Any]]
    prepare_transfer: Callable[[Any, dict[str, Any]], Awaitable[dict[str, Any]]]
    complete_transfer: Callable[[Any, dict[str, Any]], Awaitable[dict[str, Any]]]
    fetch_transfer: Callable[[Any, dict[str, Any]], Awaitable[dict[str, Any]]]
    read_transfer: Callable[[Any, str], Awaitable[dict[str, Any]]]
    create_signal: Callable[[Any, dict[str, Any]], Awaitable[dict[str, Any]]]
    revoke_signal: Callable[[Any, dict[str, Any]], Awaitable[dict[str, Any]]]
    list_wakes: Callable[[Any, str | None], Awaitable[list[dict[str, Any]]]]
    revoke_wake: Callable[[Any, str], Awaitable[dict[str, Any]]]
    record_improvement: Callable[[Any, dict[str, Any]], Awaitable[dict[str, Any]]]
    get_prompt_text: Callable[[str], str]
    events_poll: Callable[[Any, dict[str, Any]], Awaitable[dict[str, Any]]] | None = None
    events_subscribe: Callable[[Any, dict[str, Any]], Awaitable[dict[str, Any]]] | None = None
    events_unsubscribe: Callable[[Any, dict[str, Any]], Awaitable[dict[str, Any]]] | None = None


def _tool_def(
    name: str,
    description: str,
    properties: dict[str, Any],
    *,
    required: list[str] | None = None,
    read_only: bool = False,
    idempotent: bool = False,
) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": properties,
            "required": required or [],
            "additionalProperties": False,
        },
        "annotations": {
            "readOnlyHint": read_only,
            "idempotentHint": idempotent,
        },
    }


TOOLS_CATALOG: list[dict[str, Any]] = [
    _tool_def(
        "get_context",
        "Inspect server generation, participants, channels, direct chats, and latest 10 General messages.",
        {"participant_id": {"type": "string", "description": "Optional participant ID to highlight"}},
        read_only=True,
        idempotent=True,
    ),
    _tool_def(
        "register_participant",
        "Register an agent or human participant with a unique handle and optional revocable token.",
        {
            "name": {"type": "string"},
            "role": {"type": "string", "enum": ["master", "agent"]},
            "kind": {"type": "string", "enum": ["agent", "person"]},
            "agent_type": {"type": "string"},
            "working_directory": {"type": "string"},
            "joined_note": {"type": "string"},
            "plugin_name": {"type": "string"},
            "plugin_version": {"type": "string"},
            "request_key": {"type": "string"},
            "issue_token": {"type": "boolean"},
        },
        required=["name"],
    ),
    _tool_def(
        "rename_participant",
        "Rename a participant while preserving its ID, history, and signal checkpoints.",
        {
            "participant_id": {"type": "string"},
            "name": {"type": "string"},
        },
        required=["participant_id", "name"],
    ),
    _tool_def(
        "get_updates",
        "Read incremental updates from subscribed channels and direct chats, with optional long-polling (wait_seconds).",
        {
            "after_id": {"type": "integer", "minimum": 0},
            "checkpoint": {"type": "string"},
            "generation": {"type": "string"},
            "participant_id": {"type": "string"},
            "conversation_ids": {"type": "array", "items": {"type": "string"}},
            "include_direct": {"type": "boolean"},
            "waking_only": {"type": "boolean"},
            "wait_seconds": {"type": "number", "minimum": 0, "maximum": 120},
            "limit": {"type": "integer", "minimum": 1, "maximum": 500},
            "plugin": {"type": "string"},
        },
        read_only=True,
    ),
    _tool_def(
        "read_messages",
        "Read message history by conversation/order or search across full history via SQLite FTS5.",
        {
            "query": {"type": "string", "description": "Full-text FTS5 search query"},
            "conversation_id": {"type": "string"},
            "order_id": {"type": "string"},
            "role": {"type": "string"},
            "status": {"type": "string"},
            "after_id": {"type": "integer", "minimum": 0},
            "before_id": {"type": "integer", "minimum": 1},
            "latest": {"type": "boolean"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 200},
        },
        read_only=True,
        idempotent=True,
    ),
    _tool_def(
        "send_message",
        "Send a message to a channel or directly to another participant (with optional @mentions, priority, order_id, or typed envelope).",
        {
            "content": {"type": "string"},
            "conversation_id": {"type": "string"},
            "to_participant_id": {"type": "string"},
            "participant_id": {"type": "string"},
            "priority": {"type": "string", "enum": ["NORMAL", "URGENT"]},
            "order_id": {"type": "string"},
            "message_type": {"type": "string"},
            "status": {"type": "string"},
            "reply_to_id": {"type": "integer", "minimum": 1},
            "message_key": {"type": "string"},
            "payload": {"type": "object"},
            "typed": {"type": "object"},
        },
        required=["content"],
        idempotent=True,
    ),
    _tool_def(
        "list_pending_orders",
        "List unfinished Master orders awaiting completion or cancellation.",
        {
            "limit": {"type": "integer", "minimum": 1, "maximum": 200},
            "before_id": {"type": "integer", "minimum": 1},
        },
        read_only=True,
        idempotent=True,
    ),
    _tool_def(
        "post_order",
        "Publish a Master order (requires Master role).",
        {
            "order_id": {"type": "string"},
            "content": {"type": "string"},
            "status": {"type": "string"},
            "priority": {"type": "string", "enum": ["NORMAL", "URGENT"]},
            "conversation_id": {"type": "string"},
            "to_participant_id": {"type": "string"},
            "participant_id": {"type": "string"},
            "message_key": {"type": "string"},
            "reply_to_id": {"type": "integer", "minimum": 1},
            "payload": {"type": "object"},
        },
        required=["order_id", "content"],
        idempotent=True,
    ),
    _tool_def(
        "report_execution",
        "Submit an Agent execution report and auto-record self-improvement notes (requires Agent role).",
        {
            "order_id": {"type": "string"},
            "content": {"type": "string"},
            "status": {"type": "string"},
            "priority": {"type": "string", "enum": ["NORMAL", "URGENT"]},
            "conversation_id": {"type": "string"},
            "to_participant_id": {"type": "string"},
            "participant_id": {"type": "string"},
            "message_key": {"type": "string"},
            "reply_to_id": {"type": "integer", "minimum": 1},
            "payload": {"type": "object"},
            "improvements": {"type": ["array", "string"]},
            "issues": {"type": ["array", "string"]},
            "action_items": {"type": ["array", "string"]},
        },
        required=["order_id", "content"],
        idempotent=True,
    ),
    _tool_def(
        "create_channel",
        "Create a public channel and automatically announce it in General.",
        {
            "name": {"type": "string"},
            "description": {"type": "string"},
            "participant_id": {"type": "string"},
        },
        required=["name"],
    ),
    _tool_def(
        "set_channel_archived",
        "Archive or reopen a channel (General cannot be archived). Supports expected_revision.",
        {
            "conversation_id": {"type": "string"},
            "archived": {"type": "boolean"},
            "expected_revision": {"type": "integer", "minimum": 0},
        },
        required=["conversation_id", "archived"],
        idempotent=True,
    ),
    _tool_def(
        "get_pack_command",
        "Get a local shell command to pack a folder into .tar.gz and calculate size_bytes, file_count, and sha256.",
        {"folder": {"type": "string"}},
        read_only=True,
        idempotent=True,
    ),
    _tool_def(
        "prepare_transfer",
        "Step 1 of sending a file or packed folder out-of-band.",
        {
            "name": {"type": "string"},
            "kind": {"type": "string", "enum": ["file", "folder"]},
            "size_bytes": {"type": "integer", "minimum": 1},
            "sha256": {"type": "string"},
            "content_type": {"type": "string"},
            "file_count": {"type": "integer", "minimum": 0},
            "conversation_id": {"type": "string"},
            "participant_id": {"type": "string"},
        },
        required=["name", "kind", "size_bytes", "sha256"],
    ),
    _tool_def(
        "complete_transfer",
        "Step 2 of sending a file after PUT upload finishes: posts the offer message.",
        {
            "transfer_id": {"type": "string"},
            "content": {"type": "string"},
            "conversation_id": {"type": "string"},
            "to_participant_id": {"type": "string"},
            "participant_id": {"type": "string"},
            "order_id": {"type": "string"},
            "message_key": {"type": "string"},
        },
        required=["transfer_id"],
        idempotent=True,
    ),
    _tool_def(
        "fetch_transfer",
        "Accept a file transfer offer: logs receipt and returns a short-lived download ticket and SHA-256 verification command.",
        {
            "transfer_id": {"type": "string"},
            "participant_id": {"type": "string"},
        },
        required=["transfer_id"],
    ),
    _tool_def(
        "read_transfer",
        "Inspect a transfer's metadata and download receipts without downloading bytes.",
        {"transfer_id": {"type": "string"}},
        required=["transfer_id"],
        read_only=True,
        idempotent=True,
    ),
    _tool_def(
        "create_signal",
        "Create or rotate a count-only signal link for hook scripts (never exposes message text).",
        {
            "participant_id": {"type": "string"},
            "conversation_ids": {"type": "array", "items": {"type": "string"}},
            "include_direct": {"type": "boolean"},
            "after_id": {"type": "integer", "minimum": 0},
            "ttl_seconds": {"type": "number", "minimum": 60, "maximum": 604800},
        },
        required=["participant_id"],
    ),
    _tool_def(
        "revoke_signal",
        "Revoke a participant's signal link.",
        {
            "signal_id": {"type": "string"},
            "participant_id": {"type": "string"},
        },
        idempotent=True,
    ),
    _tool_def(
        "list_wakes",
        "List configured outbound auto-start wake targets (secrets masked).",
        {"participant_id": {"type": "string"}},
        read_only=True,
        idempotent=True,
    ),
    _tool_def(
        "revoke_wake",
        "Delete an outbound auto-start wake target.",
        {"wake_id": {"type": "string"}},
        required=["wake_id"],
        idempotent=True,
    ),
    _tool_def(
        "record_improvement",
        "Create a standardized self-improvement markdown record under improvements/.",
        {
            "task_name": {"type": "string"},
            "summary": {"type": "string"},
            "improvements": {"type": ["array", "string"]},
            "issues": {"type": ["array", "string"]},
            "action_items": {"type": ["array", "string"]},
            "status": {"type": "string"},
        },
        required=["summary"],
    ),
]


def _jsonrpc_result(req_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _jsonrpc_error(req_id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": err}


async def _dispatch_tool(
    name: str,
    args: dict[str, Any],
    auth: Any,
    services: MCPServices,
) -> dict[str, Any]:
    if name == "get_context":
        return await services.get_context(auth, args.get("participant_id"))
    if name == "register_participant":
        return await services.register_participant(auth, args)
    if name == "rename_participant":
        return await services.rename_participant(auth, args["participant_id"], args["name"])
    if name == "get_updates":
        return await services.get_updates(auth, args)
    if name == "read_messages":
        return {"messages": await services.read_messages(auth, args)}
    if name == "send_message":
        return await services.send_message(auth, args)
    if name == "list_pending_orders":
        return {"orders": await services.list_pending_orders(auth, args)}
    if name == "post_order":
        return await services.post_order(auth, args)
    if name == "report_execution":
        return await services.report_execution(auth, args)
    if name == "create_channel":
        return await services.create_channel(auth, args)
    if name == "set_channel_archived":
        return await services.set_channel_archived(auth, args)
    if name == "get_pack_command":
        return services.get_pack_command(args.get("folder") or "<folder>")
    if name == "prepare_transfer":
        return await services.prepare_transfer(auth, args)
    if name == "complete_transfer":
        return await services.complete_transfer(auth, args)
    if name == "fetch_transfer":
        return await services.fetch_transfer(auth, args)
    if name == "read_transfer":
        return await services.read_transfer(auth, args["transfer_id"])
    if name == "create_signal":
        return await services.create_signal(auth, args)
    if name == "revoke_signal":
        return await services.revoke_signal(auth, args)
    if name == "list_wakes":
        return {"wakes": await services.list_wakes(auth, args.get("participant_id"))}
    if name == "revoke_wake":
        return await services.revoke_wake(auth, args["wake_id"])
    if name == "record_improvement":
        return await services.record_improvement(auth, args)
    raise HubServiceError("E_MCP_UNKNOWN_TOOL", f"Unknown tool '{name}'", status_code=404)


async def handle_mcp_message(
    payload: Any,
    *,
    auth: Any,
    services: MCPServices,
    base_url: str,
) -> dict[str, Any] | list[dict[str, Any]] | None:
    """Handle one JSON-RPC 2.0 message or a batch of messages.

    Returns ``None`` for notifications (no ``id``), so HTTP layer returns 202 Accepted.
    """
    if isinstance(payload, list):
        if not payload:
            return _jsonrpc_error(None, -32600, "Invalid empty batch")
        responses = []
        for item in payload:
            res = await _handle_single(item, auth=auth, services=services, base_url=base_url)
            if res is not None:
                responses.append(res)
        return responses or None
    return await _handle_single(payload, auth=auth, services=services, base_url=base_url)


async def _handle_single(
    msg: Any,
    *,
    auth: Any,
    services: MCPServices,
    base_url: str,
) -> dict[str, Any] | None:
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str):
        return _jsonrpc_error(msg.get("id") if isinstance(msg, dict) else None, -32600, "Invalid JSON-RPC request")

    req_id = msg.get("id")
    is_notification = "id" not in msg
    method = msg["method"]
    params = msg.get("params") if isinstance(msg.get("params"), dict) else {}

    if method == "notifications/initialized" or method.startswith("notifications/"):
        return None if is_notification else _jsonrpc_result(req_id, {})

    server_capabilities = {
        "tools": {"listChanged": False},
        "resources": {"subscribe": False, "listChanged": False},
        "prompts": {"listChanged": False},
        "extensions": {
            EVENTS_EXTENSION_ID: {
                "listChanged": False,
            },
        },
    }
    server_info = {
        "name": "forgeloopbridge",
        "version": HUB_VERSION,
    }
    server_instructions = (
        "Start with get_context, register_participant if needed, "
        "read pending orders with list_pending_orders or get_updates(wait_seconds=25) "
        "(or subscribe via MCP Events io.modelcontextprotocol/events), "
        "and use @mentions or priority=URGENT sparingly."
    )

    if method == "initialize":
        requested = params.get("protocolVersion")
        negotiated = (
            requested
            if isinstance(requested, str) and requested in SUPPORTED_PROTOCOL_VERSIONS
            else "2025-03-26"
        )
        return _jsonrpc_result(
            req_id,
            {
                "protocolVersion": negotiated,
                "capabilities": server_capabilities,
                "serverInfo": server_info,
                "instructions": server_instructions,
            },
        )

    if method == "server/discover":
        requested = params.get("protocolVersion")
        negotiated = (
            requested
            if isinstance(requested, str) and requested in SUPPORTED_PROTOCOL_VERSIONS
            else SUPPORTED_PROTOCOL_VERSIONS[0]
        )
        return _jsonrpc_result(
            req_id,
            {
                "resultType": "complete",
                "protocolVersion": negotiated,
                "capabilities": server_capabilities,
                "serverInfo": server_info,
                "instructions": server_instructions,
            },
        )

    if method == "events/list":
        return _jsonrpc_result(
            req_id,
            {
                "resultType": "complete",
                "events": EVENTS_CATALOG,
            },
        )

    if method in {"events/poll", "events/subscribe", "events/unsubscribe"}:
        handler_map = {
            "events/poll": services.events_poll,
            "events/subscribe": services.events_subscribe,
            "events/unsubscribe": services.events_unsubscribe,
        }
        handler = handler_map[method]
        if handler is None:
            return _jsonrpc_error(req_id, -32601, f"Method '{method}' not available")
        try:
            res_data = await handler(auth, params)
            return _jsonrpc_result(req_id, res_data)
        except MCPEventsError as exc:
            return _jsonrpc_error(req_id, exc.code, exc.message, exc.data)
        except HubServiceError as exc:
            return _jsonrpc_error(req_id, -32602, exc.message, {"code": exc.code})

    if method == "ping":
        return None if is_notification else _jsonrpc_result(req_id, {})

    if method == "tools/list":
        return _jsonrpc_result(req_id, {"tools": TOOLS_CATALOG})

    if method == "tools/call":
        tool_name = params.get("name")
        if not isinstance(tool_name, str) or not tool_name:
            return _jsonrpc_error(req_id, -32602, "tools/call requires a string 'name'")
        arguments = params.get("arguments")
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            return _jsonrpc_error(req_id, -32602, "tools/call 'arguments' must be an object")

        try:
            data = await _dispatch_tool(tool_name, arguments, auth, services)
            serialized = json.dumps(data, ensure_ascii=False, default=str)
            return _jsonrpc_result(
                req_id,
                {
                    "content": [{"type": "text", "text": serialized}],
                    "structuredContent": data,
                    "isError": False,
                },
            )
        except (BridgeProtocolError, HubServiceError) as exc:
            err_payload = {"error": {"code": exc.code, "message": exc.message}}
            return _jsonrpc_result(
                req_id,
                {
                    "content": [{"type": "text", "text": json.dumps(err_payload)}],
                    "structuredContent": err_payload,
                    "isError": True,
                },
            )
        except HTTPException as exc:
            err_payload = {"error": {"code": f"HTTP_{exc.status_code}", "message": str(exc.detail)}}
            return _jsonrpc_result(
                req_id,
                {
                    "content": [{"type": "text", "text": json.dumps(err_payload)}],
                    "structuredContent": err_payload,
                    "isError": True,
                },
            )
        except (ValidationError, KeyError, ValueError, TypeError) as exc:
            err_payload = {"error": {"code": "E_MCP_INVALID_ARGS", "message": str(exc)}}
            return _jsonrpc_result(
                req_id,
                {
                    "content": [{"type": "text", "text": json.dumps(err_payload)}],
                    "structuredContent": err_payload,
                    "isError": True,
                },
            )

    if method == "resources/list":
        return _jsonrpc_result(
            req_id,
            {
                "resources": [
                    {
                        "uri": "bridge://instructions",
                        "name": "ForgeLoopBridge Instructions",
                        "mimeType": "text/markdown",
                    },
                    {
                        "uri": "bridge://prompts/master",
                        "name": "Universal Master Prompt",
                        "mimeType": "text/markdown",
                    },
                    {
                        "uri": "bridge://prompts/agent",
                        "name": "Universal Agent Prompt",
                        "mimeType": "text/markdown",
                    },
                ]
            },
        )

    if method == "resources/read":
        uri = params.get("uri")
        if uri == "bridge://instructions":
            text = render_instructions_markdown(base_url)
        elif uri == "bridge://prompts/master":
            text = services.get_prompt_text("master")
        elif uri == "bridge://prompts/agent":
            text = services.get_prompt_text("agent")
        else:
            return _jsonrpc_error(req_id, -32602, f"Unknown resource URI: {uri}")
        return _jsonrpc_result(
            req_id,
            {"contents": [{"uri": uri, "mimeType": "text/markdown", "text": text}]},
        )

    if method == "prompts/list":
        return _jsonrpc_result(
            req_id,
            {
                "prompts": [
                    {
                        "name": "master_orchestrator",
                        "description": "System prompt for running Master (Architect/Orchestrator).",
                    },
                    {
                        "name": "agent_executor",
                        "description": "System prompt for running Agent (Executor).",
                    },
                ]
            },
        )

    if method == "prompts/get":
        pname = params.get("name")
        if pname == "master_orchestrator":
            text = services.get_prompt_text("master")
        elif pname == "agent_executor":
            text = services.get_prompt_text("agent")
        else:
            return _jsonrpc_error(req_id, -32602, f"Unknown prompt: {pname}")
        return _jsonrpc_result(
            req_id,
            {
                "description": pname,
                "messages": [{"role": "user", "content": {"type": "text", "text": text}}],
            },
        )

    if is_notification:
        return None
    return _jsonrpc_error(req_id, -32601, f"Method '{method}' not found")
