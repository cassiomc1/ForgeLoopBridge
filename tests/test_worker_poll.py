import json
import sys
import time
from pathlib import Path

import pytest

from examples import worker_poll

WORKER_POLL = Path(worker_poll.__file__).read_text(encoding="utf-8")


def pending_request(message_key: str = "worker-pending-1", content: str = "Status") -> dict:
    return {
        "content": content,
        "typed": {
            "schema_version": 1,
            "kind": "STATUS_UPDATE",
            "message_key": message_key,
            "correlation_id": None,
            "reply_to_id": None,
            "expects_reply": False,
            "payload": {
                "kind": "STATUS_UPDATE",
                "state": "IN_PROGRESS",
                "summary": "Running.",
            },
            "canonical_refs": [],
        },
    }


def test_fetch_latest_message_id_uses_latest_mode(monkeypatch):
    captured = {}

    class FakeResponse:
        status_code = 200

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
    assert "typed_integrity: invalid" in text
    assert "decision_notice" in text
    assert "message_key" in text
    assert "authorization" in text


def test_worker_poller_feature_detects_structural_quality_without_version_inference():
    text = WORKER_POLL.lower()
    assert "structuralquality" in text
    assert "task/structural-quality" in text
    assert "quality-status" in text
    assert "package version" in text
    assert "canonical forgeloop execution boundary" in text
    assert "never infer" in text


def test_worker_poller_documents_all_forgeloop_164_boundaries():
    text = WORKER_POLL.lower()
    for capability in (
        "workspacebinding",
        "canonicalhandoffs",
        "responsibilityconstraints",
        "differentialverificationscope",
        "codeattestation",
        "trusted scoped checker",
        "not_verified",
        "verified",
        "attested",
    ):
        assert capability in text
    for name in (
        "WORKSPACE_BOUNDARY_REASON_CODES",
        "HANDOFF_REASON_CODES",
        "RESPONSIBILITY_BOUNDARY_REASON_CODES",
        "VERIFICATION_SCOPE_REASON_CODES",
        "ATTESTATION_BOUNDARY_REASON_CODES",
        "REVISION_PROVIDER_REASON_CODES",
    ):
        assert name in WORKER_POLL


def test_read_forgeloop_context_uses_canonical_host_adapter(monkeypatch, tmp_path):
    monkeypatch.setenv("FORGELOOP_CONTEXT_COMMAND", "context-adapter --fixed")
    monkeypatch.setenv("FORGELOOP_CLI", "forgeloop --local")
    monkeypatch.setenv("FORGELOOP_PROJECT_PATH", str(tmp_path))
    calls = []

    def fake_run_json_command(command, arguments, project_root):
        calls.append((command, arguments, project_root))
        if arguments[0] == "protocol-info":
            return {
                "features": {
                    "adaptiveExecutionProfiles": {"supported": True},
                    "executionProfileContext": {"supported": True},
                },
                "resources": [{"name": "task/context"}],
            }
        return {"data": {
            "taskId": "task-context-1",
            "executionProfile": {
                "requested": "light",
                "floor": "balanced",
                "resolved": "balanced",
                "reasons": ["SAFETY_FLOOR"],
                "escalated": True,
            },
            "phase": "EXECUTING",
            "nextAction": "START_VERIFICATION",
            "objective": "Build the page.",
            "deliverables": ["index.html"],
            "constraints": ["No external services."],
            "selectedGuideIds": ["clean"],
            "verificationRequirements": [{"id": "html"}],
            "contextPolicy": {
                "contextDepth": "relevant",
                "output": "standard",
                "planDepth": "standard",
                "guideStrategy": "relevant",
                "verificationStrategy": "normal",
                "optionalArtifacts": "lazy",
                "requiredSections": ["objective", "verification"],
                "excludedContext": ["full-history"],
                "allowedOptionalContext": [],
            },
            "optionalContext": {"available": [], "loaded": []},
            "invariants": {
                "lifecyclePhasesPreserved": True,
                "requiredGatesPreserved": True,
                "evidenceRequirementsPreserved": True,
                "verificationTruthPreserved": True,
                "authorityChecksPreserved": True,
                "provenancePreserved": True,
                "completionValidationPreserved": True,
                "safetyFloorPreserved": True,
                "lifecyclePhaseSkippingAllowed": False,
            },
        }}

    monkeypatch.setattr(worker_poll, "_run_json_command", fake_run_json_command)

    consumed = worker_poll.read_forgeloop_context("task-context-1")

    assert consumed["status"] == "CANONICAL"
    assert consumed["execution_profile"]["resolved"] == "balanced"
    assert calls[0][0] == ["forgeloop", "--local"]
    assert calls[0][1][:2] == ["protocol-info", "--json"]
    assert calls[1][0] == ["context-adapter", "--fixed"]
    assert calls[1][1] == ["--task", "task-context-1", "--path", str(tmp_path), "--json"]
    assert all(call[2] == tmp_path for call in calls)


