import pytest

from examples import worker_poll


@pytest.fixture(autouse=True)
def isolate_improvements(monkeypatch, tmp_path):
    monkeypatch.setenv("IMPROVEMENTS_DIR", str(tmp_path / "improvements"))



def test_get_auth_headers_raises_when_no_token(monkeypatch):
    monkeypatch.setattr(worker_poll, "AGENT_TOKEN", "")
    with pytest.raises(RuntimeError) as excinfo:
        worker_poll.get_auth_headers(None)
    assert "AGENT_TOKEN" in str(excinfo.value)


def test_get_auth_headers_with_explicit_token():
    headers = worker_poll.get_auth_headers("my-secret-token")
    assert headers["Authorization"] == "Bearer my-secret-token"
    assert headers["Content-Type"] == "application/json"


def test_fetch_latest_message_id_returns_id(monkeypatch):
    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return [{"id": 10}, {"id": 42}]

    def fake_get(url, *, params, headers, timeout):
        assert params == {"latest": "true", "limit": 1}
        return FakeResponse()

    monkeypatch.setattr(worker_poll.requests, "get", fake_get)
    assert worker_poll.fetch_latest_message_id(headers={"Authorization": "Bearer test"}) == 42


def test_fetch_latest_message_id_returns_zero_on_empty_board(monkeypatch):
    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return []

    monkeypatch.setattr(worker_poll.requests, "get", lambda *a, **kw: FakeResponse())
    assert worker_poll.fetch_latest_message_id(headers={"Authorization": "Bearer test"}) == 0


def test_fetch_messages_passes_after_id(monkeypatch):
    captured = {}

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return [{"id": 5, "content": "hi"}]

    def fake_get(url, *, params, headers, timeout):
        captured["params"] = params
        return FakeResponse()

    monkeypatch.setattr(worker_poll.requests, "get", fake_get)
    msgs = worker_poll.fetch_messages(4, headers={"Authorization": "Bearer test"})
    assert len(msgs) == 1
    assert captured["params"]["after_id"] == 4


def test_is_master_order():
    assert worker_poll.is_master_order({"role": "master", "message_type": "ORDER"}) is True
    assert worker_poll.is_master_order({"role": "engineer", "order_id": "ORD-1"}) is True
    assert worker_poll.is_master_order({"role": "MASTER", "message_type": "TASK"}) is True
    assert worker_poll.is_master_order({"role": "master", "status": "PENDING", "order_id": "ORD-1"}) is True
    assert worker_poll.is_master_order({"role": "master"}) is False
    assert worker_poll.is_master_order({"role": "master", "status": "COMPLETED"}) is False
    assert worker_poll.is_master_order({"role": "master", "status": "CANCELLED"}) is False
    assert worker_poll.is_master_order({"role": "agent"}) is False
    assert worker_poll.is_master_order({"role": "worker"}) is False
    assert worker_poll.is_master_order({"role": ""}) is False


@pytest.mark.parametrize("message_type", ["CANCEL", "DECISION", "MESSAGE", "STATUS", "APPROVAL_REQUIRED"])
def test_non_orders_are_not_executable(message_type):
    assert not worker_poll.is_master_order({
        "role": "master", "order_id": "ORD-1", "message_type": message_type,
    })


def test_corrupted_typed_orders_are_not_executable():
    assert not worker_poll.is_master_order({
        "role": "master", "message_type": "ORDER", "typed_integrity": "INVALID",
    })


def test_latest_cursor_fetch_fails_closed_on_network_error(monkeypatch):
    def fail(*args, **kwargs):
        raise worker_poll.requests.ConnectionError("offline")
    monkeypatch.setattr(worker_poll.requests, "get", fail)
    with pytest.raises(worker_poll.requests.ConnectionError):
        worker_poll.fetch_latest_message_id(headers={})


