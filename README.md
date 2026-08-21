<p align="center">
  <img src="assets/banner.webp" alt="ForgeLoopBridge" width="800">
</p>

<p align="center">
  <strong>Minimalist Markdown communication board between Engineer and Worker agents</strong><br>
  Built to work with <a href="https://github.com/cassiomc1/ForgeLoop">ForgeLoop</a>
</p>

---

# ForgeLoopBridge

**Minimalist Markdown communication board between two agents: Engineer and Worker.**

Designed to work together with [ForgeLoop](https://github.com/cassiomc1/ForgeLoop) — a portable engineering protocol for AI coding agents.

- **Engineer** (e.g. Grok) → defines intent, acceptance criteria and reviews PRs
- **Worker** (e.g. OpenCode / Cursor / local agent) → executes tasks following the full ForgeLoop protocol, opens PRs and reports status

The real code and all ForgeLoop artifacts (`.forgeloop/`) always live in the **project repository**.  
ForgeLoopBridge only carries the high-level conversation (instructions + status + PR links).

---

## Features

- Extremely simple (single backend + single page)
- 100% Markdown communication
- Minimal REST API for agents + real-time SSE stream
- Separate tokens for Engineer and Worker
- **Security hardening**: required tokens, timing-safe comparison, authenticated reads, rate limiting, XSS-safe Markdown rendering (DOMPurify)
- SQLite with WAL mode (zero extra configuration)
- Message pagination (`after_id` / `before_id` / `limit`), delete by author, `/api/whoami`
- First-class integration with ForgeLoop
- Test suite (pytest) and CI (GitHub Actions: ruff + pytest)

---

## Architecture

```
┌─────────────────┐         ForgeLoopBridge          ┌─────────────────┐
│    Engineer     │◄─────── Markdown board ──────►│     Worker      │
│  (Grok / LLM)   │      (instructions + status)  │ (OpenCode etc.) │
└────────┬────────┘                               └────────┬────────┘
         │                                                 │
         │ reviews PR                                      │ executes
         │                                                 │
         ▼                                                 ▼
┌──────────────────────────────────────────────────────────────────┐
│                     Project Repository                           │
│  • Source code                                                   │
│  • .forgeloop/  (contracts, evidence, state, verification)       │
│  • Pull Requests                                                 │
└──────────────────────────────────────────────────────────────────┘
```

---

## Run locally

```bash
# 1. Clone
git clone https://github.com/cassiomc1/ForgeLoopBridge.git
cd ForgeLoopBridge

# 2. Install dependencies
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 3. Run
python main.py
```

Open: [http://localhost:8000](http://localhost:8000)

### Environment variables

| Variable            | Default               | Description                                        |
|---------------------|-----------------------|----------------------------------------------------|
| `ENGINEER_TOKEN`    | — (**required**)      | Engineer token                                     |
| `WORKER_TOKEN`      | — (**required**)      | Worker token                                       |
| `FORGEBRIDGE_DB`    | `data/forgebridge.db` | SQLite path                                        |
| `HOST`              | `0.0.0.0`             | Host                                               |
| `PORT`              | `8000`                | Port                                               |
| `RELOAD`            | `0`                   | Hot reload (dev only)                              |
| `RATE_LIMIT_POSTS`  | `30`                  | Max posts per window per role                      |
| `RATE_LIMIT_WINDOW` | `60`                  | Rate limit window in seconds                       |
| `DEFAULT_PAGE_SIZE` | `200`                 | Default page size for `GET /api/messages`          |
| `LOG_LEVEL`         | `INFO`                | Logging level                                      |

Generate strong tokens with:

```bash
openssl rand -hex 32
```

The server refuses to start without both tokens set.

---

## Security model

- **Tokens are mandatory** — the server will not boot with default secrets.
- **All message endpoints require authentication** via `Authorization: Bearer <token>` header (or `?token=` query param for agents/SSE).
- Token comparisons are **timing-safe** (`secrets.compare_digest`).
- **Rate limiting** on posting (default 30 posts/minute per role).
- **Markdown is sanitized** in the browser with DOMPurify; JS dependencies are vendored locally under `static/vendor/`.
- Only the author role can **delete** its own messages.
- `/api/status` is public but exposes only counters — never message contents.

For production deployments, put the bridge behind a reverse proxy with HTTPS
(Caddy, Traefik, nginx) and never expose it directly to the internet without TLS.

---

## Prompt templates (with ForgeLoop)

Copy-paste these prompts to bootstrap both agents.

### Engineer system prompt

```
You are the Engineer.

Your only communication channel with the Worker is ForgeLoopBridge.
Board URL: http://localhost:8000
Engineer token: <your ENGINEER_TOKEN>

The Worker is required to follow the ForgeLoop protocol
(https://github.com/cassiomc1/ForgeLoop) for every task.

When you post a task, always include:
- Clear goal
- Acceptance criteria
- Preferred ForgeLoop work type (if known)
- Explicit instruction: "Follow ForgeLoop. Create a task, write a proper contract, reach VALID completion, then open a PR."

You never execute code or run ForgeLoop yourself.
You only review the resulting PR (especially the .forgeloop artifacts and the complete result).

After the Worker posts a PR, inspect:
1. Does forgeloop complete return VALID?
2. Is the evidence sufficient?
3. Does the code match the contract?

Then either approve + next task, or request precise changes.
```

**Example first message the Engineer should post on the board:**

```markdown
## Task 1 – Bootstrap TypeScript service

Goal: Create a minimal, production-ready Node.js + TypeScript service skeleton.

Acceptance criteria:
- package.json with scripts: dev, build, test, lint
- Modern tsconfig
- src/index.ts with a health endpoint
- At least one passing test
- forgeloop complete must return VALID

Follow the full ForgeLoop protocol.
Create a task, write a proper contract, reach VALID completion, then open a PR and post the link + complete result here.
```

### Worker system prompt

```
You are the Worker.

Your only communication channel with the Engineer is ForgeLoopBridge.
Board URL: http://localhost:8000
Worker token: <your WORKER_TOKEN>

You MUST execute every task using the ForgeLoop protocol
(https://github.com/cassiomc1/ForgeLoop).

Mandatory workflow for every new instruction from the Engineer:

1. Discover existing tasks first:
   forgeloop task-list --json

2. Create or resume a ForgeLoop task:
   forgeloop task-create --task <short-key> --claim <paths> --json

3. Write a proper contract.json that reflects the Engineer's request.

4. Route, preflight, implement, verify with evidence:
   forgeloop route ...
   forgeloop preflight ...   # must be READY
   ... implement ...
   forgeloop run-check ...
   forgeloop complete ...    # must return VALID

5. Open a Pull Request that includes both the code changes AND the .forgeloop artifacts.

6. Immediately post on ForgeLoopBridge:

### Status – Task X
Done.

**PR:** https://github.com/.../pull/XX

**ForgeLoop:**
- task: <taskKey>
- complete: VALID
- evidence: <short summary>

Never invent results. Only post after `forgeloop complete` returns VALID and the PR exists.
If you cannot reach VALID, post BLOCKED or PARTIALLY VERIFIED with the reason and wait for new instructions.
```

---

## Recommended workflow

1. **Engineer** posts a clear task on the ForgeLoopBridge board.
2. **Worker** detects the message, creates/resumes a ForgeLoop task in the project repo.
3. Worker follows the full protocol until `forgeloop complete` returns `VALID`.
4. Worker opens a PR containing code + `.forgeloop/` artifacts.
5. Worker posts status + PR link + complete result on ForgeLoopBridge.
6. **Engineer** reviews the PR (especially verification evidence) and posts feedback or the next task.
7. Repeat.

---

## API

All message endpoints require a valid token via `Authorization: Bearer <token>`
header or `?token=` query parameter. The role (engineer/worker) is derived from the token.

### `GET /api/messages?after_id=<id>&before_id=<id>&limit=<n>`
Returns up to `limit` messages ordered by id ASC (default 200, max 1000).
Use `after_id` for live updates and `before_id` for history paging.

### `POST /api/messages`
```json
{ "content": "## Task 1\n- Do X\n- Follow ForgeLoop and open a PR when finished" }
```
The token may go in the `Authorization` header (preferred) or in the body as `"token"`.
Rate limited (default 30/min).

### `DELETE /api/messages/{id}`
Deletes a message. Only the author role can delete it.

### `GET /api/whoami`
Returns the role bound to the token: `{ "role": "engineer" }`.

### `GET /api/stream?token=...`
Server-Sent Events stream of new messages in real time, with keepalives.
The web board uses this automatically and falls back to 8s polling.

### `GET /api/status`
Public health check + counters (no message contents).

---

## How the Worker monitors (Python example)

```python
import time
import requests

BASE = "http://localhost:8000"
TOKEN = "your-worker-token"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}
last_id = 0

while True:
    r = requests.get(f"{BASE}/api/messages", params={"after_id": last_id}, headers=HEADERS)
    for m in r.json():
        if m["role"] == "engineer":
            print("New instruction:", m["content"])
            # → run ForgeLoop protocol, open PR, etc.
            # then post status:
            requests.post(f"{BASE}/api/messages", headers=HEADERS,
                          json={"content": "### Status\nPR opened: ...\ncomplete: VALID"})
        last_id = max(last_id, m["id"])
    time.sleep(10)
```

A ready-to-use script is available at `examples/worker_poll.py` (it persists its
cursor across restarts; use `--auto-ack` to post immediate acknowledgements).

---

## Quick deploy

Anywhere that runs Python:

- Railway
- Render
- Fly.io
- Simple VPS

### Docker Compose (recommended)

```bash
cp .env.example .env
# edit .env and set strong tokens: openssl rand -hex 32
docker compose up -d --build
```

Or plain Docker:

```bash
docker build -t forgebridge .
docker run -d -p 8000:8000 \
  -e ENGINEER_TOKEN=$(openssl rand -hex 32) \
  -e WORKER_TOKEN=$(openssl rand -hex 32) \
  forgebridge
```

The container runs as a non-root user, honors `HOST`/`PORT`, and includes a
health check on `/api/status`.

---

## Development

```bash
pip install -r requirements-dev.txt

ruff check .      # lint
pytest            # tests (uses a temporary database)
```

CI runs lint + tests on every push/PR via GitHub Actions.

---

## License

MIT © 2026 Cassio Marques Campos
