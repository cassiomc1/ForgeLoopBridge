"""Live Execution Observer tests (Phase 1: shell.online, read-only, E2EE).

CI must not contact the real shell.online provider: every provider
subprocess is mocked or backed by a local fake executable. No test uses
the network; Bridge POSTs go to a local loopback capture server.
"""

from __future__ import annotations

import io
import json
import os
import queue
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from examples.live_observer import base as observer_base
from examples.live_observer import shell_online as shell_adapter
from examples.live_observer.base import (
    ObserverError,
    ObserverSession,
    build_observer_announcement,
    extract_session_id,
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
    is_provider_metadata_dict,
    parse_list_output,
    parse_start_output,
    query_status,
    stop_session,
    try_parse_json_line,
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


def test_stderr_handling_uses_single_blocking_reader():
    text = (ROOT / "examples" / "run_worker_observed.py").read_text(encoding="utf-8")
    assert "_StderrPump" in text
    assert "queue.Queue" in text
    assert "put_nowait" in text
    # The old nonblocking pipe design must be gone.
    assert "fcntl" not in text
    assert "O_NONBLOCK" not in text
    assert "select.select" not in text
    assert text.count("class _StderrPump(") == 1
    assert text.count("pump = _StderrPump(") == 1


def test_bridge_never_persists_password_by_construction():
    base_text = (ROOT / "examples" / "live_observer" / "base.py").read_text(encoding="utf-8")
    helper_text = (ROOT / "examples" / "run_worker_observed.py").read_text(encoding="utf-8")
    adapter_text = (ROOT / "examples" / "live_observer" / "shell_online.py").read_text(
        encoding="utf-8"
    )
    for forbidden in (
        "write_secret_file",
        "get_secret_dir",
        "remove_secret_file",
        "forgeloopbridge-observer",
        "ParsedObserverStart",
    ):
        assert forbidden not in base_text
        assert forbidden not in helper_text
        assert forbidden not in adapter_text
    assert not hasattr(observer_base, "write_secret_file")
    assert not hasattr(observer_base, "get_secret_dir")
    assert not hasattr(observer_base, "remove_secret_file")
    assert not hasattr(observer_base, "ParsedObserverStart")


# ─── Safe session model + allow-list projection ──────────────────────────────


def test_shell_online_safe_metadata_projection_discards_password():
    session = project_safe_metadata(dict(FAKE_SAFE_RESPONSE))
    assert isinstance(session, observer_base.ObserverSession)
    assert session.provider == "shell.online"
    assert session.session_id == "sess-test-001"
    assert session.share_url == "https://shell.online/example#salt=fake"
    assert session.read_only is True
    assert session.encrypted is True
    # Allow-list only: no background flag, no password, no raw JSON retention.
    assert not hasattr(session, "e2ee_password")
    assert not hasattr(session, "background")
    assert FAKE_SECRET not in repr(session)


def test_shell_online_password_never_reaches_safe_surfaces():
    session = project_safe_metadata(dict(FAKE_SAFE_RESPONSE))
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


def test_extract_session_id_is_opaque_and_total():
    assert extract_session_id(dict(FAKE_SAFE_RESPONSE)) == "sess-test-001"
    assert extract_session_id({"session_id": "  padded  "}) == "padded"
    assert extract_session_id({"read_only": True}) is None
    assert extract_session_id({"session_id": ""}) is None
    assert extract_session_id({"session_id": "has\nnewline"}) is None
    assert extract_session_id({"session_id": 42}) is None
    assert extract_session_id(None) is None
    assert extract_session_id("sess-test-001") is None


def test_shell_online_rejects_interactive_session(monkeypatch):
    payload = dict(FAKE_SAFE_RESPONSE, read_only=False)
    with pytest.raises(ObserverError) as excinfo:
        project_safe_metadata(payload)
    assert excinfo.value.code == "OBSERVER_UNSAFE_ACCESS_MODE"
    # Identity is still extractable for targeted cleanup.
    assert extract_session_id(payload) == "sess-test-001"
    with pytest.raises(ObserverError) as excinfo2:
        parse_start_output("", json.dumps(payload))
    assert excinfo2.value.code == "OBSERVER_UNSAFE_ACCESS_MODE"


def test_shell_online_rejects_unencrypted_session():
    payload = dict(FAKE_SAFE_RESPONSE, encrypted=False)
    with pytest.raises(ObserverError) as excinfo:
        project_safe_metadata(payload)
    assert excinfo.value.code == "OBSERVER_ENCRYPTION_DISABLED"
    assert extract_session_id(payload) == "sess-test-001"


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
    # Identity remains available for targeted cleanup of the unsafe session.
    assert extract_session_id(payload) == "sess-test-001"


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
    stderr = "some log noise\n" + json.dumps(dict(FAKE_SAFE_RESPONSE)) + "\nmore noise\n"
    session = parse_start_output("stdout noise", stderr)
    assert session.session_id == "sess-test-001"
    assert not hasattr(session, "e2ee_password")


def test_metadata_line_detection():
    assert try_parse_json_line('{"session_id": "a"}') == {"session_id": "a"}
    assert try_parse_json_line("  plain log line  ") is None
    assert try_parse_json_line("{not json") is None
    assert try_parse_json_line('["array"]') is None
    assert is_provider_metadata_dict({"session_id": "a"}) is True
    assert is_provider_metadata_dict({"share_url": "https://shell.online/x"}) is True
    assert is_provider_metadata_dict({"read_only": True, "encrypted": True}) is True
    assert is_provider_metadata_dict({"level": "info", "msg": "worker log"}) is False
    assert is_provider_metadata_dict(["not", "a", "dict"]) is False
    assert is_provider_metadata_dict(None) is False


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
    assert helper._normalize_exit_code(None) == 1


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


def test_terminate_bounded_escalates_then_gives_up():
    from examples import run_worker_observed as helper

    class HangThenExit:
        calls: list[str]

        def __init__(self):
            self.calls = []

        def terminate(self):
            self.calls.append("terminate")

        def kill(self):
            self.calls.append("kill")

        def wait(self, timeout=None):
            if "kill" not in self.calls:
                raise subprocess.TimeoutExpired("cmd", timeout)
            return 9

    proc = HangThenExit()
    assert helper._terminate_bounded(proc, grace=1) == 9
    assert proc.calls == ["terminate", "kill"]

    class RefusesToDie:
        def terminate(self):
            pass

        def kill(self):
            pass

        def wait(self, timeout=None):
            raise subprocess.TimeoutExpired("cmd", timeout)

    assert helper._terminate_bounded(RefusesToDie(), grace=1) is None


# ─── Single blocking stderr reader ───────────────────────────────────────────


def test_stderr_pump_delivers_metadata_and_forwards_rest():
    from examples import run_worker_observed as helper

    read_fd, write_fd = os.pipe()
    forwarded = io.StringIO()
    metadata_queue: queue.Queue = queue.Queue(maxsize=1)
    with os.fdopen(read_fd, "r", encoding="utf-8") as reader:
        pump = helper._StderrPump(reader, metadata_queue, forwarded)
        pump.start()

        def _write_lines():
            with os.fdopen(write_fd, "w", encoding="utf-8") as writer:
                writer.write("provider booting\n")
                writer.write(json.dumps(dict(FAKE_SAFE_RESPONSE)) + "\n")
                for i in range(3000):
                    writer.write(f"worker line {i}\n")

        writer_thread = threading.Thread(target=_write_lines)
        writer_thread.start()
        writer_thread.join(timeout=10)
        assert not writer_thread.is_alive()
        metadata = metadata_queue.get(timeout=10)
        pump.join(timeout=10)
        assert not pump.is_alive()

    assert json.loads(metadata)["session_id"] == "sess-test-001"
    body = forwarded.getvalue()
    # Metadata line (holding the password) is never forwarded.
    assert FAKE_SECRET not in body
    assert "share_url" not in body
    # Everything else is forwarded intact.
    assert "provider booting\n" in body
    assert body.count("worker line ") == 3000


# ─── Fake provider for real-subprocess regression tests ──────────────────────

FAKE_SHELL_SCRIPT = """\
#!/usr/bin/env python3
import json
import os
import sys

args = sys.argv[1:]
record_path = os.environ.get("FAKE_SHELL_RECORD")

def _record(text):
    if record_path:
        with open(record_path, "a", encoding="utf-8") as handle:
            handle.write(text + "\\n")

if args[:1] == ["--version"]:
    _record("version")
    print("shell version 0.7.3-test")
    sys.exit(0)
if args[:2] == ["help", "reference"]:
    _record("help")
    print("--read-only --json list --json --foreground kill attach")
    sys.exit(0)
if args[:2] == ["list", "--json"]:
    _record("list")
    print("[]")
    sys.exit(0)
if args[:1] == ["kill"]:
    _record("kill " + " ".join(args[1:]))
    sys.exit(0)
metadata = os.environ.get("FAKE_SHELL_METADATA", "none")
if metadata != "none":
    sys.stderr.write(metadata + "\\n")
    sys.stderr.flush()
separator = args.index("--")
_record("wrap")
os.execvp(args[separator + 1], args[separator + 1 :])
"""

COUNTER_WORKER = (
    "import sys;"
    "open(sys.argv[1], 'a').write('x');"
    "sys.exit(int(sys.argv[2]))"
)

NOISY_WORKER = (
    "import sys;"
    "open(sys.argv[1], 'a').write('x');"
    "total = int(sys.argv[2]);"
    "code = int(sys.argv[3]);"
    "write = sys.stderr.write;"
    "[write('w-line-%d:' % i + 'y' * 980 + '\\n') for i in range(total)];"
    "sys.stderr.flush();"
    "sys.exit(code)"
)


@pytest.fixture
def fake_shell(tmp_path):
    path = tmp_path / "fake-shell"
    path.write_text(FAKE_SHELL_SCRIPT, encoding="utf-8")
    path.chmod(0o755)
    return str(path)


# The real shell.online CLI ships for macOS/Linux only (no Windows build),
# and only POSIX execs a shebang script without an extension. On other
# platforms the provider is unsupported and the helper correctly fails open
# to a direct Worker run (covered by test_pre_start_provider_failure).
needs_posix_shell = pytest.mark.skipif(
    os.name != "posix",
    reason="shell.online provider subprocess tests require a POSIX executable",
)


@pytest.fixture
def bridge_capture():
    posts: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            posts.append(json.loads(self.rfile.read(length)))
            data = b'{"id": 1}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", posts
    server.shutdown()
    server.server_close()


def _run_helper(cmd, env_extra, timeout=60):
    env = dict(os.environ)
    env.update(env_extra)
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=ROOT,
        env=env,
    )


