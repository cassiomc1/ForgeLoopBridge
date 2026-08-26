from pathlib import Path

import pytest

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

    assert worker_poll.reports_commit_unknown_control_event(message) is True
    worker_poll.print_control_event(message)
    output = capsys.readouterr().out
    assert "COMMIT_UNKNOWN" in output
    assert "do not retry" in output.lower()


@pytest.mark.parametrize(
    "metadata",
    (
        {"next_action": "RECONCILE_ACTION"},
        {"message_type": "ACTION_RECONCILIATION_REQUIRED"},
        {"reason_code": "E_ACTION_COMMIT_UNKNOWN"},
    ),
)
def test_explicit_reconciliation_metadata_triggers_commit_unknown(metadata):
    assert worker_poll.reports_commit_unknown_control_event(metadata) is True


@pytest.mark.parametrize(
    "message",
    (
        {"content": "Write documentation about COMMIT_UNKNOWN."},
        {"content": "The previous implementation handled COMMIT_UNKNOWN correctly."},
        {"reason_code": "E_ACTION_APPROVAL_REQUIRED", "content": "approval"},
    ),
)
def test_free_form_text_does_not_trigger_commit_unknown(message):
    assert worker_poll.reports_commit_unknown_control_event(message) is False


def test_cursor_is_saved_only_after_successful_engineer_handoff(tmp_path, monkeypatch):
    monkeypatch.setattr(worker_poll, "STATE_FILE", tmp_path / "last-seen")
    message = {
        "id": 101,
        "role": "engineer",
        "content": "Implement the requested correction.",
        "task_id": "correction",
        "message_type": "TASK",
    }

    def fail_handoff(message, auto_ack=False):
        raise RuntimeError("handoff failed")

    monkeypatch.setattr(worker_poll, "handoff_message", fail_handoff)
    with pytest.raises(RuntimeError, match="handoff failed"):
        worker_poll.process_polled_messages([message], last_seen=100)
    assert worker_poll.load_last_seen() == 0

    monkeypatch.setattr(worker_poll, "handoff_message", lambda message, auto_ack=False: None)
    assert worker_poll.process_polled_messages([message], last_seen=100) == 101
    assert worker_poll.load_last_seen() == 101


def test_first_start_pending_hands_off_latest_engineer_instruction(tmp_path, monkeypatch):
    monkeypatch.setattr(worker_poll, "STATE_FILE", tmp_path / "last-seen")
    messages = [
        {"id": 40, "role": "worker", "content": "old receipt"},
        {"id": 41, "role": "engineer", "message_type": "TASK", "content": "old task"},
        {"id": 42, "role": "worker", "content": "old status"},
        {"id": 43, "role": "engineer", "message_type": "TASK", "content": "current task"},
    ]
    handed_off = []
    monkeypatch.setattr(worker_poll, "fetch_latest_messages", lambda limit=200: messages)
    monkeypatch.setattr(
        worker_poll,
        "handoff_message",
        lambda message, auto_ack=False: handed_off.append(message),
    )

    assert worker_poll.initialize_first_start("pending") == 43
    assert [message["id"] for message in handed_off] == [43]
    assert worker_poll.load_last_seen() == 43


def test_first_start_now_skips_existing_messages_explicitly(tmp_path, monkeypatch):
    monkeypatch.setattr(worker_poll, "STATE_FILE", tmp_path / "last-seen")
    monkeypatch.setattr(
        worker_poll,
        "fetch_latest_messages",
        lambda limit=200: [
            {"id": 42, "role": "engineer", "message_type": "TASK", "content": "existing"}
        ],
    )
    handed_off = []
    monkeypatch.setattr(
        worker_poll,
        "handoff_message",
        lambda message, auto_ack=False: handed_off.append(message),
    )

    assert worker_poll.initialize_first_start("now") == 42
    assert handed_off == []
    assert worker_poll.load_last_seen() == 42


