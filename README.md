# Master-Agent Bridge

> **High-reliability, minimalist communication hub for exchanging orders and executions between Master and Agent.**

A lightweight, robust message broker and dashboard designed for multi-agent workflows. It provides rock-solid persistence, atomic idempotency, real-time push via Server-Sent Events (SSE), and an intuitive Web UI.

---

## Key Features

- **Master ⇄ Agent Architecture**:
  - **Master (`master`)**: Issues tasks, orders, directives, decisions, and cancellations.
  - **Agent (`agent`)**: Consumes orders, executes actions, and reports progress, results, and blockers.
- **Ultra-Reliable Storage**: SQLite in **WAL mode** (`journal_mode=WAL`, `busy_timeout=5000`) with indexed lookups on orders, roles, message types, and timestamps.
- **Safe Retries & Idempotency**: Resend messages with a `message_key` safely—identical content returns the existing record (`200 OK`) without duplication.
- **Real-Time Streaming**: Server-Sent Events (`GET /api/stream`) for instant push updates to agents and web dashboards with slow-subscriber protection.
- **Security**: Bearer token authentication for `master` and `agent` with sliding-window rate-limiting.
- **Web Dashboard**: Modern, responsive UI with real-time SSE updates, order filtering, and light/dark mode.

---

## Quick Start

### 1. Requirements
- Python >= 3.12
- Dependencies: `fastapi`, `uvicorn`, `aiosqlite`, `pydantic`

```bash
pip install -r requirements.txt
```

### 2. Configure Environment

Copy `.env.example` to `.env` and set secure tokens:

```bash
cp .env.example .env
# Or generate secure tokens directly:
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
uvicorn main:app --host 0.0.0.0 --port 8000
```
Or via Docker Compose:
```bash
docker compose up -d
```

Open `http://localhost:8000` in your browser to access the dashboard.

---

## How It Works

```
┌─────────────────┐                               ┌─────────────────┐
│     MASTER      │                               │      AGENT      │
│  (Orchestrator) │                               │    (Executor)   │
└────────┬────────┘                               └────────┬────────┘
         │                                                 │
         │  1. POST /api/messages (ORDER)                  │
         │  {"order_id": "ORD-1", "content": "..."}        │
         ├──────────────────────────┐                      │
         │                          ▼                      │
         │                ┌──────────────────┐             │
         │                │   Bridge Server  │             │
         │                │   (SQLite WAL)   │             │
         │                └─────────┬────────┘             │
         │                          │                      │
         │                          │ 2. SSE or Polling    │
         │                          └─────────────────────►│
         │                                                 │
         │                                                 │ 3. Execute
         │                                                 │    order...
         │  4. POST /api/messages (EXECUTION)              │
         │     {"order_id": "ORD-1", "status": "COMPLETED"}│
         │◄────────────────────────────────────────────────┤
```

---

## Using with Any Web Chat (Claude, ChatGPT, Gemini) or Agent

The Bridge is fully generic and designed so that **either or both** the Master and the Agent can be operated through any AI chat interface (Claude Web, ChatGPT Web, Gemini Web, DeepSeek, Grok) or programmatic runner.

### 1. The Human-in-the-Loop Copy/Paste Workflow
1. **Master (Architect)**:
   - Click **"🤖 AI Chat"** in the Bridge dashboard or grab `prompts/web_chat_master.md`.
   - Start a conversation in Claude, ChatGPT, or Gemini with that prompt.
   - When the AI generates an Order, paste it into the Bridge (or use the composer).
2. **Agent (Executor)**:
   - On any Order card in the Bridge dashboard, click **"📋 For Agent"**.
   - Paste the copied prompt into your Agent chat session (Claude, ChatGPT, Gemini).
   - Once the Agent responds, copy its reply and click **"⚡ Smart Paste"** in the Bridge composer.
   - The Bridge automatically extracts the `STATUS: ...` and `ORDER ID: ...` and fills the fields for you!
3. **Closing the Loop**:
   - On any Execution card in the Bridge dashboard, click **"📋 For Master"** to copy the update and paste it back to your Master chat for the next order.

### 2. Direct API Integration (Custom Actions / Browser Extensions / CLI)
CORS is enabled out of the box (`Access-Control-Allow-Origin: *` with credentials support), allowing web-based clients and browser extensions to call the Bridge directly:

- **Fetch System Prompts**:
  - `GET /api/prompts/master`: Returns universal Master system prompt and guidelines.
  - `GET /api/prompts/agent`: Returns universal Agent system prompt and execution formats.
- **Pending Orders**:
  - `GET /api/orders/pending`: Returns recent orders not yet marked as `COMPLETED`.
- **Post Orders**:
  - `POST /api/orders`: Master endpoint to publish an order (`order_id`, `content`, `status`).
- **Post Executions**:
  - `POST /api/executions`: Agent endpoint to report results (`order_id`, `content`, `status`).

---

## API Reference

### 1. Post an Order or Execution
`POST /api/messages`
- Header: `Authorization: Bearer <MASTER_TOKEN>` or `Bearer <AGENT_TOKEN>`
- Body:
```json
{
  "order_id": "ORDER-101",
  "message_type": "ORDER",
  "content": "## Run Migrations\n- apply migration 003\n- run sanity checks",
  "message_key": "master-order-101"
}
```

Agent reporting execution:
```json
{
  "order_id": "ORDER-101",
  "message_type": "EXECUTION",
  "status": "COMPLETED",
  "content": "All migrations applied successfully in 140ms.",
  "reply_to_id": 1,
  "payload": {
    "duration_ms": 140,
    "applied": ["003_add_indices.sql"]
  }
}
```

### 2. Query Messages
`GET /api/messages`
- Parameters:
  - `order_id`: Filter by order identity (exact match)
  - `role` or `role_filter`: Filter by `master` or `agent` (transparent alias support)
  - `message_type`: Filter by type (`ORDER`, `EXECUTION`, `STATUS`, `DECISION`, etc.)
  - `status`: Filter by execution status (`PENDING`, `RUNNING`, `COMPLETED`, `FAILED`, `BLOCKED`, `CANCELLED`)
  - `after_id`: Messages with `id > after_id` (for continuous polling)
  - `before_id`: History paging cursor
  - `latest=true`: Return most recent page
  - `limit`: Default 200, max 1000

### 3. Real-Time Stream (SSE)
`GET /api/stream`
- Receives instant push events whenever a new message is published.
- Browser clients obtain a short-lived ticket via `POST /api/stream-ticket`.

### 4. Who Am I
`GET /api/whoami`
- Returns authenticated role: `{"role": "master"}` or `{"role": "agent"}`.

### 5. Health & Status
- `GET /healthz`: Minimal liveness check (`{"status": "ok"}`).
- `GET /api/status`: Statistics (`total_messages`, `last_message_at`, `last_message_role`).

---

## Running the Agent Poller

The repository includes a ready-to-run polling adapter for automated agents (executable via `examples/worker_poll.py` or `examples/agent_worker.py`):

```bash
python examples/agent_worker.py \
  --bridge-url http://localhost:8000 \
  --token $AGENT_TOKEN \
  --run-mode daemon \
  --start-mode pending
```

Modes:
- `--run-mode daemon`: Continuously polls for new orders.
- `--run-mode once`: Checks for available orders once and exits.
- `--run-mode bounded`: Exits after N idle polls without work.

---

## Running Tests

Run the full test suite with pytest:

```bash
pytest
```