def _helper_base(fake_shell, bridge_url, task_id="task-1"):
    return [
        sys.executable,
        "examples/run_worker_observed.py",
        "--provider",
        "shell-online",
        "--shell-command",
        fake_shell,
        "--bridge-url",
        bridge_url,
        "--worker-token-env",
        "WORKER_TOKEN",
        "--task-id",
        task_id,
        "--metadata-timeout",
        "10",
        "--",
    ]


def _read_record(path: Path) -> list[str]:
    if not path.exists():
        return []
    return path.read_text(encoding="utf-8").split()


def _counter_runs(path: Path) -> int:
    # Fail-closed termination may land while the Worker interpreter is still
    # starting, so post-start runs prove at-most-once (never twice), while
    # pre-start direct runs prove exactly-once.
    return len(path.read_text(encoding="utf-8")) if path.exists() else 0


@needs_posix_shell
def test_observed_success_posts_once_and_preserves_exit(
    fake_shell, bridge_capture, tmp_path
):
    bridge_url, posts = bridge_capture
    counter = tmp_path / "counter"
    record = tmp_path / "record"
    result = _run_helper(
        _helper_base(fake_shell, bridge_url)
        + [sys.executable, "-c", COUNTER_WORKER, str(counter), "0"],
        {
            "WORKER_TOKEN": "test-token",
            "FAKE_SHELL_METADATA": json.dumps(dict(FAKE_SAFE_RESPONSE)),
            "FAKE_SHELL_RECORD": str(record),
        },
    )
    combined = result.stdout + result.stderr
    assert result.returncode == 0
    assert counter.read_text(encoding="utf-8") == "x"
    # Exactly one Bridge announcement, carrying the URL but never the secret.
    assert len(posts) == 1
    assert "https://shell.online/example#salt=fake" in posts[0]["content"]
    assert FAKE_SECRET not in json.dumps(posts)
    assert "typed" not in posts[0]
    # Password never reaches helper output; metadata line is not forwarded.
    assert FAKE_SECRET not in combined
    assert "share_url" not in result.stderr
    assert "Live observer started." in result.stdout
    assert "shell list" in result.stdout
    # End-of-turn targeted cleanup of the created session only.
    record_lines = _read_record(record)
    assert record_lines.count("wrap") == 1
    assert "kill" in record_lines
    assert record_lines[record_lines.index("kill") + 1] == "sess-test-001"
    assert "--all" not in record_lines
    # No password file is created anywhere by the helper run.
    assert not Path("/tmp/forgeloopbridge-observer").exists()