def test_pending_start_uses_full_pending_backlog(monkeypatch):
    monkeypatch.setattr(worker_poll, "fetch_latest_message_id", lambda *a, **kw: 1000)
    old_order = {"id": 1, "role": "master", "order_id": "OLD", "content": "Older unfinished work"}
    monkeypatch.setattr(worker_poll, "fetch_pending_orders", lambda *a, **kw: [old_order], raising=False)
    monkeypatch.setattr(worker_poll, "fetch_messages", lambda *a, **kw: [])
    executed = []
    monkeypatch.setattr(worker_poll, "execute_order", lambda msg, *a, **kw: executed.append(msg))
    assert worker_poll.run_worker_loop(token="test", run_mode="once") == 0
    assert executed == [old_order]


def test_worker_reports_nonzero_exit_on_execution_and_reporting_failure(monkeypatch):
    monkeypatch.setattr(worker_poll, "fetch_latest_message_id", lambda *a, **kw: 0)
    monkeypatch.setattr(worker_poll, "fetch_messages", lambda *a, **kw: [
        {"id": 1, "role": "master", "message_type": "ORDER", "content": "Work"},
    ])
    def fail(*args, **kwargs):
        raise worker_poll.requests.ConnectionError("offline")
    monkeypatch.setattr(worker_poll, "execute_order", fail)
    monkeypatch.setattr(worker_poll, "post_execution_report", fail)
    assert worker_poll.run_worker_loop(token="test", start_mode="history", run_mode="once") == 1



def test_post_execution_report_posts_correct_body(monkeypatch):
    captured = {}

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"id": 100, "status": "COMPLETED"}

    def fake_post(url, *, json, headers, timeout):
        captured["json"] = json
        return FakeResponse()

    monkeypatch.setattr(worker_poll.requests, "post", fake_post)
    resp = worker_poll.post_execution_report(
        content="Finished processing order",
        order_id="ORD-1",
        status="COMPLETED",
        reply_to_id=12,
        headers={"Authorization": "Bearer test"},
    )
    assert resp["id"] == 100
    assert captured["json"]["order_id"] == "ORD-1"
    assert captured["json"]["status"] == "COMPLETED"
    assert captured["json"]["reply_to_id"] == 12


def test_execute_order_sends_running_and_completed_for_verified_executor(monkeypatch):
    posts = []

    def fake_post_report(**kwargs):
        posts.append(kwargs)
        return {"id": len(posts)}

    monkeypatch.setattr(worker_poll, "post_execution_report", fake_post_report)

    order = {
        "id": 1,
        "role": "master",
        "order_id": "ORD-42",
        "content": "Perform sanity check",
    }
    worker_poll.execute_order(
        order, "http://test", headers={"Authorization": "Bearer test"},
        executor=lambda msg: "Sanity check performed and verified",
    )

    assert len(posts) == 2
    assert posts[0]["status"] == "RUNNING"
    assert posts[0]["order_id"] == "ORD-42"
    assert posts[1]["status"] == "COMPLETED"
    assert posts[1]["order_id"] == "ORD-42"


def test_run_worker_loop_once_mode(monkeypatch):
    monkeypatch.setattr(worker_poll, "get_auth_headers", lambda token: {"Authorization": "Bearer test"})
    monkeypatch.setattr(worker_poll, "fetch_latest_message_id", lambda *a, **kw: 0)
    monkeypatch.setattr(worker_poll, "fetch_pending_orders", lambda *a, **kw: [
        {"id": 1, "role": "master", "order_id": "ORD-1", "content": "do task 1"},
    ])

    polled = [False]

    def fake_fetch_messages(after_id, **kw):
        polled[0] = True
        return [
            {"id": 1, "role": "master", "order_id": "ORD-1", "content": "do task 1"}
        ]

    executed = []
    monkeypatch.setattr(worker_poll, "fetch_messages", fake_fetch_messages)
    monkeypatch.setattr(worker_poll, "execute_order", lambda msg, *a, **kw: executed.append(msg))

    exit_code = worker_poll.run_worker_loop(run_mode="once")
    assert exit_code == 0
    assert polled[0] is False  # Pending startup uses the pending backlog API.
    assert len(executed) == 1
    assert executed[0]["order_id"] == "ORD-1"


