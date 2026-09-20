import pytest

from examples import worker_poll


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
    assert worker_poll.is_master_order({"role": "master"}) is True
    assert worker_poll.is_master_order({"role": "engineer"}) is True
    assert worker_poll.is_master_order({"role": "MASTER"}) is True
    assert worker_poll.is_master_order({"role": "master", "status": "PENDING"}) is True
    assert worker_poll.is_master_order({"role": "master", "status": "COMPLETED"}) is False
    assert worker_poll.is_master_order({"role": "master", "status": "CANCELLED"}) is False
    assert worker_poll.is_master_order({"role": "agent"}) is False
    assert worker_poll.is_master_order({"role": "worker"}) is False
    assert worker_poll.is_master_order({"role": ""}) is False



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


def test_execute_order_sends_running_and_completed(monkeypatch):
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
    worker_poll.execute_order(order, "http://test", headers={"Authorization": "Bearer test"})

    assert len(posts) == 2
    assert posts[0]["status"] == "RUNNING"
    assert posts[0]["order_id"] == "ORD-42"
    assert posts[1]["status"] == "COMPLETED"
    assert posts[1]["order_id"] == "ORD-42"


def test_run_worker_loop_once_mode(monkeypatch):
    monkeypatch.setattr(worker_poll, "get_auth_headers", lambda token: {"Authorization": "Bearer test"})
    monkeypatch.setattr(worker_poll, "fetch_latest_message_id", lambda *a, **kw: 0)

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
    assert polled[0] is True
    assert len(executed) == 1
    assert executed[0]["order_id"] == "ORD-1"


def test_run_worker_loop_bounded_mode_exits_on_idle(monkeypatch):
    monkeypatch.setattr(worker_poll, "get_auth_headers", lambda token: {"Authorization": "Bearer test"})
    monkeypatch.setattr(worker_poll, "fetch_latest_message_id", lambda *a, **kw: 0)
    monkeypatch.setattr(worker_poll, "fetch_messages", lambda *a, **kw: [])
    monkeypatch.setattr(worker_poll.time, "sleep", lambda s: None)

    exit_code = worker_poll.run_worker_loop(run_mode="bounded", max_idle_polls=2)
    assert exit_code == 0
