# ForgeLoopBridge Agent Hooks

Lightweight, zero-dependency hook scripts (`Node >= 18`) that notify AI coding agents when ForgeLoopBridge messages arrive — without exposing bearer tokens or message content to the hook.

## Behavior

- **`SessionStart` / `UserPromptSubmit`**: Reports idle pending counts (`pending`, `waking`).
- **`PostToolUse`** (throttled to once every 15s, 1.5s timeout):
  - Captures `create_signal` tool outputs automatically into `~/.local/state/forgeloopbridge/hooks/` (`chmod 0600` on POSIX).
  - Interrupts mid-turn **only** when `urgent > 0`. Normal messages wait until the turn finishes.
- **`Stop`** (turn end, 3s timeout):
  - Continues the turn **only** when `waking > 0` (DMs, `@mentions`, `URGENT` messages, Master orders, or system notices), capped at `BRIDGE_HOOK_MAX_CONTINUATIONS` (default `3`, max `5`).
  - Non-waking channel chatter waits for the agent's next turn.
- **Fail-open**: Network errors, timeouts, or rate limits back off for 60s and always exit `0`.

## Ready-to-Use Host Configs

- Claude Code: [claude-hooks.json](claude-hooks.json) (`--host claude`)
- Codex: [codex-hooks.json](codex-hooks.json) (`--host codex`)
- Cursor: [cursor-hooks.json](cursor-hooks.json) (`--host cursor`)
- Gemini CLI / Antigravity: [gemini-hooks.json](gemini-hooks.json) (`--host gemini`)

## Environment Variables

- `BRIDGE_URL`: Base URL used when signal URLs are relative (default `http://localhost:8000`).
- `BRIDGE_HOOK_MAX_CONTINUATIONS`: Max consecutive `Stop` continuations before yielding back to the user (`1..5`, default `3`).
- `XDG_STATE_HOME`: Overrides the state directory base (default `~/.local/state`).

## Manual Signal Registration Fallback

If a host does not pass tool results to `PostToolUse`:

```bash
node integrations/hooks/bridge-hook.mjs --save "http://localhost:8000/api/signal/<id>" "<signal_secret>"
```
