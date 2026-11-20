# ForgeLoopBridge (Master-Agent Bridge)

> **High-reliability, local-first multi-agent coordination hub and MCP server for orders, executions, channels, direct chats, file transfers, and auto-start wakes.**

ForgeLoopBridge combines a structured Master ⇄ Agent task/order protocol (`ORDER` / `EXECUTION` / `DECISION` / `BLOCKER` / `REVIEW_RESULT`, pending order queries, and automated post-mortem self-improvement logs) with native **MCP (Streamable HTTP + stdio)**, **count-only Signal Links & Agent Hooks**, **SQLite FTS5 full-text search**, **channels/DMs/`@mentions`**, **out-of-band file transfers**, and **outbound wake scheduling** — all running inside a single lightweight Python + SQLite WAL service.

---

## Key Features

- **Master ⇄ Agent Task Protocol**:
  - **Master (`master`)**: Issues structured orders, decisions, cancellations, and reviews.
  - **Agent (`agent`)**: Consumes pending orders, reports progress (`RUNNING`, `COMPLETED`, `FAILED`, `BLOCKED`), and records execution details.
- **Native MCP Server (`/mcp`) & Self-Served Onboarding (`/instructions`)**:
  - Built-in Streamable HTTP JSON-RPC 2.0 endpoint (`POST /mcp`) exposing **21 tools**, resources, and prompts, plus a zero-dependency stdio bridge ([examples/mcp_stdio.py](examples/mcp_stdio.py)).
  - Self-served setup instructions at `GET /instructions` and `GET /llms.txt`.
- **Real-Time SSE & Long-Polling (`wait_seconds`)**:
  - Server-Sent Events (`GET /api/stream` via short-lived tickets) and zero-busy-wait long-polling (`wait_seconds` on `GET /api/messages` and `GET /api/updates`).
  - `db_generation` cursor safety (`X-Bridge-Generation`, `409 E_BRIDGE_GENERATION_MISMATCH`) and stuck-reader detection (`GET /api/health/readers`).
- **Count-Only Signal Links & Universal Agent Hooks**:
  - Hooks for Claude Code, Codex, Cursor, Gemini CLI, and Antigravity ([integrations/hooks/](integrations/hooks/README.md)) receive only counts (`pending`, `urgent`, `waking`) over `Authorization: Bearer` without exposing bearer tokens or message text.
  - Interrupts mid-turn (`PostToolUse`) **only** on `URGENT` messages and continues turn-end (`Stop`) **only** on waking messages (with configurable continuation cap).
- **Multi-Agent Identities, Channels, Direct Chats & `@mentions`**:
  - Register agents or human participants (`/api/participants`) with revocable `flb_...` tokens, rename history, online presence, and soft-remove/restore.
  - Built-in `#General` channel plus custom archivable channels (`/api/conversations/channels`) with automatic General announcements, auditable 1-to-1 Direct Chats (`/api/conversations/direct`), `NORMAL`/`URGENT` delivery priority, and code-safe `@mention` extraction.
- **SQLite WAL + FTS5 Full-Text Search**:
  - `journal_mode=WAL`, `busy_timeout=5000`, atomic idempotency via `message_key`, and synchronized `messages_fts` (`unicode61`) search via `GET /api/messages/search` or `GET /api/messages?q=...`.
- **Out-of-Band File & Folder Transfers**:
  - Reserve, stream-upload with SHA-256 verification, announce in a message, and fetch via short-lived download tickets (`/api/transfers/*`) with receipt tracking and TTL cleanup.
- **Auto-Start Outbound Wakes**:
  - Trigger webhooks, Claude Code routines, or opt-in local commands (`/api/wakes`) with debounce, per-participant `wake_cadence`, `URGENT` bypass, hourly/daily budgets, exponential backoff, and SSRF protection.
- **Append-Only Mode & Retention Pruning**:
  - Optional soft-delete mode (`APPEND_ONLY_MODE=1`) and controlled pruning (`POST /api/retention/prune`) that preserves FTS5 index consistency.
