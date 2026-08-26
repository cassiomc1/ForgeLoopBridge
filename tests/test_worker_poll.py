from pathlib import Path

from examples import worker_poll

WORKER_POLL = Path(worker_poll.__file__).read_text(encoding="utf-8")


def test_fetch_latest_message_id_uses_latest_mode(monkeypatch):
    captured = {}

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return [{"id": 42}]

    def fake_get(url, *, params, headers, timeout):
        captured["url"] = url
        captured["params"] = params
        captured["headers"] = headers
        return FakeResponse()

    monkeypatch.setattr(worker_poll.requests, "get", fake_get)

    assert worker_poll.fetch_latest_message_id() == 42
    assert captured["params"] == {"latest": "true", "limit": 1}
    assert "Bearer" in captured["headers"]["Authorization"]


def test_fetch_latest_message_id_returns_zero_for_empty_board(monkeypatch):
    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return []

    def fake_get(url, *, params, headers, timeout):
        return FakeResponse()

    monkeypatch.setattr(worker_poll.requests, "get", fake_get)

    assert worker_poll.fetch_latest_message_id() == 0


def test_worker_poller_uses_capability_discovery_and_safe_dispatch_language():
    text = WORKER_POLL.lower()
    assert "forgeloop 1.5" not in text
    assert "protocol-info --json" in text
    assert "feature" in text
    assert "capabilit" in text
    assert "commit_unknown" in text
    assert "do not retry" in text
    assert "approval" in text
    assert "not forgeloop authority" in text
    assert "forgeloop complete --task <task-id> --json" in text
    assert ".worker_last_seen" in text


def test_post_status_forwards_coordination_references(monkeypatch):
    captured = {}

    class FakeResponse:
        def raise_for_status(self):
            pass

    def fake_post(url, *, headers, json, timeout):
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        return FakeResponse()

    monkeypatch.setattr(worker_poll.requests, "post", fake_post)

    worker_poll.post_status(
        "receipt",
        task_id="task-a",
        action_id="action-a",
        approval_id="approval-a",
        next_action="REQUEST_ACTION_APPROVAL",
        reason_code="E_ACTION_APPROVAL_REQUIRED",
    )

    assert captured["json"] == {
        "token": worker_poll.WORKER_TOKEN,
        "content": "receipt",
        "message_type": "STATUS",
        "task_id": "task-a",
        "action_id": "action-a",
        "approval_id": "approval-a",
        "next_action": "REQUEST_ACTION_APPROVAL",
        "reason_code": "E_ACTION_APPROVAL_REQUIRED",
    }


def test_commit_unknown_is_a_hard_stop(capsys):
    message = {
        "task_id": "release",
        "message_type": "ACTION_RECONCILIATION_REQUIRED",
        "action_id": "action-release",
        "next_action": "RECONCILE_ACTION",
        "reason_code": "COMMIT_UNKNOWN",
        "content": "external state is ambiguous",
    }

    assert worker_poll.is_commit_unknown(message) is True
    worker_poll.print_control_event(message)
    output = capsys.readouterr().out
    assert "COMMIT_UNKNOWN" in output
    assert "do not retry" in output.lower()