def test_run_worker_loop_bounded_mode_exits_on_idle(monkeypatch):
    monkeypatch.setattr(worker_poll, "get_auth_headers", lambda token: {"Authorization": "Bearer test"})
    monkeypatch.setattr(worker_poll, "fetch_latest_message_id", lambda *a, **kw: 0)
    monkeypatch.setattr(worker_poll, "fetch_pending_orders", lambda *a, **kw: [])
    monkeypatch.setattr(worker_poll, "fetch_messages", lambda *a, **kw: [])
    monkeypatch.setattr(worker_poll.time, "sleep", lambda s: None)

    exit_code = worker_poll.run_worker_loop(run_mode="bounded", max_idle_polls=2)
    assert exit_code == 0


def test_worker_without_executor_never_claims_completion(monkeypatch):
    posts = []
    monkeypatch.setattr(worker_poll, "post_execution_report", lambda **kw: posts.append(kw))
    worker_poll.execute_order({"id": 1, "role": "master", "content": "Do work"}, "http://test", {})
    assert len(posts) == 1
    assert posts[0]["status"] == "BLOCKED"
    assert "No task executor" in posts[0]["content"]


def test_pending_order_fetch_reads_every_page(monkeypatch):
    calls = []
    pages = [
        [{"id": i} for i in range(150, 50, -1)],
        [{"id": i} for i in range(50, 0, -1)],
    ]
    class Response:
        def raise_for_status(self):
            pass
        def json(self):
            return pages.pop(0)
    def get(*args, **kwargs):
        calls.append(kwargs["params"])
        return Response()
    monkeypatch.setattr(worker_poll.requests, "get", get)
    assert [o["id"] for o in worker_poll.fetch_pending_orders(headers={})] == list(range(1, 151))
    assert calls == [{"limit": 100}, {"limit": 100, "before_id": 51}]


def test_bounded_worker_stops_after_repeated_poll_failures(monkeypatch):
    def fail(*args, **kwargs):
        raise worker_poll.requests.ConnectionError("offline")
    monkeypatch.setattr(worker_poll, "fetch_messages", fail)
    monkeypatch.setattr(worker_poll.time, "sleep", lambda _: None)
    assert worker_poll.run_worker_loop(
        token="test", start_mode="history", run_mode="bounded", max_idle_polls=2,
    ) == 1


def test_failed_report_does_not_advance_cursor_past_unhandled_order(monkeypatch):
    cursors = []
    executed = []
    order = {"id": 8, "role": "master", "message_type": "ORDER", "content": "Work"}
    def fetch(after_id, **kwargs):
        cursors.append(after_id)
        return [order] if after_id < 8 else []
    def execute(msg, **kwargs):
        executed.append(msg["id"])
        if len(executed) == 1:
            raise RuntimeError("Executor offline")
    def report(**kwargs):
        raise worker_poll.requests.ConnectionError("Reporting offline")
    monkeypatch.setattr(worker_poll, "fetch_messages", fetch)
    monkeypatch.setattr(worker_poll, "execute_order", execute)
    monkeypatch.setattr(worker_poll, "post_execution_report", report)
    monkeypatch.setattr(worker_poll.time, "sleep", lambda _: None)
    assert worker_poll.run_worker_loop(
        token="test", start_mode="history", run_mode="bounded", max_idle_polls=2,
    ) == 1
    assert executed == [8, 8]
    assert cursors[:2] == [0, 0]


def test_startup_arrivals_are_not_dispatched_twice(monkeypatch):
    def order(message_id):
        return {"id": message_id, "role": "master", "message_type": "ORDER", "content": "Work"}
    monkeypatch.setattr(worker_poll, "fetch_latest_message_id", lambda *a, **kw: 100)
    monkeypatch.setattr(worker_poll, "fetch_pending_orders", lambda *a, **kw: [order(1), order(101)])
    monkeypatch.setattr(worker_poll, "fetch_messages", lambda cursor, **kw: [
        msg for msg in [order(101), order(102)] if msg["id"] > cursor
    ])
    executed = []
    monkeypatch.setattr(worker_poll, "execute_order", lambda msg, **kw: executed.append(msg["id"]))
    monkeypatch.setattr(worker_poll.time, "sleep", lambda _: None)
    assert worker_poll.run_worker_loop(token="test", run_mode="bounded", max_idle_polls=1) == 0
    assert executed == [1, 101, 102]