def test_save_last_seen_replaces_a_temporary_cursor_file(tmp_path, monkeypatch):
    state_file = tmp_path / "last-seen"
    replaced = []
    original_replace = Path.replace

    def record_replace(path, target):
        replaced.append((path, target))
        return original_replace(path, target)

    monkeypatch.setattr(worker_poll, "STATE_FILE", state_file)
    monkeypatch.setattr(Path, "replace", record_replace)

    worker_poll.save_last_seen(7)

    assert state_file.read_text(encoding="utf-8") == "7"
    assert replaced == [(tmp_path / "last-seen.tmp", state_file)]
    assert not (tmp_path / "last-seen.tmp").exists()


def test_fetch_messages_page_forwards_before_id_and_limit(monkeypatch):
    captured = {}

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return [{"id": 10}]

    def fake_get(url, *, params, headers, timeout):
        captured["url"] = url
        captured["params"] = params
        captured["headers"] = headers
        return FakeResponse()

    monkeypatch.setattr(worker_poll.requests, "get", fake_get)

    messages = worker_poll.fetch_messages_page(before_id=50, limit=20)
    assert messages == [{"id": 10}]
    assert captured["params"] == {"before_id": 50, "limit": 20}
    assert "Bearer" in captured["headers"]["Authorization"]


def test_first_start_pending_backward_pages_beyond_latest_page(tmp_path, monkeypatch):
    monkeypatch.setattr(worker_poll, "STATE_FILE", tmp_path / "last-seen")

    # Engineer TASK #100 followed by 250 Worker status messages (#101 to #350)
    engineer_task = {
        "id": 100,
        "role": "engineer",
        "message_type": "TASK",
        "content": "Execute protocol task.",
    }
    worker_messages = [
        {"id": i, "role": "worker", "message_type": "STATUS", "content": f"Worker update {i}"}
        for i in range(101, 351)
    ]
    all_messages = [engineer_task] + worker_messages

    def fake_fetch_page(before_id=None, limit=200):
        if before_id is None:
            return all_messages[-limit:]
        return [m for m in all_messages if m["id"] < before_id][-limit:]

    monkeypatch.setattr(worker_poll, "fetch_messages_page", fake_fetch_page)
    monkeypatch.setattr(
        worker_poll,
        "fetch_latest_messages",
        lambda limit=200: fake_fetch_page(before_id=None, limit=limit),
    )

    handed_off = []
    monkeypatch.setattr(
        worker_poll,
        "handoff_message",
        lambda message, auto_ack=False: handed_off.append(message),
    )

    assert worker_poll.initialize_first_start("pending") == 100
    assert [message["id"] for message in handed_off] == [100]
    assert worker_poll.load_last_seen() == 100


def test_first_start_pending_exhausts_history_when_no_engineer_message(tmp_path, monkeypatch):
    monkeypatch.setattr(worker_poll, "STATE_FILE", tmp_path / "last-seen")

    # 250 worker messages, no engineer messages
    worker_messages = [
        {"id": i, "role": "worker", "message_type": "STATUS", "content": f"Worker update {i}"}
        for i in range(1, 251)
    ]

    def fake_fetch_page(before_id=None, limit=200):
        if before_id is None:
            return worker_messages[-limit:]
        return [m for m in worker_messages if m["id"] < before_id][-limit:]

    monkeypatch.setattr(worker_poll, "fetch_messages_page", fake_fetch_page)
    monkeypatch.setattr(
        worker_poll,
        "fetch_latest_messages",
        lambda limit=200: fake_fetch_page(before_id=None, limit=limit),
    )

    handed_off = []
    monkeypatch.setattr(
        worker_poll,
        "handoff_message",
        lambda message, auto_ack=False: handed_off.append(message),
    )

    assert worker_poll.initialize_first_start("pending") == 250
    assert handed_off == []
    assert worker_poll.load_last_seen() == 250
