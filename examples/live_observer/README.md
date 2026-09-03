# Live Execution Observer (optional shell.online integration)

An optional, lazy/opt-in, read-only live execution observer. An Engineer can
watch a Worker terminal in a browser while the Worker performs real work.

```text
Engineer ──coordination──▶ ForgeLoopBridge ◀──coordination── Worker
                                                  ├──▶ ForgeLoop (canonical authority)
                                                  └──▶ shell.online (optional read-only live observer) ──▶ Engineer
```

The Bridge records coordination, ForgeLoop owns engineering truth, and
shell.online may let an Engineer watch execution without becoming part of
either authority boundary.

## Non-negotiable boundary

- ForgeLoopBridge: coordination transport only (Markdown, Typed Schema v1,
  REST, SSE, auth, persistence, filtering, correlation, idempotency, cursor
  recovery, SQLite, coordination status).
- ForgeLoop: sole canonical engineering authority (lifecycle, claims,
  ownership, recovery, evidence, verification, completion, approvals,
  actions, workspace binding, handoffs, responsibility, scope, attestation,
  structural quality, canonical `.forgeloop/` state).
- shell.online: live PTY sharing only (browser observation, session
  transport). shell.online output is not canonical evidence, its state is
  not Bridge task state and is not ForgeLoop lifecycle state.

## Security position

Disabled by default, opt-in, read-only only in Phase 1, E2EE required,
non-authoritative, non-evidence, no Bridge persistence of provider secrets.
Interactive browser control is unsupported: it would create a second
operational channel bypassing Engineer → Bridge → Worker coordination.

## Configuration

```text
FORGEBRIDGE_LIVE_OBSERVER=none          # none | shell-online
FORGEBRIDGE_LIVE_OBSERVER_COMMAND=shell # provider executable (no auto-install)
```

Phase 1 access mode is always `READ_ONLY`. There is no interactive option.

## Provider behavior (re-verified before coding)

- Command/PTY stays on the Worker machine; sessions may be interactive or
  read-only; `--read-only` blocks browser input and is fixed per session.
- Terminal payloads are E2EE by default; `--json` produces structured
  session metadata; `shell list --json` exposes relay status (`online`,
  `reconnecting`, `expired`, `unknown`).
- Share URL + E2EE password together form access credentials; task-bound
  sessions close when the wrapped command exits.
- Upstream references: `https://shell.online/`,
  `https://github.com/TeoSlayer/shell.online`, `shell --version`,
  `shell help reference`. Do not depend on undocumented internals.

## Helper

`examples/run_worker_observed.py` is a launcher around one Worker command.
It reads observer config, probes the provider with bounded timeouts,
builds `shell --read-only --json --foreground -- <worker-command>`,
parses provider metadata, enforces read-only + E2EE, keeps the share URL
only, posts one Bridge announcement, preserves the Worker exit code, and
cleans up only its own session.

```bash
python examples/run_worker_observed.py \
  --provider shell-online \
  --bridge-url http://localhost:8000 \
  --worker-token-env WORKER_TOKEN \
  --task-id taskvault-mvp \
  -- codex exec --ephemeral ...
```

`--foreground` is required so the orchestrator still observes the real
Worker exit (default background detaches). Without advertised
`--foreground` support the helper runs the Worker directly (fail-open).

Upstream distributes the CLI for macOS and Linux; where provider support
is not verified, the observer stays unsupported there and the Worker runs
directly without it.

## Credential boundary

ForgeLoopBridge never stores the E2EE password: provider metadata is
parsed, the share URL is kept, and the password is discarded immediately.
The password never appears in Bridge SQLite, messages, typed messages, SSE
history, logs, reports, Git, or snapshots. shell.online retains its own
owner-side session record; operators retrieve the password locally with
`shell list`.

## Failure policy

Pre-start provider failure fails open: the Worker runs directly, exactly
once, and the observer is reported unavailable. Post-start security
violation (non-read-only session, unencrypted session, or invalid share
URL) fails closed: the unsafe session is stopped with
`shell kill <session-id>`, the invocation is terminated boundedly with a
non-zero exit, and the Worker command is never executed a second time.

Task-bound observer sessions normally close with the Worker process.
Targeted `shell kill <session-id>` is reserved for exceptional cleanup
(security violations, interrupts, signals, abnormal termination) and never
uses `--all`.

## Announcement

One Markdown message per Worker invocation over the existing POST path
(no Typed Schema change):

```markdown
### Live Execution Observer

The current Worker turn can be observed live.

- Provider: `shell.online`
- Access: `READ_ONLY`
- Encryption: `E2EE`
- Session: `abc123`
- [Open live terminal](https://...)

This terminal is observational only.

Use ForgeLoopBridge for Engineer ↔ Worker instructions.
Terminal output is not canonical ForgeLoop evidence.
```

## Status and cleanup

`shell list --json` reconciliation is diagnostic only (`expired` is not
Worker failure, `unknown` is not proof of death, `online` is not Worker
health). Task-bound sessions normally close with the Worker, so no kill is
issued on a normal exit; exceptional cleanup runs `shell kill <session-id>`
for the created session only (never `--all`). All subprocesses use argv
arrays, `shell=False`, and bounded timeouts.

## Lifetime

`observer lifetime <= Worker-turn lifetime` (plus bounded cleanup). The
observer never keeps the Worker alive; each invocation gets its own
session and fresh resume uses Bridge + ForgeLoop, not scrollback.

## Privacy

Live observation can expose source code, commands, paths, tests, runtime
output, and accidentally printed credentials. E2EE protects relay content
from plaintext inspection but does not make sensitive content safe to
share indiscriminately. Observer mode stays intentional opt-in. No
terminal recording: nothing is copied into Bridge SQLite.
