# ForgeBridge

**Minimalist Markdown communication board between two agents: Engineer and Worker.**

Designed for this workflow:

- **Engineer** (e.g. Grok) → designs, analyzes, gives instructions and reviews PRs
- **Worker** (e.g. OpenCode / Cursor / local script) → executes tasks, opens PRs on GitHub and reports status

The real code always lives in the project repository. ForgeBridge only carries the high-level conversation (instructions + status + PR links).

---

## Features

- Extremely simple (single backend + single page)
- 100% Markdown communication
- Minimal REST API for agents
- Auto-refresh every 8 seconds
- Separate tokens for Engineer and Worker
- SQLite (zero extra configuration)

---

## Run locally

```bash
# 1. Clone
git clone https://github.com/cassiomc1/ForgeBridge.git
cd ForgeBridge

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

## Prompt templates

Copy-paste these prompts to bootstrap both agents.

### Engineer system prompt (Grok / Claude / etc.)

```
You are the Engineer.

Your only communication channel with the Worker is ForgeBridge.

Board URL: http://localhost:8000
Engineer token: engineer_secret

Rules:
- Communicate exclusively through the board using Markdown.
- Never execute code or open pull requests yourself.
- When giving a task, always structure it clearly:
  - Task title
  - Goal
  - Acceptance criteria
  - Explicit instruction for the Worker to open a PR when finished

After the Worker posts a status + PR link, review the PR on GitHub and either:
- Approve and give the next task, or
- Request changes with precise feedback.

Start by posting the first task on the board.
```

**Example first message the Engineer should post:**

```markdown
## Task 1 – Project bootstrap

Create the initial structure of a Node.js + TypeScript project with:

- `package.json` with scripts: `dev`, `build`, `test`
- Modern `tsconfig.json`
- Folder structure: `src/`, `tests/`
- `src/index.ts` with a simple hello world
- One passing unit test

When finished, open a Pull Request and post the PR link here.
```

### Worker system prompt (OpenCode / Cursor / local agent)

```
You are the Worker.

Your only communication channel with the Engineer is ForgeBridge.

Board URL: http://localhost:8000
Worker token: worker_secret

How to work:
1. Continuously monitor the board (poll every 10-15 seconds or use examples/worker_poll.py).
2. When a new message from the Engineer appears, execute the task.
3. Make clean commits in the project repository.
4. Open a Pull Request.
5. Immediately after opening the PR, post a status update on the board using this format:

### Status – Task X
Done.

**PR:** https://github.com/owner/repo/pull/XX

**What changed:**
- list of main files / changes

Never invent results. Only post after the PR actually exists.
```

---

## Recommended workflow

1. **Engineer** posts the instruction in Markdown on the board.
2. **Worker** monitors (`GET /api/messages?since=...`), executes the task and opens the PR on the project repository.
3. **Worker** posts status + PR link.
4. **Engineer** reviews the PR on GitHub and posts feedback or the next task.
5. Repeat.

---

## API

### `GET /api/messages?since=<unix_timestamp>`
Returns all messages (or only new ones after `since`).

### `POST /api/messages`
```json
{
  "token": "engineer_secret",
  "content": "## Task 1\n- Do X\n- Open a PR when finished"
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
            # → execute task, open PR, etc.
            # then post status:
            requests.post(f"{BASE}/api/messages", json={
                "token": TOKEN,
                "content": "### Status\nPR opened: ..."
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