- **Automated Self-Improvement**:
  - Generates standardized `<task_name>-<YYYY-MM-DD>-<HH-MM-SS>.md` logs under `improvements/` on every execution.
- **Web Dashboard v4.0**:
  - Responsive light/dark UI with collapsible Channels/DMs sidebar, FTS5 database search modal, `NORMAL`/`URGENT` composer, `@mention` highlighting, File Transfers modal, Agents & Wakes manager, MCP & Hooks onboarding tab, and Smart Paste.

---

## Quick Start

### 1. Requirements
- Python >= 3.12 (`fastapi`, `uvicorn[standard]`, `aiosqlite`, `pydantic`)
- Optional: Node.js >= 18 (for [integrations/hooks/](integrations/hooks/README.md) and frontend checks)
- Optional: `requests>=2.32` (for [examples/worker_poll.py](examples/worker_poll.py))

```bash
pip install -r requirements.txt
```

### 2. Configure Environment

Copy `.env.example` to `.env` and set secure tokens:

```bash
cp .env.example .env
export MASTER_TOKEN=$(openssl rand -hex 32)
export AGENT_TOKEN=$(openssl rand -hex 32)
```

*(Legacy `ENGINEER_TOKEN` and `WORKER_TOKEN` remain supported as backward-compatible aliases).*

### 3. Start the Server

Directly with Python:
```bash
python main.py
```
Or with uvicorn:
```bash
uvicorn main:app --env-file .env --host 0.0.0.0 --port 8000
```
Or via Docker Compose:
```bash
docker compose up -d
```

Open `http://localhost:8000` in your browser to access the dashboard.

---

## Connecting AI Coding Agents via MCP

Agents can connect directly to `http://localhost:8000/mcp` (or read `GET /instructions` / `GET /llms.txt` in one prompt):

### Streamable HTTP MCP Configuration (Claude Code, Codex, Cursor, Gemini CLI, Antigravity, OpenCode)

```json
{
  "mcpServers": {
    "forgeloopbridge": {
      "url": "http://localhost:8000/mcp",
      "headers": {
        "Authorization": "Bearer ${BRIDGE_TOKEN}"
      }
    }
  }
}
```

### Stdio-Only MCP Clients

Use the bundled zero-dependency stdio proxy:

```bash
BRIDGE_URL=http://localhost:8000 BRIDGE_TOKEN=$AGENT_TOKEN python examples/mcp_stdio.py
```

### Exposed MCP Tools (21)
- **Context & Identity**: `get_context`, `register_participant`, `rename_participant`
- **Messaging & Orders**: `get_updates`, `read_messages`, `send_message`, `list_pending_orders`, `post_order`, `report_execution`
- **Channels**: `create_channel`, `set_channel_archived`
- **File Transfers**: `get_pack_command`, `prepare_transfer`, `complete_transfer`, `fetch_transfer`, `read_transfer`
- **Hooks & Wakes**: `create_signal`, `revoke_signal`, `list_wakes`, `revoke_wake`
- **Self-Improvement**: `record_improvement`

Also exposes MCP resources (`bridge://instructions`, `bridge://prompts/master`, `bridge://prompts/agent`) and MCP prompts (`master_orchestrator`, `agent_executor`).

---

## Agent Hooks & Count-Only Signal Links

See [integrations/hooks/README.md](integrations/hooks/README.md) for full details and ready-to-use manifests (`claude-hooks.json`, `codex-hooks.json`, `cursor-hooks.json`, `gemini-hooks.json`).

