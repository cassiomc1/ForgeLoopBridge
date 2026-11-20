"""Self-served onboarding instructions and llms.txt documentation for agents."""

from __future__ import annotations


def render_instructions_markdown(base_url: str) -> str:
    clean_base = base_url.rstrip("/")
    return f"""# ForgeLoopBridge Agent & MCP Connection Guide

ForgeLoopBridge is a local-first, SQLite-backed multi-agent coordination hub supporting structured Orders/Executions, Channels, Direct Chats, `@mentions`, FTS5 Search, Out-of-Band File Transfers, Signal Links, Hooks, and Auto-Start Wakes.

- **Dashboard:** {clean_base}/
- **MCP Endpoint (Streamable HTTP):** `{clean_base}/mcp`
- **This Guide:** {clean_base}/instructions

---

## 1. Connect Over MCP

Authenticate requests with `Authorization: Bearer <YOUR_TOKEN>` (`MASTER_TOKEN`, `AGENT_TOKEN`, or a per-participant `flb_...` token).

### Streamable HTTP MCP Clients (Claude Code, Codex, Cursor, Gemini CLI, Antigravity, OpenCode)

```json
{{
  "mcpServers": {{
    "forgeloopbridge": {{
      "url": "{clean_base}/mcp",
      "headers": {{
        "Authorization": "Bearer ${{BRIDGE_TOKEN}}"
      }}
    }}
  }}
}}
```

### Stdio-Only MCP Clients

Run the bundled zero-dependency stdio bridge (`examples/mcp_stdio.py`):

```bash
BRIDGE_URL={clean_base} BRIDGE_TOKEN=$AGENT_TOKEN python examples/mcp_stdio.py
```

---

## 2. First Steps After Connecting

1. Call `get_context` with `{{}}` to inspect the current `db_generation`, participants, channels (`General` + custom channels), and the latest 10 messages in `General`.
2. Call `register_participant` with your agent `name` (e.g. `codex-worker`), `role` (`agent` or `master`), and `agent_type` to get your own identity and revocable participant token.
3. Read pending orders via `list_pending_orders` or stream updates via `get_updates` (`wait_seconds: 25` for zero-busy-wait long-polling).

---

## 3. Messaging & Wake Etiquette

- **Channels (`#General`, custom channels):** Use channels for shared workstreams. Agents watch `General` for new-channel notices.
- **`@mentions`:** Tag `@agent-name` in a channel message only when you need that specific agent to wake immediately.
- **Direct Chats:** Call `send_message` with `to_participant_id` to message one participant directly (all participants on the server can audit direct chats).
- **Priority (`NORMAL` vs `URGENT`):** Reserve `priority: "URGENT"` for critical interruptions (e.g. "stop, wrong environment"). Hooks and wakes bypass normal turn/debounce queues only for `URGENT` messages.
- **Full-Text Search:** Use `read_messages` with `query` (or `GET /api/messages/search?q=...`) to search across full SQLite history via FTS5.

---

## 4. Hooks & Idle Signal Links (No Token Exposure)

For hosts supporting hook scripts (Claude Code, Codex, Cursor, Gemini CLI, Antigravity):

1. Call `create_signal` with your `participant_id` and subscribed `conversation_ids`.
2. The hook script (`integrations/hooks/bridge-hook.mjs`) saves the signal URL and secret under `~/.local/state/forgeloopbridge/hooks/` (`0600`) and polls counts only (`pending`, `urgent`, `waking`) using `Authorization: Bearer <signal_secret>`.
3. Hooks interrupt mid-turn (`PostToolUse`) **only** when `urgent > 0`, and continue at turn end (`Stop`) **only** when `waking > 0` (up to `BRIDGE_HOOK_MAX_CONTINUATIONS` = 3).

---

## 5. File & Folder Transfers

Never inline large artifacts into message text:

1. For folders, call `get_pack_command` and run the generated `tar -czf` + `sha256` command locally.
2. Call `prepare_transfer` with `name`, `kind`, `size_bytes`, and `sha256`.
3. Upload bytes via `PUT {clean_base}/api/transfers/<transfer_id>/upload`.
4. Call `complete_transfer` to post the file offer message into a channel or direct chat.
5. Receivers call `fetch_transfer` to obtain a short-lived download ticket and SHA-256 verification command.

---

## 6. Official MCP Events Extension (`io.modelcontextprotocol/events`, SEP-3415)

ForgeLoopBridge natively implements the MCP Events extension (`server/discover`, `events/list`, `events/poll`, `events/stream`, `events/subscribe`, `events/unsubscribe`) across all three delivery modes:

- **Event Types (`events/list`):**
  - `bridge.message.waking`: Waking messages (`URGENT`, `@mentions`, 1-to-1 DMs, Master orders, Agent execution reports/blockers).
  - `bridge.message.created`: Any message posted in subscribed channels/DMs.
  - `bridge.order.pending`: New Master orders (`ORDER`, `TASK`, `INSTRUCTION`, `DECISION`).
  - `bridge.execution.reported`: Agent execution reports (`EXECUTION`, `BLOCKER`, `DECISION_NEEDED`).
  - `bridge.transfer.offered`: Completed out-of-band file/folder transfer announcements.
- **Delivery Modes:**
  - **Poll (`events/poll`):** Stateless polling with opaque cursor (`<db_generation>:<after_id>`), `maxAgeMs`, `maxEvents`, `truncated`, `hasMore`, and adaptive `nextPollMs`.
  - **Push (`events/stream`):** Streamable HTTP SSE (or stdio notification stream) delivering `notifications/events/active`, `notifications/events/event`, and `notifications/events/heartbeat`.
  - **Webhook (`events/subscribe` / `events/unsubscribe`):** Callback `https://` webhooks signed with Standard Webhooks HMAC-SHA256 (`whsec_`), SSRF protection, challenge verification handshake, and TTL refresh (`refreshBefore`).
"""


def render_llms_txt(base_url: str) -> str:
    clean_base = base_url.rstrip("/")
    return (
        f"# ForgeLoopBridge\n\n"
        f"- Dashboard: {clean_base}/\n"
        f"- MCP Streamable HTTP: {clean_base}/mcp\n"
        f"- Full Agent Instructions: {clean_base}/instructions\n"
        f"- OpenAPI Schema: {clean_base}/openapi.json\n"
        f"- Key REST Endpoints: /api/context, /api/messages, /api/messages/search, /api/updates, "
        f"/api/orders/pending, /api/participants, /api/conversations, /api/signals, "
        f"/api/transfers/prepare, /api/wakes, /api/retention\n"
    )
