# AgentBridge — Historical Audit & Improvements Log (v2.0)

> **ARCHIVE / HISTORICAL RECORD**
>
> This document preserves the original v2.0 audit and security improvements.
> All critical security items (DOMPurify HTML sanitization, secure token comparison,
> rate limiting, and mandatory authentication) were implemented and remain active in v3.0.0+.

---

## 🔴 Critical (Security)

### 1. Frontend XSS — Unsanitized Markdown
**Location:** `static/index.html:302`
```js
<div class="message-body">${marked.parse(msg.content)}</div>
```
`marked.parse()` does not sanitize HTML. Any message containing `<script>` or `<img onerror=...>` would execute in the browser of anyone viewing the board.

**Remediation:** add [DOMPurify](https://github.com/cure53/DOMPurify):
```js
const clean = DOMPurify.sanitize(marked.parse(msg.content));
```

### 2. Hardcoded Default Tokens Silently Accepted
**Location:** `main.py:19-20`
```python
ENGINEER_TOKEN = os.getenv("ENGINEER_TOKEN", "engineer_secret")
WORKER_TOKEN = os.getenv("WORKER_TOKEN", "worker_secret")
```
If environment variables were omitted, anyone reading the README would gain full access.

**Remediation:** fail startup (or log a severe warning) if tokens are not explicitly defined:
```python
ENGINEER_TOKEN = os.getenv("ENGINEER_TOKEN")
WORKER_TOKEN = os.getenv("WORKER_TOKEN")
if not ENGINEER_TOKEN or not WORKER_TOKEN:
    raise RuntimeError("Define MASTER_TOKEN and AGENT_TOKEN in environment")
```

### 3. Token Comparison Vulnerable to Timing Attack
**Location:** `main.py:92-95`
```python
if msg.token == ENGINEER_TOKEN:
```
**Remediation:** use constant-time comparison:
```python
import secrets
if secrets.compare_digest(msg.token, ENGINEER_TOKEN):
```

### 4. Unauthenticated API Reading
**Location:** `main.py:71-86`
`GET /api/messages` was public — anyone could inspect the entire conversation between Master and Agent (which might contain sensitive project data).

**Remediation:** require a valid token via header (`Authorization: Bearer <token>` or query param) to read messages.

### 5. Lack of Rate Limiting
**Location:** `POST /api/messages`
A malfunctioning agent loop (or attacker) could flood the database indefinitely.

**Remediation:** apply in-memory sliding-window rate limiting per role/token (e.g., N messages/minute).

---

## 🟠 Important (Bugs & Robustness)

### 6. Float Timestamp Sorting/Filtering (`since`) — Message Loss Risk
**Location:** `main.py:76-84` and `static/index.html:313`
`time.time()` has limited resolution and clocks may drift. Two messages created concurrently or where `created_at == since` could be missed by `created_at > ?`.

**Remediation:** pagination based on auto-incrementing `id`:
```
GET /api/messages?after_id=42
SELECT ... WHERE id > ? ORDER BY id ASC
```
Retain `since` as a deprecated alias if needed, but migrate clients to `after_id`.

### 7. No Pagination/Limit on `GET /api/messages`
Over time the board grows unbounded, causing the initial load to become sluggish.

**Remediation:** add `limit` (default ~200) + DESC sorting on initial load, plus `before_id` for backwards scrolling.

### 8. Per-request SQLite Connection + Missing WAL
**Location:** `main.py:74, 105, 119`
Concurrent writes could result in `database is locked`. Reopening SQLite connections per request is also inefficient.

**Remediation:**
- Enable WAL mode in `init_db()`: `PRAGMA journal_mode=WAL;`
- Set `busy_timeout=5000` to mitigate contention.

### 9. `reload=True` in Production Entrypoint
**Location:** `main.py:148`
```python
uvicorn.run("main:app", host=HOST, port=PORT, reload=True)
```
Running `python main.py` in production would enable hot-reloading (unnecessary overhead and security risk).

**Remediation:** govern reload via environment variable:
```python
uvicorn.run("main:app", host=HOST, port=PORT, reload=os.getenv("RELOAD") == "1")
```

### 10. Dockerfile Ignores `HOST`/`PORT` and Runs as Root
**Location:** `Dockerfile:14`
```dockerfile
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
```
Documented environment variables `HOST`/`PORT` were ignored, and the container process executed as root.

**Remediation:**
```dockerfile
RUN useradd -m appuser && chown -R appuser /app
USER appuser
CMD ["sh", "-c", "uvicorn main:app --host ${HOST:-0.0.0.0} --port ${PORT:-8000}"]
```
Also add a container `HEALTHCHECK` targeting `/api/status` or `/healthz`.

### 11. CWD-relative Paths
**Location:** `main.py:18, 139, 143`
`DB_PATH = "data/bridge.db"` and `FileResponse("static/index.html")` break if the process starts outside the project root directory.

**Remediation:** resolve paths relative to `__file__`:
```python
BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv("BRIDGE_DB", BASE_DIR / "data" / "bridge.db"))
STATIC_DIR = BASE_DIR / "static"
```

### 12. Unused Dependency
**Location:** `requirements.txt:5`
`python-multipart` is only needed for multipart form uploads; no endpoints require it.

**Remediation:** remove or document its purpose.

### 13. CDN Without Integrity Hash (Supply Chain Risk)
**Location:** `static/index.html:7`
```html
<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
```
No `integrity`/SRI hash and no pinned version. If the CDN were compromised, arbitrary JavaScript could execute on the dashboard.

**Remediation:** pin version and include SRI hash, or vendor assets locally.

### 14. `cursor.lastrowid` May Be `None`
**Location:** `main.py:111`
`aiosqlite` typing defines `lastrowid` as `Optional[int]`. Passing it directly to `MessageOut(id=msg_id)` works at runtime, but an explicit check guarantees type safety.

### 15. Missing Global Error Handling and Logging
Absence of structured logging for requests, database errors, and invalid authentication attempts.

**Remediation:** configure standard `logging`, log failed token attempts (helpful for diagnosing misconfigured agents), and handle unhandled exceptions.

---

## 🟡 Improvements (UX & Quality)

### 16. Frontend: Role Selector is Misleading
**Location:** `static/index.html:260-263`
The `<select>` Master/Agent dropdown suggested the user could pick their role, but the true role is determined by the server-validated **token**. Users entering a Master token while selecting Agent would be confused.

**Remediation:** dynamically detect role via `GET /api/whoami` after validating the token, or lock the selector to match the verified token.

### 17. Frontend: Forced Auto-scroll on Every Poll
**Location:** `static/index.html:308`
`window.scrollTo(...)` triggered on every received message, interrupting reading when the user scrolled up.

**Remediation:** only auto-scroll if the user is already near the bottom of the page (~100px).

### 18. Frontend: Hint Exposes Default Tokens in UI
**Location:** `static/index.html:268`
In production, showing default tokens on screen undermines any secret token configured via environment variables.

**Remediation:** remove hardcoded token hints and guide users to provide tokens securely.

### 19. Frontend: Fixed 8s Polling + Duplicate Status Request
Each cycle issued 2 requests (`/api/messages` and `/api/status`).

**Remediation:** implement **Server-Sent Events (SSE)** for real-time push notification, eliminating unnecessary polling overhead.

### 20. `examples/agent_worker.py`: State Lost on Restart and Status Spam
- Initializing `last_seen = 0` on restart reprocessed all historical messages as "new".
- Posting placeholder status messages on every instruction polluted the timeline.

**Remediation:** persist `last_seen` in state storage and make status updates informative and intentional.

### 21. No Automated Tests
Zero test coverage initially.

**Remediation:** add automated test suite covering authentication, order dispatch, execution reporting, pagination, and validation.

### 22. Missing CI
Add GitHub Actions running linter (`ruff`) and test suites on every pull request.

### 23. High-Value Capabilities
- **DELETE /api/messages/{id}** (restricted to author role) to allow correcting mistaken posts.
- **Order & Execution Tracking**: dedicated tracking of `order_id` and execution statuses (`PENDING`, `RUNNING`, `COMPLETED`, `FAILED`, `BLOCKED`).
- **Real-Time Push**: SSE stream (`/api/stream`) for low-latency updates.
- **SQLite WAL & Backups**: WAL mode enabled with safe backup instructions.
- **Docker Compose**: quick startup with `docker-compose.yml`.

### 24. Documentation
- Comprehensive README with setup, architecture, and API reference.
- Clear production deployment and security guidelines (`openssl rand -hex 32`, reverse proxy, HTTPS).

---

## Prioritized Summary

| # | Item | Severity | Effort |
|---|------|----------|--------|
| 1 | Sanitize Markdown (XSS) | 🔴 Critical | Low |
| 2 | Mandatory Environment Tokens | 🔴 Critical | Low |
| 4 | Authenticate API Reads | 🔴 Critical | Medium |
| 5 | Rate Limiting | 🔴 High | Low |
| 6 | ID-Based Pagination | 🟠 High | Medium |
| 8 | WAL + Connection Tuning | 🟠 Medium | Low |
| 9-10 | Reload / Docker Hardening | 🟠 Medium | Low |
| 11 | Absolute Path Resolution | 🟠 Medium | Low |
| 13 | SRI / Local Asset Vendoring | 🟠 Medium | Low |
| 16-20 | Frontend UX + Agent Examples | 🟡 Medium | Medium |
| 21-22 | Tests + CI Pipeline | 🟡 Medium | Medium |
| 19 | SSE Real-Time Stream | 🟡 Low | High |