1. After joining via MCP, the agent calls `create_signal` with its `participant_id` and `conversation_ids`.
2. The `PostToolUse` hook automatically stores the signal URL and secret in `~/.local/state/forgeloopbridge/hooks/` (`0600`).
3. On each check, the hook sends `GET /api/signal/<id>` with `Authorization: Bearer <signal_secret>` and receives **only counts** (`{"pending": N, "urgent": M, "waking": K, "more": false, "after_id": ..., "cursor": ..., "expires_at": ...}`):
   - **`PostToolUse`** (throttled every 15s): notifies **only** when `urgent > 0`.
   - **`Stop`**: continues the turn **only** when `waking > 0` (DMs, `@mentions`, `URGENT` messages, Master orders, or system notices), capped at `BRIDGE_HOOK_MAX_CONTINUATIONS` (default `3`, max `5`).
   - **`SessionStart` / `UserPromptSubmit`**: reports messages that arrived while idle.

---

## Using with Any Web Chat (Claude, ChatGPT, Gemini)

1. Click **"🤖 AI Chat"** in the dashboard (tabs: **Master**, **Agent**, **Universal Guide**, **MCP & Hooks**).
2. Click **"📋 For Agent"** on an Order card or **"📋 For Master"** on an Execution card to copy formatted prompts.
3. Paste replies back into the dashboard using **"⚡ Smart Paste"** to auto-extract `STATUS:` and `ORDER ID:`.

---

## API Reference

### Authentication & Hardening
- Primary auth: `Authorization: Bearer <MASTER_TOKEN | AGENT_TOKEN | participant flb_... token>`.
- Legacy query/body `?token=` is only accepted on legacy endpoints when `ALLOW_QUERY_TOKEN=1` (emits `Deprecation: true` header) and is rejected on all v4.0 endpoints (`/mcp`, `/api/participants*`, `/api/conversations*`, `/api/updates`, `/api/messages/search`, `/api/signals*`, `/api/transfers*`, `/api/wakes*`, `/api/retention*`). Set `ALLOW_QUERY_TOKEN=0` to disable `?token=` globally.

### Core Endpoints
| Endpoint | Description |
|---|---|
| `GET /healthz`, `GET /api/status` | Liveness check and public metrics (`bridge_api_version`, `db_generation`, `active_participants`, `channels_count`, `stuck_readers_count`) |
| `GET /instructions`, `GET /llms.txt` | Self-served Markdown/text agent onboarding guide |
| `POST /mcp` (`GET` → 405, `DELETE` → 204) | Streamable HTTP JSON-RPC 2.0 MCP server |
| `GET /api/whoami` | Authenticated role (and `participant` object when using a participant token) |
| `GET /api/context` | Server generation, participants, channels, DMs, and latest 10 `#General` messages |
| `POST /api/messages`, `GET /api/messages`, `DELETE /api/messages/{id}` | Post/query/delete messages (`conversation_id`, `to_participant_id`, `priority`, `q`, `wait_seconds`, `generation`, `include_deleted`) |
| `GET /api/messages/search?q=...` | Full-text SQLite FTS5 search across history |
| `GET /api/updates` | Incremental updates with `waking` classification, `renames`, `checkpoint`, and long-polling (`wait_seconds`) |
| `GET /api/health/readers` | Inspect readers stuck polling the same checkpoint repeatedly |
| `GET /api/orders/pending`, `POST /api/orders`, `POST /api/executions` | Pending Master order backlog, order dispatch, and execution reporting |
| `POST /api/stream-ticket`, `GET /api/stream` | Ticket-authenticated real-time SSE stream |

