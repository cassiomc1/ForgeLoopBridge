import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path

os.environ.setdefault("MASTER_TOKEN", "test_master_token_1234567890abcdef")
os.environ.setdefault("AGENT_TOKEN", "test_agent_token_0987654321fedcba")
_tmpdb = tempfile.mkdtemp()
os.environ["BRIDGE_DB"] = str(Path(_tmpdb) / "test.db")

import httpx  # noqa: E402
import pytest  # noqa: E402

import main  # noqa: E402
import self_improvement  # noqa: E402
from main import AGENT_TOKEN, MASTER_TOKEN, app  # noqa: E402

HEADERS_MASTER = {"Authorization": f"Bearer {MASTER_TOKEN}"}
HEADERS_AGENT = {"Authorization": f"Bearer {AGENT_TOKEN}"}


@pytest.fixture(autouse=True)
async def clean_db():
    await main.init_db()
    async with main.connect_db() as db:
        await db.execute("DELETE FROM messages")
    yield


@pytest.fixture
def temp_improvements_dir(monkeypatch):
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        monkeypatch.setenv("IMPROVEMENTS_DIR", str(tmp_path))
        monkeypatch.setattr(self_improvement, "DEFAULT_IMPROVEMENTS_DIR", str(tmp_path))
        monkeypatch.setattr(main, "IMPROVEMENTS_DIR", tmp_path)
        yield tmp_path



@pytest.fixture
async def client():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def test_sanitize_task_name():
    assert self_improvement.sanitize_task_name("ORDER-101") == "order-101"
    assert self_improvement.sanitize_task_name("Fix Auth Bug!! (urgent)") == "fix-auth-bug-urgent"
    assert self_improvement.sanitize_task_name("") == "general-task"
    assert self_improvement.sanitize_task_name(None) == "general-task"
    assert self_improvement.sanitize_task_name("---") == "general-task"


def test_generate_improvement_filename():
    dt = datetime(2026, 9, 20, 11, 5, 30, tzinfo=UTC)
    filename = self_improvement.generate_improvement_filename("ORDER-101", dt)
    assert filename == "order-101-2026-09-20-11-05-30.md"


def test_create_and_list_improvement_record(temp_improvements_dir):
    dt = datetime(2026, 9, 20, 12, 0, 0, tzinfo=UTC)
    path = self_improvement.create_improvement_record(
        task_name="order-auth-refactor",
        role="agent",
        status="COMPLETED",
        summary="Refactored token checks to prevent timing attacks.",
        improvements=["Add property-based testing for auth tokens."],
        issues=["Token comparison lacked salt in older mocks."],
        action_items=["Apply secure token comparison globally."],
        target_dir=temp_improvements_dir,
        timestamp=dt,
    )

    assert path.exists()
    assert path.name == "order-auth-refactor-2026-09-20-12-00-00.md"

    content = path.read_text(encoding="utf-8")
    assert "# Self-Improvement Record: order-auth-refactor" in content
    assert "## 1. Executive Summary" in content
    assert "Refactored token checks to prevent timing attacks." in content
    assert "## 2. Friction Points & Identified Issues" in content
    assert "Token comparison lacked salt in older mocks." in content
    assert "## 3. Recommended System Improvements" in content
    assert "Add property-based testing for auth tokens." in content
    assert "## 4. Concrete Action Items & Next Steps" in content
    assert "Apply secure token comparison globally." in content

    # Listing records
    records = self_improvement.list_improvement_records(target_dir=temp_improvements_dir)
    assert len(records) == 1
    assert records[0]["filename"] == path.name


def test_extract_improvement_sections_from_markdown():
    raw_markdown = """
STATUS: COMPLETED
ORDER ID: ORD-55

SUMMARY:
Implemented database migrations.

SELF-IMPROVEMENT / SYSTEM SUGGESTIONS:
- Cache schema introspection queries
- Increase default connection pool timeout

BLOCKERS / FRICTION:
- SQLite lock contention on concurrent writes

ACTION ITEMS:
- Configure WAL mode busy_timeout to 10000ms
"""
    extracted = self_improvement.extract_improvement_sections(raw_markdown)
    assert len(extracted["improvements"]) == 2
    assert "Cache schema introspection queries" in extracted["improvements"]
    assert "Increase default connection pool timeout" in extracted["improvements"]
    assert len(extracted["issues"]) == 1
    assert "SQLite lock contention on concurrent writes" in extracted["issues"]
    assert len(extracted["action_items"]) == 1
    assert "Configure WAL mode busy_timeout to 10000ms" in extracted["action_items"]


def test_read_improvement_record_and_traversal_guard(temp_improvements_dir):
    path = self_improvement.create_improvement_record(
        task_name="task-safe",
        target_dir=temp_improvements_dir,
    )

    # Valid read
    content = self_improvement.read_improvement_record(path.name, target_dir=temp_improvements_dir)
    assert "Self-Improvement Record" in content

    # Non-existent
    with pytest.raises(FileNotFoundError):
        self_improvement.read_improvement_record("non-existent-file.md", target_dir=temp_improvements_dir)

    # Path traversal attack attempt
    with pytest.raises(FileNotFoundError):
        self_improvement.read_improvement_record("../../etc/passwd", target_dir=temp_improvements_dir)