def test_context_status_payload_preserves_host_usage_and_unknowns(monkeypatch):
    context = {
        "status": "CANONICAL",
        "execution_profile": {
            "requested": "auto",
            "floor": "light",
            "resolved": "light",
            "reasons": [],
            "escalated": False,
        },
        "context_policy": {
            "context_depth": "targeted",
            "output": "compact",
            "plan_depth": "short",
            "guide_strategy": "targeted",
            "verification_strategy": "focused",
            "optional_artifacts": "lazy",
            "required_sections": [],
            "excluded_context": [],
            "allowed_optional_context": [],
        },
    }

    payload = worker_poll.build_context_status_payload(
        context,
        {
            "source": "HOST_REPORTED",
            "items": {"taskContext": 20, "guides": 5},
        },
    )

    assert payload["context_usage"] == {
        "source": "HOST_REPORTED",
        "profile": "light",
        "items": {
            "task_context": 20,
            "guides": 5,
            "history": None,
            "protocol_instructions": None,
            "repository_context": None,
            "other": None,
        },
    }
    assert "total" not in payload["context_usage"]["items"]

    unknown = worker_poll.build_context_status_payload(
        context,
        {"source": "UNKNOWN", "items": {}},
    )
    assert all(value is None for value in unknown["context_usage"]["items"].values())


def test_post_status_forwards_coordination_references(monkeypatch):
    captured = {}

    class FakeResponse:
        status_code = 200

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
        "content": "receipt",
        "message_type": "STATUS",
        "task_id": "task-a",
        "action_id": "action-a",
        "approval_id": "approval-a",
        "next_action": "REQUEST_ACTION_APPROVAL",
        "reason_code": "E_ACTION_APPROVAL_REQUIRED",
    }
    assert captured["headers"]["Authorization"] == f"Bearer {worker_poll.WORKER_TOKEN}"


@pytest.mark.parametrize(
    ("status_code", "classification"),
    (
        (408, "TRANSIENT"),
        (425, "TRANSIENT"),
        (429, "TRANSIENT"),
        (500, "TRANSIENT"),
        (503, "TRANSIENT"),
        (300, "PERMANENT"),
        (400, "PERMANENT"),
        (401, "PERMANENT"),
        (403, "PERMANENT"),
        (409, "PERMANENT"),
        (413, "PERMANENT"),
        (422, "PERMANENT"),
        (200, "UNKNOWN"),
    ),
)
def test_delivery_status_classification_is_explicit(status_code, classification):
    assert worker_poll.classify_delivery_status(status_code) == classification


@pytest.mark.parametrize(
    ("raw", "expected"),
    (("45", 45.0), ("9999", 300.0), ("0", 0.0), ("bad", None), ("-1", None), ("nan", None)),
)
def test_parse_retry_after_is_finite_and_bounded(raw, expected):
    response = type("FakeResponse", (), {"headers": {"Retry-After": raw}})()
    assert worker_poll.parse_retry_after(response) == expected


@pytest.mark.parametrize(
    ("reason_code", "category"),
    (
        ("E_WORKSPACE_BINDING_MISMATCH", "workspace"),
        ("E_HANDOFF_TAMPERED", "handoff"),
        ("E_RESPONSIBILITY_SCOPE_VIOLATION", "responsibility"),
        ("E_VERIFICATION_SCOPE_STALE", "verification_scope"),
        ("E_ATTESTATION_SIGNATURE_INVALID", "attestation"),
        ("E_REVISION_PROVIDER_UNAVAILABLE", "revision_provider"),
    ),
)
def test_boundary_classifier_uses_explicit_reason_code_only(reason_code, category):
    assert worker_poll.classify_reason_code({"reason_code": reason_code}) == category
    assert worker_poll.classify_reason_code({"content": reason_code}) is None