### Multi-Agent, Transfers, Wakes & Retention
- **Participants**: `POST /api/participants`, `GET /api/participants`, `PATCH /api/participants/{id}`, `POST /api/participants/{id}/remove`, `POST /api/participants/{id}/restore`, `POST /api/participants/{id}/rotate-token`.
- **Channels & DMs**: `GET /api/conversations`, `POST /api/conversations/channels`, `POST /api/conversations/{id}/archive`, `POST /api/conversations/direct`.
- **Signal Links**: `POST /api/signals`, `DELETE /api/signals`, `DELETE /api/signals/{id}`, `GET /api/signal/{id}`, `POST /api/signal/{id}/ack`.
- **File Transfers**: `GET /api/transfers/pack-command`, `POST /api/transfers/prepare`, `PUT /api/transfers/{id}/upload`, `POST /api/transfers/{id}/complete`, `GET /api/transfers`, `GET /api/transfers/{id}`, `POST /api/transfers/{id}/download-ticket`, `GET /api/transfers/{id}/download`, `POST /api/transfers/cleanup`.
- **Auto-Start Wakes**: `POST /api/wakes`, `GET /api/wakes`, `DELETE /api/wakes/{id}`, `POST /api/wakes/{id}/test`, `POST /api/wakes/dispatch`.
- **Retention & Self-Improvement**: `GET /api/retention`, `POST /api/retention/prune`, `POST /api/improvements`, `GET /api/improvements`, `GET /api/improvements/{filename}`.

---

## Environment Variables

See [.env.example](.env.example) for defaults:

| Variable | Default | Purpose |
|---|---|---|
| `MASTER_TOKEN`, `AGENT_TOKEN` | *(required)* | Primary Master and Agent bearer tokens |
| `BRIDGE_DB`, `IMPROVEMENTS_DIR`, `UPLOADS_DIR` | `data/bridge.db`, `improvements`, `data/uploads` | SQLite, improvement logs, and file transfer storage paths |
| `ALLOW_QUERY_TOKEN` | `1` | Set to `0` to reject `?token=` query authentication globally |
| `APPEND_ONLY_MODE` | `0` | Set to `1` to make `DELETE /api/messages/{id}` soft-delete (`deleted_at`) |
| `RETENTION_DAYS` | `0` | Default retention policy metadata (`POST /api/retention/prune`) |
| `MAX_LONG_POLL_SECONDS` | `50` | Maximum `wait_seconds` allowed for long-polling |
| `STUCK_READER_THRESHOLD` | `5` | Repeated identical checkpoint polls before flagging a stuck reader |
| `MAX_TRANSFER_BYTES`, `TRANSFER_TTL_DAYS`, `TRANSFER_TICKET_TTL` | `104857600`, `30`, `3600` | File transfer size cap, expiry days, and download ticket TTL |
| `ENABLE_LOCAL_WAKE_COMMANDS` | `0` | Set to `1` to allow `kind="local_command"` wakes |
| `WAKE_ALLOW_PRIVATE_URLS` | `0` | Set to `1` to permit private/loopback wake webhook URLs |
| `WAKE_HOURLY_BUDGET`, `WAKE_DAILY_BUDGET`, `WAKE_TIMEOUT_SECONDS` | `60`, `480`, `5` | Outbound wake rate limits and HTTP timeout |

---

## Running the Agent Poller

```bash
python examples/agent_worker.py \
  --bridge-url http://localhost:8000 \
  --token $AGENT_TOKEN \
  --run-mode daemon \
  --start-mode pending \
  --transport long-poll \
  --wait-seconds 25 \
  --cursor-file ~/.local/state/forgeloopbridge/worker-cursor.json
```

Supported flags:
- `--run-mode daemon|once|bounded` (`--max-idle-polls N`)
- `--start-mode pending|now|history`
- `--transport poll|long-poll|sse` (`--wait-seconds S`)
- `--cursor-file <path>`: persists cursor and `db_generation` atomically (`0600`), resetting automatically on `E_BRIDGE_GENERATION_MISMATCH`
- `--conversation-id <id>`: filter to a specific channel/DM
- `--participant-name <handle>`: auto-register participant identity on startup

---

## Running Tests & Quality Checks

```bash
pytest
ruff check .
python scripts/check_frontend_syntax.py
node tests/frontend_cursor.cjs
python scripts/check_package_install.py
```

`scripts/check_package_install.py` builds a wheel, installs it in a temporary virtual environment outside the checkout, and verifies the installed package (`main`, `self_improvement`, `bridge_hub`, `/instructions`, `/mcp`, FTS5 search, static assets, prompts, and improvement logs).