@needs_posix_shell
def test_observed_worker_exit_code_preserved_through_wrapper(
    fake_shell, bridge_capture, tmp_path
):
    bridge_url, _ = bridge_capture
    counter = tmp_path / "counter"
    result = _run_helper(
        _helper_base(fake_shell, bridge_url)
        + [sys.executable, "-c", COUNTER_WORKER, str(counter), "4"],
        {
            "WORKER_TOKEN": "test-token",
            "FAKE_SHELL_METADATA": json.dumps(dict(FAKE_SAFE_RESPONSE)),
            "FAKE_SHELL_RECORD": str(tmp_path / "record"),
        },
    )
    assert result.returncode == 4
    assert counter.read_text(encoding="utf-8") == "x"


@needs_posix_shell
def test_large_stderr_stream_does_not_deadlock(fake_shell, bridge_capture, tmp_path):
    bridge_url, _ = bridge_capture
    counter = tmp_path / "counter"
    result = _run_helper(
        _helper_base(fake_shell, bridge_url)
        + [sys.executable, "-c", NOISY_WORKER, str(counter), "1500", "0"],
        {
            "WORKER_TOKEN": "test-token",
            "FAKE_SHELL_METADATA": json.dumps(dict(FAKE_SAFE_RESPONSE)),
            "FAKE_SHELL_RECORD": str(tmp_path / "record"),
        },
        timeout=90,
    )
    assert result.returncode == 0
    assert counter.read_text(encoding="utf-8") == "x"
    # >1 MiB of Worker stderr drained continuously and forwarded.
    assert len(result.stderr.encode("utf-8")) > 1024 * 1024
    assert result.stderr.count("w-line-") == 1500
    assert FAKE_SECRET not in result.stdout + result.stderr