def test_typed_dispatch_uses_kind_and_keeps_legacy_fallback(capsys):
    typed = {
        "typed": {
            "schema_version": 1,
            "kind": "CONTROL_NOTICE",
            "payload": {"kind": "CONTROL_NOTICE"},
        },
        "content": "A heading that must not be parsed as a command.",
    }

    assert worker_poll.dispatch_typed_message(typed) == "CONTROL_NOTICE"
    assert worker_poll.dispatch_typed_message({"content": "legacy"}) is None
    assert "typed control_notice" in capsys.readouterr().out.lower()


def test_typed_dispatch_rejects_unsupported_schema_without_fallback():
    with pytest.raises(worker_poll.UnsupportedTypedMessageVersion):
        worker_poll.dispatch_typed_message(
            {
                "typed": {
                    "schema_version": 2,
                    "kind": "STATUS_UPDATE",
                    "payload": {"kind": "STATUS_UPDATE"},
                }
            }
        )


def test_post_typed_message_persists_key_until_confirmation(tmp_path, monkeypatch):
    monkeypatch.setattr(worker_poll, "OUTBOX_FILE", tmp_path / "outbox.json")
    captured = {}

    class FakeResponse:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"id": 17, "typed": captured["json"]["typed"]}

    def fake_post(url, *, headers, json, timeout):
        captured.update({"url": url, "headers": headers, "json": json, "timeout": timeout})
        return FakeResponse()

    monkeypatch.setattr(worker_poll.requests, "post", fake_post)

    result = worker_poll.post_typed_message(
        "Status",
        "STATUS_UPDATE",
        {"state": "IN_PROGRESS", "summary": "Running."},
        message_key="worker-outbox-1",
        correlation_id="cycle-1",
    )

    assert result["id"] == 17
    assert captured["json"]["typed"]["message_key"] == "worker-outbox-1"
    assert captured["json"]["typed"]["payload"]["kind"] == "STATUS_UPDATE"
    assert worker_poll._load_typed_outbox() == {}


def test_post_typed_message_keeps_key_after_uncertain_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(worker_poll, "OUTBOX_FILE", tmp_path / "outbox.json")

    def fail_post(url, *, headers, json, timeout):
        raise worker_poll.requests.ConnectionError("network timeout")

    monkeypatch.setattr(worker_poll.requests, "post", fail_post)

    with pytest.raises(worker_poll.requests.ConnectionError):
        worker_poll.post_typed_message(
            "Status",
            "STATUS_UPDATE",
            {"state": "WAITING", "summary": "Waiting."},
            message_key="worker-outbox-2",
        )

    assert "worker-outbox-2" in worker_poll._load_typed_outbox()


def test_typed_outbox_never_persists_authentication_material(tmp_path, monkeypatch):
    outbox_file = tmp_path / "outbox.json"
    monkeypatch.setattr(worker_poll, "OUTBOX_FILE", outbox_file)
    captured = {}

    def fail_post(url, *, headers, json, timeout):
        captured.update({"headers": headers, "json": json})
        raise worker_poll.requests.ConnectionError("network timeout")

    monkeypatch.setattr(worker_poll.requests, "post", fail_post)

    with pytest.raises(worker_poll.requests.ConnectionError):
        worker_poll.post_typed_message(
            "Safe status",
            "STATUS_UPDATE",
            {"state": "WAITING", "summary": "Waiting."},
            message_key="worker-outbox-secure",
        )

    persisted = outbox_file.read_text(encoding="utf-8")
    assert worker_poll.WORKER_TOKEN not in persisted
    assert "Authorization" not in persisted
    assert "Bearer" not in persisted
    assert "token" not in persisted.lower()
    assert captured["headers"] == {"Authorization": f"Bearer {worker_poll.WORKER_TOKEN}"}
    assert "token" not in json.dumps(captured["json"]).lower()


@pytest.mark.parametrize(
    "secret_field",
    ("Authorization", "Bearer", "ticket", "sse_ticket", "engineer_token", "signing_key", "oidc_token"),
)
def test_outbox_rejects_secret_fields(tmp_path, monkeypatch, secret_field):
    outbox_file = tmp_path / "outbox.json"
    monkeypatch.setattr(worker_poll, "OUTBOX_FILE", outbox_file)
    request = pending_request("worker-secret-field")
    request[secret_field] = "secret-material"

    with pytest.raises(worker_poll.OutboxSecurityError):
        worker_poll._save_typed_outbox(
            {"worker-secret-field": worker_poll._new_outbox_entry(request)}
        )

    assert not outbox_file.exists()


