"""Live Execution Observer tests (Phase 1: shell.online, read-only, E2EE).

CI must not contact the real shell.online provider: every provider
subprocess is mocked or backed by a local fake executable.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

from examples.live_observer import base as observer_base
from examples.live_observer import shell_online as shell_adapter
from examples.live_observer.base import (
    ObserverError,
    ObserverSession,
    build_observer_announcement,
    get_provider_name,
    get_shell_command,
    is_observer_enabled,
    map_relay_status,
    project_safe_metadata,
    validate_share_url,
)
from examples.live_observer.shell_online import (
    build_wrapper_argv,
    find_session_entry,
    parse_list_output,
    parse_start_output,
    query_status,
    stop_session,
)

ROOT = Path(__file__).resolve().parents[1]
FAKE_SAFE_RESPONSE = {
    "share_url": "https://shell.online/example#salt=fake",
    "e2ee_password": "SUPER_SECRET_TEST_PASSWORD",
    "session_id": "sess-test-001",
    "read_only": True,
    "encrypted": True,
    "background": False,
}
FAKE_SECRET = "SUPER_SECRET_TEST_PASSWORD"


def _clear_env(monkeypatch, name: str) -> None:
    monkeypatch.delenv(name, raising=False)


# ─── Default disabled behavior ───────────────────────────────────────────────


def test_observer_disabled_by_default(monkeypatch):
    _clear_env(monkeypatch, "FORGEBRIDGE_LIVE_OBSERVER")
    _clear_env(monkeypatch, "FORGEBRIDGE_LIVE_OBSERVER_COMMAND")
    assert get_provider_name() == "none"
    assert is_observer_enabled() is False
    assert get_shell_command() == "shell"


def test_observer_env_opt_in(monkeypatch):
    monkeypatch.setenv("FORGEBRIDGE_LIVE_OBSERVER", "shell-online")
    assert is_observer_enabled() is True
    assert get_provider_name() == "shell-online"
    monkeypatch.setenv("FORGEBRIDGE_LIVE_OBSERVER_COMMAND", "custom-shell")
    assert get_shell_command() == "custom-shell"


def test_observer_unknown_provider_is_not_supported():
    assert observer_base.is_supported_provider("none") is True
    assert observer_base.is_supported_provider("shell-online") is True
    assert observer_base.is_supported_provider("weird-provider") is False


def test_phase1_access_mode_is_always_read_only():
    assert observer_base.ACCESS_MODE == "READ_ONLY"


# ─── Provider executable detection (fail-open) ───────────────────────────────


def test_shell_online_missing_is_non_fatal(monkeypatch):
    monkeypatch.setattr(shell_adapter.shutil, "which", lambda _cmd: None)
    shell_adapter.clear_probe_cache()
    try:
        assert shell_adapter.available("definitely-missing-shell") is False
        with pytest.raises(ObserverError) as excinfo:
            shell_adapter.resolve_executable("definitely-missing-shell")
        assert excinfo.value.code == "OBSERVER_EXECUTABLE_NOT_FOUND"
    finally:
        shell_adapter.clear_probe_cache()


def test_shell_online_preflight_uses_bounded_argv_subprocesses(monkeypatch):
    calls: list[dict] = []

    def fake_which(cmd):
        return f"/usr/local/bin/{cmd}"

    class FakeProc:
        returncode = 0
        stdout = "shell version 0.7.3\n"
        stderr = ""

    def fake_run(argv, **kwargs):
        calls.append({"argv": argv, "kwargs": kwargs})
        assert kwargs.get("shell") is False
        assert kwargs.get("timeout") is not None
        assert float(kwargs["timeout"]) <= 60
        proc = FakeProc()
        # Second call (help reference) advertises the required surface.
        if argv[-2:] == ["help", "reference"]:
            proc = FakeProc()
            proc.stdout = "--read-only --json list --json --foreground kill attach"
        return proc

    monkeypatch.setattr(shell_adapter.shutil, "which", fake_which)
    monkeypatch.setattr(shell_adapter.subprocess, "run", fake_run)
    shell_adapter.clear_probe_cache()
    try:
        probe = shell_adapter.preflight("shell")
        assert probe["supports_read_only"] is True
        assert probe["supports_json"] is True
        assert probe["supports_foreground"] is True
        assert shell_adapter.available("shell") is True
    finally:
        shell_adapter.clear_probe_cache()
    assert len(calls) >= 2
    for call in calls:
        assert isinstance(call["argv"], list)


def test_provider_subprocesses_never_use_shell_true():
    for module in ("base.py", "shell_online.py"):
        text = (ROOT / "examples" / "live_observer" / module).read_text(encoding="utf-8")
        assert "shell=True" not in text
    helper = (ROOT / "examples" / "run_worker_observed.py").read_text(encoding="utf-8")
    assert "shell=True" not in helper
    assert "curl" not in helper or "curl" in helper and "| sh" not in helper
    assert "curl ... | sh" not in helper


def test_helper_has_no_interactive_option():
    import argparse

    from examples import run_worker_observed as helper

    text = (ROOT / "examples" / "run_worker_observed.py").read_text(encoding="utf-8")
    # No interactive CLI flag may be registered (prose may document its absence).
    parser_actions: list[str] = []
    original_add = argparse.ArgumentParser.add_argument

    def capture_add(self, *args, **kwargs):
        for name in args:
            if isinstance(name, str) and name.startswith("-"):
                parser_actions.append(name)
        return original_add(self, *args, **kwargs)

    import unittest.mock as _mock

    with _mock.patch.object(argparse.ArgumentParser, "add_argument", capture_add):
        helper.parse_args(["--provider", "none", "--", "echo", "hi"])
    assert "--interactive" not in parser_actions
    # No interactive/e2ee-disabling argv is ever constructed.
    assert '"--interactive"' not in text
    assert "'--interactive'" not in text
    assert "kill --all" not in text
    assert '"--all"' not in text
    assert "'--all'" not in text


# ─── Safe session model + allow-list projection ──────────────────────────────


def test_shell_online_safe_metadata_projection():
    parsed = project_safe_metadata(dict(FAKE_SAFE_RESPONSE))
    session = parsed.session
    assert session.provider == "shell.online"
    assert session.session_id == "sess-test-001"
    assert session.share_url == "https://shell.online/example#salt=fake"
    assert session.read_only is True
    assert session.encrypted is True
    assert parsed.e2ee_password == FAKE_SECRET
    # Allow-list only: no background flag, no raw JSON retention.
    assert not hasattr(session, "e2ee_password")
    assert not hasattr(session, "background")
    assert "SUPER_SECRET" not in repr(session)


def test_shell_online_password_is_not_persisted():
    parsed = project_safe_metadata(dict(FAKE_SAFE_RESPONSE))
    session = parsed.session
    announcement = build_observer_announcement(session, task_id="taskvault-mvp")
    assert FAKE_SECRET not in announcement
    assert FAKE_SECRET not in json.dumps(
        {
            "provider": session.provider,
            "session_id": session.session_id,
            "share_url": session.share_url,
            "read_only": session.read_only,
            "encrypted": session.encrypted,
        }
    )
    assert observer_base.format_start_log(session).find(FAKE_SECRET) == -1
    assert observer_base.format_end_log(session.session_id).find(FAKE_SECRET) == -1


def test_shell_online_rejects_interactive_session(monkeypatch):
    payload = dict(FAKE_SAFE_RESPONSE, read_only=False)
    with pytest.raises(ObserverError) as excinfo:
        project_safe_metadata(payload)
    assert excinfo.value.code == "OBSERVER_UNSAFE_ACCESS_MODE"
    # Helper-level: parse path also rejects.
    with pytest.raises(ObserverError) as excinfo2:
        parse_start_output("", json.dumps(payload))
    assert excinfo2.value.code == "OBSERVER_UNSAFE_ACCESS_MODE"


def test_shell_online_rejects_unencrypted_session():
    payload = dict(FAKE_SAFE_RESPONSE, encrypted=False)
    with pytest.raises(ObserverError) as excinfo:
        project_safe_metadata(payload)
    assert excinfo.value.code == "OBSERVER_ENCRYPTION_DISABLED"


@pytest.mark.parametrize(
    "bad_url",
    [
        "javascript:alert(1)",
        "data:text/html,<h1>hi</h1>",
        "file:///etc/passwd",
        "http://shell.online/insecure",
        "https://evil.example.com/phish#salt=x",
        "https://user:pass@shell.online/with-creds",
        "https://shell.online.evil.com/spoof",
        "",
        "not-a-url",
    ],
)
def test_shell_online_rejects_invalid_url(bad_url):
    with pytest.raises(ObserverError) as excinfo:
        validate_share_url(bad_url)
    assert excinfo.value.code == "OBSERVER_URL_INVALID"
    payload = dict(FAKE_SAFE_RESPONSE, share_url=bad_url)
    with pytest.raises(ObserverError):
        project_safe_metadata(payload)


def test_shell_online_accepts_provider_host_variants():
    assert (
        validate_share_url("https://shell.online/s/abc#salt=xyz")
        == "https://shell.online/s/abc#salt=xyz"
    )
    assert (
        validate_share_url("https://relay.shell.online/s/abc#salt=xyz")
        == "https://relay.shell.online/s/abc#salt=xyz"
    )


def test_shell_online_malformed_json_is_non_fatal():
    with pytest.raises(ObserverError) as excinfo:
        parse_start_output("no json here", "still no json")
    assert excinfo.value.code == "OBSERVER_JSON_INVALID"
    with pytest.raises(ObserverError):
        parse_start_output("{not-json", "")
    # Callers treat this as observer-unavailable, never a Worker failure.
    assert excinfo.value.code != "COMMIT_UNKNOWN"


def test_parse_start_output_prefers_stderr_json_line():
    stderr = (
        "some log noise\n"
        + json.dumps(dict(FAKE_SAFE_RESPONSE))
        + "\nmore noise\n"
    )
    parsed = parse_start_output("stdout noise", stderr)
    assert parsed.session.session_id == "sess-test-001"
    assert parsed.e2ee_password == FAKE_SECRET


def test_wrapper_argv_is_safe_and_foreground(monkeypatch):
    monkeypatch.setattr(shell_adapter.shutil, "which", lambda cmd: f"/bin/{cmd}")
    monkeypatch.setattr(
        shell_adapter,
        "preflight",
        lambda cmd=None: {
            "supports_read_only": True,
            "supports_json": True,
            "supports_foreground": True,
        },
    )
    argv = build_wrapper_argv(["python", "-c", "print(1)"], "shell")
    assert argv[:4] == ["/bin/shell", "--read-only", "--json", "--foreground"]
    assert argv[4] == "--"
    assert argv[5:] == ["python", "-c", "print(1)"]
    joined = " ".join(argv)
    assert "--no-e2ee" not in joined
    assert "--persistent" not in joined
    assert "--interactive" not in joined
    assert "--all" not in joined


def test_wrapper_argv_refuses_unsafe_background_wrap(monkeypatch):
    monkeypatch.setattr(shell_adapter.shutil, "which", lambda cmd: f"/bin/{cmd}")
    monkeypatch.setattr(
        shell_adapter,
        "preflight",
        lambda cmd=None: {
            "supports_read_only": True,
            "supports_json": True,
            "supports_foreground": False,
        },
    )
    with pytest.raises(ObserverError) as excinfo:
        build_wrapper_argv(["echo", "hi"], "shell")
    assert excinfo.value.code == "OBSERVER_START_FAILED"


# ─── Observer status (diagnostic only, never lifecycle) ──────────────────────


def _mock_list_run(monkeypatch, entries, returncode=0):
    def fake_which(cmd):
        return f"/bin/{cmd}"

    class FakeProc:
        pass

    def fake_run(argv, **kwargs):
        assert argv == ["/bin/shell", "list", "--json"]
        proc = FakeProc()
        proc.returncode = returncode
        proc.stdout = json.dumps(entries)
        proc.stderr = ""
        return proc

    monkeypatch.setattr(shell_adapter.shutil, "which", fake_which)
    monkeypatch.setattr(shell_adapter.subprocess, "run", fake_run)


def test_shell_online_status_online(monkeypatch):
    _mock_list_run(
        monkeypatch,
        [{"session_id": "sess-1", "relay_status": "online"}],
    )
    assert query_status("sess-1") == "ONLINE"
    assert map_relay_status("online") == "ONLINE"


def test_shell_online_status_reconnecting(monkeypatch):
    _mock_list_run(
        monkeypatch,
        [{"session_id": "sess-1", "relay_status": "reconnecting"}],
    )
    assert query_status("sess-1") == "RECONNECTING"


def test_shell_online_status_expired_is_not_worker_failure(monkeypatch):
    _mock_list_run(
        monkeypatch,
        [{"session_id": "sess-1", "relay_status": "expired"}],
    )
    status = query_status("sess-1")
    assert status == "EXPIRED"
    # Observer diagnostics never become Worker/lifecycle failures.
    assert status not in ("FAILED", "BLOCKED", "COMPLETE_REPORTED")
    assert map_relay_status("expired") == "EXPIRED"


def test_shell_online_status_unknown_is_not_worker_failure(monkeypatch):
    _mock_list_run(monkeypatch, [{"session_id": "sess-1", "relay_status": "unknown"}])
    assert query_status("sess-1") == "UNKNOWN"
    _mock_list_run(monkeypatch, [{"session_id": "other", "relay_status": "online"}])
    assert query_status("missing-session") == "UNKNOWN"


def test_status_query_failure_is_diagnostic(monkeypatch):
    _mock_list_run(monkeypatch, [], returncode=1)
    with pytest.raises(ObserverError) as excinfo:
        query_status("sess-1")
    assert excinfo.value.code == "OBSERVER_STATUS_FAILED"


def test_parse_list_output_accepts_wrapped_shapes():
    assert parse_list_output('[{"session_id": "a"}]') == [{"session_id": "a"}]
    assert parse_list_output('{"sessions": [{"session_id": "a"}]}') == [
        {"session_id": "a"}
    ]
    assert find_session_entry([{"session_id": "sess-abc"}], "sess-abc") == {
        "session_id": "sess-abc"
    }
    assert find_session_entry([{"session_id": "sess-abc"}], "missing") is None


# ─── Session cleanup targets only the created session ────────────────────────


def test_shell_online_kills_only_created_session(monkeypatch):
    calls: list[list[str]] = []

    def fake_which(cmd):
        return f"/bin/{cmd}"

    class FakeProc:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(argv, **kwargs):
        calls.append(argv)
        assert kwargs.get("shell") is False
        return FakeProc()

    monkeypatch.setattr(shell_adapter.shutil, "which", fake_which)
    monkeypatch.setattr(shell_adapter.subprocess, "run", fake_run)
    stop_session("sess-test-001")
    assert calls == [["/bin/shell", "kill", "sess-test-001"]]
    for argv in calls:
        assert "--all" not in argv
    with pytest.raises(ObserverError):
        stop_session("--all")
    with pytest.raises(ObserverError):
        stop_session("")


# ─── Worker exit propagation ─────────────────────────────────────────────────


def test_worker_exit_code_is_preserved():
    from examples import run_worker_observed as helper

    assert helper._normalize_exit_code(0) == 0
    assert helper._normalize_exit_code(1) == 1
    assert helper._normalize_exit_code(3) == 3
    # Signal N maps to conventional 128+N instead of collapsing to success.
    assert helper._normalize_exit_code(-15) == 143


def test_run_direct_preserves_exit_codes():
    from examples import run_worker_observed as helper

    assert helper.run_direct([sys.executable, "-c", "import sys; sys.exit(0)"]) == 0
    assert helper.run_direct([sys.executable, "-c", "import sys; sys.exit(1)"]) == 1
    assert helper.run_direct([sys.executable, "-c", "import sys; sys.exit(3)"]) == 3


def test_run_worker_observed_provider_none_runs_direct(monkeypatch):
    from examples import run_worker_observed as helper

    code = helper.main(
        [
            "--provider",
            "none",
            "--",
            sys.executable,
            "-c",
            "import sys; sys.exit(7)",
        ]
    )
    assert code == 7


def test_run_worker_observed_missing_provider_fails_open(monkeypatch):
    from examples import run_worker_observed as helper

    monkeypatch.setattr(helper, "preflight", lambda cmd=None: (_ for _ in ()).throw(
        ObserverError("OBSERVER_EXECUTABLE_NOT_FOUND", "missing")
    ))
    code = helper.run_observed(
        [sys.executable, "-c", "import sys; sys.exit(5)"],
        shell_cmd="missing-shell",
        bridge_url="http://localhost:8000",
        worker_token=None,
        task_id=None,
        metadata_timeout=1.0,
    )
    assert code == 5


# ─── Bridge announcement (once, non-authoritative, no secret) ────────────────


def test_observer_message_posted_once(monkeypatch):
    from examples import run_worker_observed as helper

    posted: list[dict] = []

    class FakeStderr:
        def readline(self):
            return json.dumps(dict(FAKE_SAFE_RESPONSE)) + "\n"

        def read(self, *args):
            return ""

        def fileno(self):
            return 99

    class FakeProc:
        stderr = FakeStderr()
        returncode = 0

        def wait(self):
            return 0

    monkeypatch.setattr(
        helper, "preflight", lambda cmd=None: {"supports_read_only": True, "supports_json": True}
    )
    monkeypatch.setattr(
        helper, "build_wrapper_argv", lambda worker, cmd=None, foreground=True: ["shell-mock", *worker]
    )
    monkeypatch.setattr(helper.subprocess, "Popen", lambda *args, **kwargs: FakeProc())
    monkeypatch.setattr(helper.select, "select", lambda r, w, x, t=None: ([r[0]], [], []))
    monkeypatch.setattr(helper, "write_secret_file", lambda *args: Path("/tmp/fake-secret.json"))
    monkeypatch.setattr(helper, "remove_secret_file", lambda path: None)
    monkeypatch.setattr(helper, "stop_session", lambda session_id, cmd=None: None)

    def fake_post(bridge_url, token, content, task_id):
        posted.append({"content": content, "task_id": task_id})
        assert FAKE_SECRET not in content
        return True

    monkeypatch.setattr(helper, "post_observer_announcement", fake_post)
    code = helper.run_observed(
        [sys.executable, "-c", "print(1)"],
        shell_cmd="shell",
        bridge_url="http://localhost:8000",
        worker_token="worker-token",
        task_id="taskvault-mvp",
        metadata_timeout=2.0,
    )
    assert code == 0
    assert len(posted) == 1
    assert "taskvault-mvp" in posted[0]["content"] or posted[0]["task_id"] == "taskvault-mvp"


def test_observer_message_contains_non_authoritative_notice():
    session = ObserverSession(
        provider="shell.online",
        session_id="abc123",
        share_url="https://shell.online/s/abc123#salt=fake",
        read_only=True,
        encrypted=True,
    )
    content = build_observer_announcement(session)
    lowered = content.lower()
    assert "### live execution observer" in lowered
    assert "read_only" in lowered
    assert "e2ee" in lowered
    assert "[open live terminal](https://shell.online/s/abc123#salt=fake)" in lowered
    assert "observational only" in lowered
    assert "use forgeloopbridge for engineer" in lowered
    assert "terminal output is not canonical forgeloop evidence" in lowered


def test_observer_message_contains_no_secret():
    session = project_safe_metadata(dict(FAKE_SAFE_RESPONSE)).session
    content = build_observer_announcement(session, task_id="t1")
    assert FAKE_SECRET not in content
    assert "e2ee_password" not in content
    assert "host token" not in content.lower()


def test_observer_announcement_refuses_unsafe_session():
    bad = ObserverSession(
        provider="shell.online",
        session_id="x",
        share_url="https://shell.online/s/x#salt=y",
        read_only=False,
        encrypted=True,
    )
    with pytest.raises(ObserverError):
        build_observer_announcement(bad)


def test_post_observer_announcement_never_sends_secret(monkeypatch):
    from examples import run_worker_observed as helper

    captured: dict = {}

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        captured["headers"] = dict(request.header_items())
        captured["body"] = json.loads(request.data.decode("utf-8"))
        assert timeout is not None
        return FakeResponse()

    monkeypatch.setattr(helper.urllib.request, "urlopen", fake_urlopen)
    session = project_safe_metadata(dict(FAKE_SAFE_RESPONSE)).session
    content = build_observer_announcement(session)
    assert helper.post_observer_announcement(
        "http://localhost:8000", "worker-token", content, "task-1"
    ) is True
    assert captured["url"] == "http://localhost:8000/api/messages"
    assert captured["body"]["content"] == content
    assert FAKE_SECRET not in json.dumps(captured["body"])
    assert "typed" not in captured["body"]


# ─── Local secret file boundary ──────────────────────────────────────────────


def test_local_secret_file_is_owner_only(tmp_path, monkeypatch):
    monkeypatch.setattr(observer_base, "get_secret_dir", lambda: tmp_path / "observer")
    path = observer_base.write_secret_file("sess-test-001", "https://shell.online/x#salt=y", FAKE_SECRET)
    assert path.exists()
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["e2ee_password"] == FAKE_SECRET
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    observer_base.remove_secret_file(path)
    assert not path.exists()


# ─── Timeouts on every provider subprocess ───────────────────────────────────


def test_all_provider_subprocesses_are_bounded(monkeypatch):
    timeouts: list[float] = []

    def fake_which(cmd):
        return f"/bin/{cmd}"

    class FakeProc:
        returncode = 0
        stdout = "--read-only --json list --json --foreground kill"
        stderr = ""

    def fake_run(argv, **kwargs):
        timeouts.append(float(kwargs.get("timeout", 0) or 0))
        proc = FakeProc()
        if argv[-2:] == ["list", "--json"]:
            proc.stdout = "[]"
        return proc

    monkeypatch.setattr(shell_adapter.shutil, "which", fake_which)
    monkeypatch.setattr(shell_adapter.subprocess, "run", fake_run)
    shell_adapter.clear_probe_cache()
    try:
        shell_adapter.preflight("shell")
        shell_adapter.query_status("missing")
        shell_adapter.stop_session("sess-1")
    finally:
        shell_adapter.clear_probe_cache()
    assert len(timeouts) >= 4
    assert all(0 < timeout <= 60 for timeout in timeouts)


# ─── Authority invariants: nothing changes Bridge/ForgeLoop contracts ────────


def test_typed_schema_v1_is_unchanged():
    from bridge_protocol.models import SUPPORTED_TYPED_SCHEMA_VERSIONS, TYPED_MESSAGE_KINDS

    assert set(TYPED_MESSAGE_KINDS) == {
        "TASK_REQUEST",
        "STATUS_UPDATE",
        "DECISION_REQUEST",
        "DECISION_RESPONSE",
        "DECISION_NOTICE",
        "BLOCKER",
        "REVIEW_RESULT",
        "CONTROL_NOTICE",
        "HANDOFF_NOTICE",
        "VERIFICATION_REPORT",
        "ATTESTATION_REPORT",
    }
    assert tuple(SUPPORTED_TYPED_SCHEMA_VERSIONS) == (1,)


def test_database_schema_is_unchanged():
    text = (ROOT / "main.py").read_text(encoding="utf-8")
    lowered = text.lower()
    for forbidden in (
        "observer",
        "shell.online",
        "share_url",
        "e2ee_password",
        "live_observer",
        "relay_status",
    ):
        assert forbidden not in lowered
    for path in ("bridge_protocol/models.py", "bridge_protocol/validation.py"):
        body = (ROOT / path).read_text(encoding="utf-8").lower()
        for forbidden in ("observer", "shell.online", "share_url", "e2ee"):
            assert forbidden not in body


def test_bridge_rest_and_sse_contracts_are_unchanged():
    text = (ROOT / "main.py").read_text(encoding="utf-8")
    assert 'BRIDGE_API_VERSION = "2.1.3"' in text
    assert '@app.get("/api/messages"' in text
    assert '@app.post("/api/messages"' in text
    assert '@app.get("/api/stream")' in text
    assert '@app.post("/api/stream-ticket")' in text
    assert "/api/observer" not in text
    assert "live_observer" not in text.lower()


def test_no_terminal_recording_or_evidence_claims():
    for relative in (
        "examples/live_observer/base.py",
        "examples/live_observer/shell_online.py",
        "examples/run_worker_observed.py",
    ):
        text = (ROOT / relative).read_text(encoding="utf-8").lower()
        assert "terminal output is not canonical" in text or "not canonical" in text or "observational only" in text
        for forbidden in (
            "terminal-as-evidence",
            "observer evidence",
            "observer verification",
            "verificationstatus valid",
        ):
            assert forbidden not in text