async def test_api_improvements_lifecycle(client, temp_improvements_dir):
    # 1. Post improvement via API as Master
    res = await client.post(
        "/api/improvements",
        json={
            "task_name": "API-OPTIMIZATION",
            "summary": "Optimized pagination cursors.",
            "improvements": ["Add ETags for immutable message history."],
            "issues": ["Large offset queries degraded query latency."],
            "action_items": ["Implement key-based cursor queries."],
        },
        headers=HEADERS_MASTER,
    )
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "ok"
    filename = data["filename"]
    assert filename.startswith("api-optimization-")
    assert filename.endswith(".md")

    # 2. List improvements
    list_res = await client.get("/api/improvements", headers=HEADERS_AGENT)
    assert list_res.status_code == 200
    records = list_res.json()
    assert len(records) >= 1
    assert any(r["filename"] == filename for r in records)

    # 3. Read specific improvement
    get_res = await client.get(f"/api/improvements/{filename}", headers=HEADERS_MASTER)
    assert get_res.status_code == 200
    file_data = get_res.json()
    assert file_data["filename"] == filename
    assert "Optimized pagination cursors." in file_data["content"]


async def test_post_execution_automatically_creates_improvement_record(client, temp_improvements_dir):
    # Post execution as Agent
    res = await client.post(
        "/api/executions",
        json={
            "order_id": "ORD-AUTO-01",
            "content": """
STATUS: COMPLETED
ORDER ID: ORD-AUTO-01

SUMMARY:
Completed payment integration.

IMPROVEMENTS:
- Add webhook signature validation helper

FRICTION POINTS:
- Mock payment gateway had intermittent connection drops
""",
            "status": "COMPLETED",
        },
        headers=HEADERS_AGENT,
    )
    assert res.status_code == 200

    # Verify improvement file was auto-generated
    files = list(temp_improvements_dir.glob("ord-auto-01-*.md"))
    assert len(files) == 1
    content = files[0].read_text(encoding="utf-8")
    assert "Add webhook signature validation helper" in content
    assert "Mock payment gateway had intermittent connection drops" in content


def test_worker_poll_execute_order_creates_improvement_record(monkeypatch, temp_improvements_dir):
    from examples import worker_poll

    posted = []

    def fake_post_report(**kwargs):
        posted.append(kwargs)

    monkeypatch.setattr(worker_poll, "post_execution_report", fake_post_report)
    monkeypatch.setattr(worker_poll.time, "sleep", lambda s: None)

    msg = {
        "id": 42,
        "order_id": "ORD-WORKER-TEST",
        "content": "Deploy cache warming script.",
        "role": "master",
    }
    worker_poll.execute_order(msg, "http://test", headers={"Authorization": "Bearer test"})

    assert len(posted) == 1
    assert posted[0]["status"] == "BLOCKED"
    files = list(temp_improvements_dir.glob("ord-worker-test-*.md"))
    assert len(files) == 1
    content = files[0].read_text(encoding="utf-8")
    assert "Self-Improvement Record: ORD-WORKER-TEST" in content
    assert "Maintain continuous automated test verifications" in content


def test_records_created_in_same_second_preserve_each_execution(temp_improvements_dir):
    dt = datetime(2026, 10, 14, 12, 0, 0, tzinfo=UTC)
    paths = [
        self_improvement.create_improvement_record(
            "same-task", summary=f"Execution {i}", timestamp=dt, target_dir=temp_improvements_dir,
        )
        for i in range(3)
    ]
    assert len(set(paths)) == 3
    assert all(f"Execution {i}" in path.read_text() for i, path in enumerate(paths))


def test_concurrent_improvement_records_do_not_overwrite(temp_improvements_dir):
    from concurrent.futures import ThreadPoolExecutor

    dt = datetime(2026, 10, 14, 12, 0, 0, tzinfo=UTC)
    def create(index):
        return self_improvement.create_improvement_record(
            "same-task", summary=f"Execution {index}", timestamp=dt, target_dir=temp_improvements_dir,
        )
    with ThreadPoolExecutor(max_workers=8) as pool:
        paths = list(pool.map(create, range(16)))
    assert len(set(paths)) == 16
    assert all(f"Execution {i}" in path.read_text() for i, path in enumerate(paths))


def test_improvement_bullets_are_not_mistaken_for_headings():
    extracted = self_improvement.extract_improvement_sections(
        "SELF-IMPROVEMENT:\n- Proposed architecture improvements\n"
        "- Reduce blockers with better diagnostics\nACTION ITEMS:\n- Review next steps weekly\n"
    )
    assert extracted["improvements"] == [
        "Proposed architecture improvements", "Reduce blockers with better diagnostics",
    ]
    assert extracted["action_items"] == ["Review next steps weekly"]


def test_improvement_listing_does_not_disclose_symlink_contents(temp_improvements_dir, tmp_path):
    secret = tmp_path / "outside.md"
    secret.write_text("Private file outside the improvements directory")
    link = temp_improvements_dir / "linked-record.md"
    try:
        link.symlink_to(secret)
    except OSError:
        pytest.skip("Symlinks unavailable on this platform")
    assert self_improvement.list_improvement_records(temp_improvements_dir) == []


async def test_execution_retry_does_not_duplicate_or_rewrite_improvement(client, temp_improvements_dir):
    body = {"order_id": "RETRY-LOG", "content": "Done", "message_key": "retry-log-key"}
    first = await client.post("/api/executions", json=body, headers=HEADERS_AGENT)
    assert first.status_code == 200
    files = list(temp_improvements_dir.glob("*.md"))
    assert len(files) == 1
    original = files[0].read_bytes()
    retry = await client.post(
        "/api/executions", json=body | {"improvements": ["Unexpected rewrite"]}, headers=HEADERS_AGENT,
    )
    assert retry.status_code == 200
    assert retry.json() == first.json()
    assert list(temp_improvements_dir.glob("*.md")) == files
    assert files[0].read_bytes() == original