def test_retry_pending_typed_messages_replays_exact_request_and_removes_on_2xx(tmp_path, monkeypatch):
    outbox_file = tmp_path / "outbox.json"
    monkeypatch.setattr(worker_poll, "OUTBOX_FILE", outbox_file)
    request = pending_request("worker-replay-1")
    worker_poll._save_typed_outbox({"worker-replay-1": worker_poll._new_outbox_entry(request)})
    captured = {}

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"id": 19}

    def fake_post(url, *, headers, json, timeout):
        captured.update({"url": url, "headers": headers, "json": json, "timeout": timeout})
        return FakeResponse()

    monkeypatch.setattr(worker_poll.requests, "post", fake_post)

    assert worker_poll.retry_pending_typed_messages() == 1
    assert captured["json"] == request
    assert captured["json"]["typed"]["message_key"] == "worker-replay-1"
    assert captured["headers"] == {"Authorization": f"Bearer {worker_poll.WORKER_TOKEN}"}
    assert worker_poll._load_typed_outbox() == {}


@pytest.mark.parametrize("failure", ("network", "server"))
def test_retry_pending_typed_messages_preserves_uncertain_failures(tmp_path, monkeypatch, failure):
    outbox_file = tmp_path / "outbox.json"
    monkeypatch.setattr(worker_poll, "OUTBOX_FILE", outbox_file)
    request = pending_request("worker-retry-preserve")
    worker_poll._save_typed_outbox(
        {"worker-retry-preserve": worker_poll._new_outbox_entry(request)}
    )

    def fake_post(url, *, headers, json, timeout):
        if failure == "network":
            raise worker_poll.requests.ConnectionError("network timeout")
        return type(
            "FakeResponse",
            (),
            {"status_code": 503, "json": lambda self: {"error": {"code": "E_TEMPORARY"}}},
        )()

    monkeypatch.setattr(worker_poll.requests, "post", fake_post)

    assert worker_poll.retry_pending_typed_messages() == 0
    pending = worker_poll._load_typed_outbox()
    assert pending["worker-retry-preserve"]["request"] == request
    assert pending["worker-retry-preserve"]["attempts"] == 1
    assert pending["worker-retry-preserve"]["last_error"]
    assert pending["worker-retry-preserve"]["next_attempt_at"] > time.time()


@pytest.mark.parametrize("status_code", (408, 425, 429))
def test_transient_client_statuses_remain_pending(tmp_path, monkeypatch, status_code):
    outbox_file = tmp_path / "outbox.json"
    monkeypatch.setattr(worker_poll, "OUTBOX_FILE", outbox_file)
    key = f"worker-transient-{status_code}"
    request = pending_request(key)
    worker_poll._save_typed_outbox({key: worker_poll._new_outbox_entry(request)})
    now = 1_000.0
    monkeypatch.setattr(worker_poll.time, "time", lambda: now)

    response_type = type(
        "FakeResponse",
        (),
        {
            "status_code": status_code,
            "headers": {"Retry-After": "45"} if status_code == 429 else {},
            "json": lambda self: {"error": {"code": "E_TEMPORARY"}},
        },
    )
    calls = []

    def fake_post(url, *, headers, json, timeout):
        calls.append(json)
        return response_type()

    monkeypatch.setattr(worker_poll.requests, "post", fake_post)

    assert worker_poll.retry_pending_typed_messages() == 0
    pending = worker_poll._load_typed_outbox()
    assert pending[key]["request"] == request
    assert pending[key]["next_attempt_at"] >= now + (45 if status_code == 429 else 1)
    assert worker_poll._load_failed_outbox() == {}
    assert calls == [request]


def test_retry_pending_skips_future_next_attempt(tmp_path, monkeypatch):
    outbox_file = tmp_path / "outbox.json"
    monkeypatch.setattr(worker_poll, "OUTBOX_FILE", outbox_file)
    key = "worker-future-retry"
    entry = worker_poll._new_outbox_entry(pending_request(key))
    entry["next_attempt_at"] = time.time() + 300
    worker_poll._save_typed_outbox({key: entry})
    calls = []
    monkeypatch.setattr(worker_poll.requests, "post", lambda *args, **kwargs: calls.append(True))

    assert worker_poll.retry_pending_typed_messages() == 0
    assert calls == []
    assert worker_poll._load_typed_outbox()[key]["attempts"] == 0