@needs_posix_shell
@pytest.mark.parametrize(
    ("mutate", "reason"),
    (
        ({"read_only": False}, "interactive"),
        ({"encrypted": False}, "unencrypted"),
        ({"share_url": "https://evil.example/"}, "invalid-url"),
    ),
)
def test_post_start_security_failure_is_fail_closed(
    fake_shell, bridge_capture, tmp_path, mutate, reason
):
    bridge_url, posts = bridge_capture
    counter = tmp_path / "counter"
    record = tmp_path / "record"
    payload = dict(FAKE_SAFE_RESPONSE, session_id="sess-test", **mutate)
    result = _run_helper(
        _helper_base(fake_shell, bridge_url)
        + [sys.executable, "-c", COUNTER_WORKER, str(counter), "0"],
        {
            "WORKER_TOKEN": "test-token",
            "FAKE_SHELL_METADATA": json.dumps(payload),
            "FAKE_SHELL_RECORD": str(record),
        },
    )
    assert result.returncode != 0, reason
    # Targeted cleanup of the unsafe session that was actually created.
    record_lines = _read_record(record)
    assert record_lines.count("wrap") == 1, reason
    assert "kill" in record_lines, reason
    assert record_lines[record_lines.index("kill") + 1] == "sess-test", reason
    assert "--all" not in record_lines
    # Nothing published; Worker executed at most once (no rerun fallback).
    assert posts == [], reason
    assert _counter_runs(counter) <= 1, reason
    assert FAKE_SECRET not in result.stdout + result.stderr


