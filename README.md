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
- Minimal REST API for agents
- Auto-refresh every 8 seconds
- Separate tokens for Engineer and Worker
- SQLite (zero extra configuration)
- First-class integration with ForgeLoop

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

### Environment variables (optional)

| Variable          | Default               | Description                  |
|-------------------|-----------------------|------------------------------|
| `ENGINEER_TOKEN`  | `engineer_secret`     | Engineer token               |
| `WORKER_TOKEN`    | `worker_secret`       | Worker token                 |
| `FORGEBRIDGE_DB`  | `data/forgebridge.db` | SQLite path                  |
| `HOST`            | `0.0.0.0`             | Host                         |
| `PORT`            | `8000`                | Port                         |

---

## Prompt templates (with ForgeLoop)

Copy-paste these prompts to bootstrap both agents.

### Engineer system prompt

```
You are the Engineer.

Your only communication channel with the Worker is ForgeLoopBridge.
Board URL: http://localhost:8000
Engineer token: engineer_secret

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
Worker token: worker_secret

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

### `GET /api/messages?since=<unix_timestamp>`
Returns all messages (or only new ones after `since`).

### `POST /api/messages`
```json
{
  "token": "engineer_secret",
  "content": "## Task 1\n- Do X\n- Follow ForgeLoop and open a PR when finished"
}
```

### `GET /api/status`
Health check + last activity.

---

## How the Worker monitors (Python example)

```python
import time
import requests

BASE = "http://localhost:8000"
TOKEN = "worker_secret"
last = 0

while True:
    r = requests.get(f"{BASE}/api/messages", params={"since": last})
    msgs = r.json()
    for m in msgs:
        if m["role"] == "engineer":
            print("New instruction:", m["content"])
            # → run ForgeLoop protocol, open PR, etc.
            # then post status:
            requests.post(f"{BASE}/api/messages", json={
                "token": TOKEN,
                "content": "### Status\nPR opened: ...\ncomplete: VALID"
            })
        last = max(last, m["created_at"])
    time.sleep(10)
```

A ready-to-use script is available at `examples/worker_poll.py`.

---

## Quick deploy

Anywhere that runs Python:

- Railway
- Render
- Fly.io
- Simple VPS

Docker example (optional):

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
```

---

## License

MIT © 2026 Cassio Marques Campos