def test_retry_pending_delivers_when_due(tmp_path, monkeypatch):
    outbox_file = tmp_path / "outbox.json"
    monkeypatch.setattr(worker_poll, "OUTBOX_FILE", outbox_file)
    key = "worker-due-retry"
    entry = worker_poll._new_outbox_entry(pending_request(key))
    entry["next_attempt_at"] = time.time() - 1
    worker_poll._save_typed_outbox({key: entry})

    class FakeResponse:
        status_code = 200
        headers = {}

        def json(self):
            return {"id": 20}

    calls = []

    def fake_post(url, *, headers, json, timeout):
        calls.append(json)
        return FakeResponse()

    monkeypatch.setattr(worker_poll.requests, "post", fake_post)

    assert worker_poll.retry_pending_typed_messages() == 1
    assert calls == [pending_request(key)]
    assert worker_poll._load_typed_outbox() == {}


@pytest.mark.parametrize(
    ("status_code", "error_code"),
    (
        (400, "E_BRIDGE_TYPED_PAYLOAD_INVALID"),
        (401, "E_AUTHENTICATION_FAILED"),
        (403, "E_FORBIDDEN"),
        (409, "E_BRIDGE_IDEMPOTENCY_CONFLICT"),
        (413, "E_BRIDGE_TYPED_PAYLOAD_TOO_LARGE"),
        (422, "E_BRIDGE_TYPED_PAYLOAD_INVALID"),
    ),
)
def test_permanent_typed_outbox_failures_are_quarantined_and_not_retried(
    tmp_path, monkeypatch, status_code, error_code
):
    outbox_file = tmp_path / "outbox.json"
    monkeypatch.setattr(worker_poll, "OUTBOX_FILE", outbox_file)
    key = f"worker-permanent-{status_code}"
    worker_poll._save_typed_outbox({key: worker_poll._new_outbox_entry(pending_request(key))})
    calls = []

    class FakeResponse:
        def __init__(self):
            self.status_code = status_code

        def json(self):
            return {"error": {"code": error_code}}

    def fake_post(url, *, headers, json, timeout):
        calls.append(json)
        return FakeResponse()

    monkeypatch.setattr(worker_poll.requests, "post", fake_post)

    assert worker_poll.retry_pending_typed_messages() == 0
    assert worker_poll._load_typed_outbox() == {}
    failed = worker_poll._load_failed_outbox()
    assert key in failed
    assert error_code in failed[key]["last_error"]

    assert worker_poll.retry_pending_typed_messages() == 0
    assert len(calls) == 1


def test_corrupt_or_legacy_secret_outbox_is_quarantined(tmp_path, monkeypatch):
    outbox_file = tmp_path / "outbox.json"
    monkeypatch.setattr(worker_poll, "OUTBOX_FILE", outbox_file)

    outbox_file.write_text("{not-json", encoding="utf-8")
    assert worker_poll._load_typed_outbox() == {}
    assert list(tmp_path.glob("outbox.corrupt.*.json"))

    outbox_file.write_text(
        json.dumps({"worker-legacy-1": {"token": worker_poll.WORKER_TOKEN}}),
        encoding="utf-8",
    )
    assert worker_poll._load_typed_outbox() == {}
    assert len(list(tmp_path.glob("outbox.corrupt.*.json"))) == 2


def test_outbox_entry_and_byte_limits_fail_closed(tmp_path, monkeypatch):
    outbox_file = tmp_path / "outbox.json"
    monkeypatch.setattr(worker_poll, "OUTBOX_FILE", outbox_file)
    first = worker_poll._new_outbox_entry(pending_request("worker-limit-1"))
    second = worker_poll._new_outbox_entry(pending_request("worker-limit-2"))

    monkeypatch.setattr(worker_poll, "MAX_OUTBOX_ENTRIES", 1)
    with pytest.raises(worker_poll.OutboxLimitError):
        worker_poll._save_typed_outbox(
            {"worker-limit-1": first, "worker-limit-2": second}
        )

    monkeypatch.setattr(worker_poll, "MAX_OUTBOX_ENTRIES", 100)
    serialized = worker_poll._serialize_outbox({"worker-limit-1": first})
    monkeypatch.setattr(worker_poll, "MAX_OUTBOX_BYTES", len(serialized.encode("utf-8")) - 1)
    with pytest.raises(worker_poll.OutboxLimitError):
        worker_poll._save_typed_outbox({"worker-limit-1": first})