@needs_posix_shell
def test_security_failure_without_session_id_terminates_without_global_kill(
    fake_shell, bridge_capture, tmp_path
):
    bridge_url, posts = bridge_capture
    counter = tmp_path / "counter"
    record = tmp_path / "record"
    payload = {
        "read_only": True,
        "encrypted": True,
        "share_url": "https://shell.online/s/orphan#salt=y",
    }
    result = _run_helper(
        _helper_base(fake_shell, bridge_url)
        + [sys.executable, "-c", COUNTER_WORKER, str(counter), "0"],
        {
            "WORKER_TOKEN": "test-token",
            "FAKE_SHELL_METADATA": json.dumps(payload),
            "FAKE_SHELL_RECORD": str(record),
        },
    )
    assert result.returncode != 0
    assert "OBSERVER_SECURITY_VALIDATION_FAILED" in result.stdout + result.stderr
    # No session id to clean; never a global cleanup command.
    record_lines = _read_record(record)
    assert record_lines.count("wrap") == 1
    assert "kill" not in record_lines
    assert "--all" not in record_lines
    assert posts == []
    assert _counter_runs(counter) <= 1


def test_pre_start_provider_failure_runs_worker_directly_once(tmp_path):
    counter = tmp_path / "counter"
    result = _run_helper(
        [
            sys.executable,
            "examples/run_worker_observed.py",
            "--provider",
            "shell-online",
            "--shell-command",
            "/nonexistent-shell-binary",
            "--",
            sys.executable,
            "-c",
            COUNTER_WORKER,
            str(counter),
            "5",
        ],
        {},
    )
    assert result.returncode == 5
    assert counter.read_text(encoding="utf-8") == "x"
    assert "observer unavailable" in result.stdout + result.stderr


# ─── Bridge announcement (once, non-authoritative, no secret) ────────────────


def test_observer_message_posted_once_mocked(monkeypatch):
    from examples import run_worker_observed as helper

    posted: list[dict] = []
    stopped: list[str] = []

    class FakeStderr:
        def __init__(self, lines):
            self._lines = iter(lines)

        def __iter__(self):
            return self

        def __next__(self):
            return next(self._lines)

    class FakeProc:
        def __init__(self):
            self.stderr = FakeStderr([json.dumps(dict(FAKE_SAFE_RESPONSE)) + "\n"])
            self.returncode = 0

        def wait(self, timeout=None):
            return 0

        def terminate(self):
            pass

        def kill(self):
            pass

    monkeypatch.setattr(
        helper, "preflight", lambda cmd=None: {"supports_read_only": True, "supports_json": True}
    )
    monkeypatch.setattr(
        helper,
        "build_wrapper_argv",
        lambda worker, cmd=None, foreground=True: ["shell-mock", *worker],
    )
    monkeypatch.setattr(helper.subprocess, "Popen", lambda *args, **kwargs: FakeProc())
    monkeypatch.setattr(helper, "stop_session", lambda session_id, cmd=None: stopped.append(session_id))

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
        metadata_timeout=5.0,
    )
    assert code == 0
    assert len(posted) == 1
    assert "taskvault-mvp" in posted[0]["content"] or posted[0]["task_id"] == "taskvault-mvp"
    assert stopped == ["sess-test-001"]


