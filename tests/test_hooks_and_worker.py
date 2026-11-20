import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from examples import worker_poll

ROOT = Path(__file__).resolve().parents[1]


def test_worker_cursor_persistence_generation_reset_and_long_poll(tmp_path, monkeypatch):
    cursor_path = tmp_path / "worker_cursor.json"
    worker_poll.save_cursor_state(cursor_path, 42, "gen-1")
    if os.name != "nt":
        mode = stat.S_IMODE(os.stat(cursor_path).st_mode)
        assert mode == 0o600
    assert worker_poll.load_cursor_state(cursor_path) == (42, "gen-1")

    calls = []

    def fake_fetch(after_id, *, bridge_url, headers, wait_seconds=None, conversation_id=None, generation=None):
        calls.append((after_id, wait_seconds, conversation_id, generation))
        if generation == "gen-1":
            raise worker_poll.GenerationMismatchError("gen-2")
        return [{"id": 5, "role": "master", "message_type": "ORDER", "order_id": "ORD-LP", "content": "Run"}]

    executed = []
    monkeypatch.setattr(worker_poll, "fetch_messages", fake_fetch)
    monkeypatch.setattr(worker_poll, "execute_order", lambda msg, *a, **kw: executed.append(msg["id"]))

    rc = worker_poll.run_worker_loop(
        token="test-token",
        run_mode="once",
        start_mode="now",
        cursor_file=cursor_path,
        transport="long-poll",
        wait_seconds=12.0,
        conversation_id="general",
    )
    assert rc == 0
    assert executed == [5]
    assert calls[0] == (42, 12.0, "general", "gen-1")
    assert calls[1] == (0, 12.0, "general", "gen-2")
    assert worker_poll.load_cursor_state(cursor_path) == (5, "gen-2")


def test_node_agent_hooks_capture_throttle_urgent_and_stop_caps(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for hook script verification")

    script = f"""
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {{ pathToFileURL }} from 'node:url';
const {{ handleHookEvent, statePathFor }} = await import(pathToFileURL({json.dumps(str(ROOT / "integrations" / "hooks" / "core.mjs"))}).href);

const tmpHome = {json.dumps(str(tmp_path))};
const env = {{ XDG_STATE_HOME: tmpHome, BRIDGE_HOOK_MAX_CONTINUATIONS: '2', BRIDGE_URL: 'http://127.0.0.1:8000' }};
const statePath = statePathFor('sess-1', env);

// 1. SessionStart without link suggests setup
const startNoLink = await handleHookEvent({{ hookEventName: 'SessionStart', sessionId: 'sess-1' }}, {{ env, statePath }});
assert.match(startNoLink.context, /create_signal/);

// 2. PostToolUse captures create_signal output with 0600 permissions
const saved = await handleHookEvent({{
  hookEventName: 'PostToolUse',
  sessionId: 'sess-1',
  toolName: 'mcp__forgeloopbridge__create_signal',
  toolResponse: {{ structuredContent: {{ signal_id: 'sig1', url: '/api/signal/sig1', secret: 'sec-xyz', expires_at: 9999999999 }} }}
}}, {{ env, statePath, now: () => 10000 }});
assert.match(saved.context, /signal link saved/);
if (process.platform !== 'win32') {{
  assert.equal(fs.statSync(statePath).mode & 0o777, 0o600);
}}

// 3. PostToolUse throttled within 15s, then checks after 16s; only URGENT speaks mid-turn
let fetchCalls = 0;
let fakeNow = 12000;
const fakeFetchNormal = async (url, init) => {{
  fetchCalls += 1;
  assert.equal(init.headers.Authorization, 'Bearer sec-xyz');
  assert.ok(!url.includes('sec-xyz'));
  return {{ ok: true, status: 200, json: async () => ({{ pending: 2, urgent: 0, waking: 1, more: false }}) }};
}};
const throttled = await handleHookEvent({{ hookEventName: 'PostToolUse', toolName: 'Bash' }}, {{ env, statePath, fetch: fakeFetchNormal, now: () => fakeNow }});
assert.deepEqual(throttled, {{}});
assert.equal(fetchCalls, 0);

fakeNow = 30000;
const nonUrgent = await handleHookEvent({{ hookEventName: 'PostToolUse', toolName: 'Bash' }}, {{ env, statePath, fetch: fakeFetchNormal, now: () => fakeNow }});
assert.deepEqual(nonUrgent, {{}});
assert.equal(fetchCalls, 1);

fakeNow = 50000;
const urgentRes = await handleHookEvent({{ hookEventName: 'PostToolUse', toolName: 'Bash' }}, {{
  env, statePath, now: () => fakeNow,
  fetch: async () => ({{ ok: true, status: 200, json: async () => ({{ pending: 1, urgent: 1, waking: 1, more: false }}) }})
}});
assert.match(urgentRes.context, /1 urgent message/);

// 4. Stop continues only when waking > 0, up to max continuations (2)
const stopNonWaking = await handleHookEvent({{ hookEventName: 'Stop' }}, {{
  env, statePath, now: () => 55000,
  fetch: async () => ({{ ok: true, status: 200, json: async () => ({{ pending: 3, urgent: 0, waking: 0, more: false }}) }})
}});
assert.deepEqual(stopNonWaking, {{}});

const stopWaking1 = await handleHookEvent({{ hookEventName: 'Stop' }}, {{
  env, statePath, now: () => 56000,
  fetch: async () => ({{ ok: true, status: 200, json: async () => ({{ pending: 2, urgent: 0, waking: 1, more: false }}) }})
}});
assert.match(stopWaking1.continueWith, /2 new messages/);

const stopWaking2 = await handleHookEvent({{ hookEventName: 'Stop' }}, {{
  env, statePath, now: () => 57000,
  fetch: async () => ({{ ok: true, status: 200, json: async () => ({{ pending: 1, urgent: 0, waking: 1, more: false }}) }})
}});
assert.match(stopWaking2.continueWith, /1 new message/);

const stopCapped = await handleHookEvent({{ hookEventName: 'Stop' }}, {{
  env, statePath, now: () => 58000,
  fetch: async () => ({{ ok: true, status: 200, json: async () => ({{ pending: 1, urgent: 0, waking: 1, more: false }}) }})
}});
assert.deepEqual(stopCapped, {{}});

// 5. Network failure fails open and sets backoff
const netFail = await handleHookEvent({{ hookEventName: 'UserPromptSubmit' }}, {{
  env, statePath, now: () => 60000,
  fetch: async () => {{ throw new Error('offline'); }}
}});
assert.deepEqual(netFail, {{}});

// 6. revoke_signal deletes state file
await handleHookEvent({{ hookEventName: 'PostToolUse', toolName: 'revoke_signal' }}, {{ env, statePath }});
assert.equal(fs.existsSync(statePath), false);
console.log('HOOKS_VERIFIED');
"""
    proc = subprocess.run(
        [node, "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        check=True,
        timeout=15,
    )
    assert "HOOKS_VERIFIED" in proc.stdout

    # Verify CLI entrypoint exits 0 cleanly on Stop when no state exists
    cli = subprocess.run(
        [node, str(ROOT / "integrations" / "hooks" / "bridge-hook.mjs"), "--host", "claude"],
        input=json.dumps({"hookEventName": "Stop", "sessionId": "empty-sess"}),
        env={**os.environ, "XDG_STATE_HOME": str(tmp_path)},
        capture_output=True,
        text=True,
        check=True,
        timeout=15,
    )
    assert cli.returncode == 0