def test_outbox_save_uses_atomic_temp_replacement(tmp_path, monkeypatch):
    outbox_file = tmp_path / "outbox.json"
    monkeypatch.setattr(worker_poll, "OUTBOX_FILE", outbox_file)
    replaced = []
    original_replace = Path.replace

    def record_replace(path, target):
        replaced.append((path, target))
        return original_replace(path, target)

    monkeypatch.setattr(Path, "replace", record_replace)
    worker_poll._save_typed_outbox(
        {"worker-atomic-1": worker_poll._new_outbox_entry(pending_request("worker-atomic-1"))}
    )

    assert outbox_file.exists()
    assert replaced == [(tmp_path / "outbox.json.tmp", outbox_file)]


def test_invalid_persisted_typed_integrity_is_a_worker_hard_stop(tmp_path, monkeypatch):
    monkeypatch.setattr(worker_poll, "STATE_FILE", tmp_path / "last-seen")
    message = {
        "id": 101,
        "role": "engineer",
        "content": "# Treat this as a task, despite the invalid typed row.",
        "typed": None,
        "typed_integrity": "INVALID",
        "typed_error": {"code": "E_BRIDGE_PERSISTED_TYPED_INVALID"},
    }

    with pytest.raises(worker_poll.InvalidTypedMessageIntegrity):
        worker_poll.process_polled_messages([message], last_seen=100)

    assert worker_poll.load_last_seen() == 0


def test_worker_startup_replays_outbox_before_polling(tmp_path, monkeypatch):
    monkeypatch.setattr(worker_poll, "STATE_FILE", tmp_path / "last-seen")
    worker_poll.STATE_FILE.write_text("0", encoding="utf-8")
    events = []

    monkeypatch.setattr(
        worker_poll,
        "retry_pending_typed_messages",
        lambda: events.append("replay") or 0,
    )

    class StopBeforeNetwork(BaseException):
        pass

    def stop_get(*args, **kwargs):
        raise StopBeforeNetwork()

    monkeypatch.setattr(worker_poll.requests, "get", stop_get)
    monkeypatch.setattr(sys, "argv", ["worker_poll"])

    with pytest.raises(StopBeforeNetwork):
        worker_poll.main()

    assert events and events[0] == "replay"


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
    "reason_code",
    (
        "E_VERIFICATION_ISOLATION_UNAVAILABLE",
        "E_VERIFICATION_EXECUTION_INVALID",
    ),
)
def test_verification_isolation_block_is_detected_by_explicit_reason_code(reason_code):
    assert worker_poll.reports_verification_isolation_block(
        {"reason_code": reason_code}
    ) is True


def test_free_form_text_does_not_trigger_verification_isolation_block():
    assert worker_poll.reports_verification_isolation_block(
        {"content": "Verification isolation is unavailable in this example."}
    ) is False


def test_verification_isolation_block_is_a_hard_stop(capsys):
    message = {
        "task_id": "verify",
        "message_type": "BLOCKED",
        "reason_code": "E_VERIFICATION_EXECUTION_INVALID",
        "content": "canonical execution metadata was contradictory",
    }

    worker_poll.print_control_event(message)
    output = capsys.readouterr().out.lower()
    assert "hard stop" in output
    assert "verification isolation" in output
    assert "do not downgrade" in output
    assert "canonical forgeloop" in output


def test_auto_ack_preserves_isolation_blocker_and_adds_hard_stop_guidance(monkeypatch):
    message = {
        "id": 12,
        "role": "engineer",
        "task_id": "verify",
        "message_type": "BLOCKED",
        "reason_code": "E_VERIFICATION_ISOLATION_UNAVAILABLE",
        "content": "the trusted adapter is unavailable",
    }
    captured = {}
    monkeypatch.setattr(
        worker_poll,
        "post_status",
        lambda **kwargs: captured.update(kwargs),
    )

    worker_poll.handoff_message(message, auto_ack=True)

    assert captured["reason_code"] == "E_VERIFICATION_ISOLATION_UNAVAILABLE"
    assert "hard stop" in captured["content"].lower()
    assert "no weaker-isolation retry" in captured["content"].lower()
    assert "synthetic evidence" in captured["content"].lower()


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