def test_fail_closed_never_reruns_worker_mocked(monkeypatch, capsys):
    from examples import run_worker_observed as helper

    stopped: list[str] = []
    direct_runs: list[list[str]] = []

    class FakeStderr:
        def __init__(self, lines):
            self._lines = iter(lines)

        def __iter__(self):
            return self

        def __next__(self):
            return next(self._lines)

    class FakeProc:
        def __init__(self):
            payload = dict(FAKE_SAFE_RESPONSE, read_only=False, session_id="sess-test")
            self.stderr = FakeStderr([json.dumps(payload) + "\n"])
            self.returncode = 0
            self.terminated = False

        def wait(self, timeout=None):
            return 0

        def terminate(self):
            self.terminated = True

        def kill(self):
            pass

    monkeypatch.setattr(
        helper, "preflight", lambda cmd=None: {"supports_read_only": True, "supports_json": True}
    )
    monkeypatch.setattr(
        helper,
        "build_wrapper_argv",
        lambda worker, cmd=None, foreground=True: ["shell-mock", *worker],
    )
    monkeypatch.setattr(helper.subprocess, "Popen", lambda *args, **kwargs: FakeProc())
    monkeypatch.setattr(helper, "stop_session", lambda session_id, cmd=None: stopped.append(session_id))
    monkeypatch.setattr(
        helper, "run_direct", lambda command: direct_runs.append(command) or 99
    )
    posted: list[dict] = []
    monkeypatch.setattr(
        helper,
        "post_observer_announcement",
        lambda *args: posted.append(args) or True,
    )
    code = helper.run_observed(
        [sys.executable, "-c", "print(1)"],
        shell_cmd="shell",
        bridge_url="http://localhost:8000",
        worker_token="worker-token",
        task_id=None,
        metadata_timeout=5.0,
    )
    assert code != 0
    assert stopped == ["sess-test"]
    assert direct_runs == []
    assert posted == []
    assert FAKE_SECRET not in capsys.readouterr().out


def test_metadata_timeout_keeps_worker_result_mocked(monkeypatch):
    from examples import run_worker_observed as helper

    class BlockingStderr:
        def __iter__(self):
            return self

        def __next__(self):
            threading.Event().wait(30)
            raise StopIteration

    class FakeProc:
        def __init__(self):
            self.stderr = BlockingStderr()

        def wait(self, timeout=None):
            return 5

        def terminate(self):
            pass

        def kill(self):
            pass

    monkeypatch.setattr(
        helper, "preflight", lambda cmd=None: {"supports_read_only": True, "supports_json": True}
    )
    monkeypatch.setattr(
        helper,
        "build_wrapper_argv",
        lambda worker, cmd=None, foreground=True: ["shell-mock", *worker],
    )
    monkeypatch.setattr(helper.subprocess, "Popen", lambda *args, **kwargs: FakeProc())
    monkeypatch.setattr(
        helper, "run_direct", lambda command: (_ for _ in ()).throw(AssertionError("rerun"))
    )
    code = helper.run_observed(
        [sys.executable, "-c", "print(1)"],
        shell_cmd="shell",
        bridge_url="http://localhost:8000",
        worker_token=None,
        task_id=None,
        metadata_timeout=0.2,
    )
    assert code == 5


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
    session = project_safe_metadata(dict(FAKE_SAFE_RESPONSE))
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
    session = project_safe_metadata(dict(FAKE_SAFE_RESPONSE))
    content = build_observer_announcement(session)
    assert helper.post_observer_announcement(
        "http://localhost:8000", "worker-token", content, "task-1"
    ) is True
    assert captured["url"] == "http://localhost:8000/api/messages"
    assert captured["body"]["content"] == content
    assert FAKE_SECRET not in json.dumps(captured["body"])
    assert "typed" not in captured["body"]


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
