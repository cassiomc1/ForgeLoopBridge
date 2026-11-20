import crypto from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

export const TOOL_CHECK_MIN_INTERVAL_MS = 15_000;
export const BACKOFF_ON_ERROR_MS = 60_000;
export const DEFAULT_MAX_CONTINUATIONS = 3;
export const MAX_ALLOWED_CONTINUATIONS = 5;

/**
 * Resolve XDG-compliant private state directory outside the project workspace.
 */
export function resolveStateDir(env = process.env) {
  const base =
    env.XDG_STATE_HOME && env.XDG_STATE_HOME.trim()
      ? env.XDG_STATE_HOME.trim()
      : path.join(os.homedir(), ".local", "state");
  return path.join(base, "forgeloopbridge", "hooks");
}

export function statePathFor(sessionKey = "default", env = process.env) {
  const hash = crypto
    .createHash("sha256")
    .update(String(sessionKey || "default"))
    .digest("hex")
    .slice(0, 24);
  return path.join(resolveStateDir(env), `${hash}.json`);
}

export function loadState(filePath) {
  try {
    const raw = fs.readFileSync(filePath, "utf8");
    const parsed = JSON.parse(raw);
    return parsed && typeof parsed === "object" ? parsed : null;
  } catch {
    return null;
  }
}

export function saveState(filePath, state) {
  const dir = path.dirname(filePath);
  fs.mkdirSync(dir, { recursive: true, mode: 0o700 });
  const tmpPath = `${filePath}.${process.pid}.${Date.now()}.tmp`;
  fs.writeFileSync(tmpPath, JSON.stringify(state, null, 2) + "\n", { mode: 0o600 });
  if (process.platform !== "win32") {
    try {
      fs.chmodSync(tmpPath, 0o600);
    } catch {
      // ignore chmod failures on filesystems without POSIX permissions
    }
  }
  fs.renameSync(tmpPath, filePath);
}

export function forgetState(filePath) {
  try {
    fs.unlinkSync(filePath);
  } catch {
    // ignore missing file
  }
}

export function maxContinuationsFromEnv(env = process.env) {
  const raw = Number(env.BRIDGE_HOOK_MAX_CONTINUATIONS ?? DEFAULT_MAX_CONTINUATIONS);
  if (!Number.isFinite(raw)) return DEFAULT_MAX_CONTINUATIONS;
  return Math.max(1, Math.min(MAX_ALLOWED_CONTINUATIONS, Math.floor(raw)));
}

export function resolveSignalUrl(signalUrl, baseUrl = "http://localhost:8000") {
  const cleanUrl = String(signalUrl || "").trim();
  if (!cleanUrl) return "";
  if (cleanUrl.startsWith("http://") || cleanUrl.startsWith("https://")) {
    return cleanUrl;
  }
  const origin = String(baseUrl || "http://localhost:8000").replace(/\/+$/, "");
  return `${origin}${cleanUrl.startsWith("/") ? "" : "/"}${cleanUrl}`;
}

function tryParseJson(value) {
  if (value && typeof value === "object") return value;
  if (typeof value !== "string") return null;
  try {
    return JSON.parse(value);
  } catch {
    return null;
  }
}

export function extractSignalFromToolResult(toolName, toolResponse) {
  if (!toolName || !String(toolName).endsWith("create_signal")) return null;
  let obj = tryParseJson(toolResponse);
  if (obj && obj.structuredContent && typeof obj.structuredContent === "object") {
    obj = obj.structuredContent;
  } else if (obj && Array.isArray(obj.content)) {
    const firstText = obj.content.find((item) => item && item.type === "text" && item.text);
    if (firstText) obj = tryParseJson(firstText.text);
  }
  if (!obj || typeof obj !== "object") return null;

  const url = obj.url;
  const secret = obj.secret;
  if (typeof url !== "string" || !url || typeof secret !== "string" || !secret) {
    return null;
  }
  return {
    signalId: obj.signal_id || obj.signalId || null,
    url,
    secret,
    expiresAt: typeof obj.expires_at === "number" ? obj.expires_at : Number(obj.expiresAt || 0) || null,
    afterId: typeof obj.after_id === "number" ? obj.after_id : 0,
  };
}

export function isRevokeSignalTool(toolName) {
  return Boolean(toolName && String(toolName).endsWith("revoke_signal"));
}

const plural = (n, one, many) => (n === 1 ? one : many);

export function wakingOf(counts) {
  const pending = Number(counts?.pending || 0);
  const waking = counts?.waking === undefined ? pending : Number(counts.waking || 0);
  return Math.max(0, Math.min(waking, pending));
}

export function formatBreakdown(counts) {
  const pending = Number(counts?.pending || 0);
  const waking = wakingOf(counts);
  if (waking === pending) return "";
  if (waking === 0) return " (channel messages only, none waking)";
  const rest = counts.more ? "the rest" : String(pending - waking);
  return ` (${waking} waking: DMs, @mentions, orders, or notices; ${rest} other channel ${plural(pending - waking, "message", "messages")})`;
}

export function formatWaitingNotice(counts) {
  const pending = Number(counts?.pending || 0);
  const urgent = Number(counts?.urgent || 0);
  const amount = counts?.more ? `More than ${pending}` : String(pending);
  const urgentText = urgent > 0 ? `, ${urgent} urgent` : "";
  return `ForgeLoopBridge: ${amount} new ${plural(pending, "message", "messages")}${urgentText}${formatBreakdown(counts)}. Call get_updates to read ${plural(pending, "it", "them")}, then carry on.`;
}

export function formatUrgentNotice(counts) {
  const urgent = Number(counts?.urgent || 0);
  return `ForgeLoopBridge: ${urgent} urgent ${plural(urgent, "message", "messages")} just arrived. Call get_updates immediately.`;
}

export function formatIdleNotice(counts) {
  const pending = Number(counts?.pending || 0);
  const amount = counts?.more ? `More than ${pending}` : String(pending);
  return `ForgeLoopBridge: ${amount} new ${plural(pending, "message", "messages")}${formatBreakdown(counts)} arrived while idle. Call get_updates when ready.`;
}

async function fetchSignalCounts(state, timeoutMs, fetchImpl) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const fullUrl = resolveSignalUrl(state.url, state.baseUrl);
    const res = await fetchImpl(fullUrl, {
      method: "GET",
      headers: {
        Authorization: `Bearer ${state.secret}`,
        Accept: "application/json",
      },
      signal: controller.signal,
    });
    if (res.status === 401 || res.status === 403 || res.status === 404 || res.status === 410) {
      return { expired: true, status: res.status };
    }
    if (!res.ok) {
      return { error: `http_${res.status}` };
    }
    const counts = await res.json();
    return { counts };
  } catch (err) {
    return { error: err?.name === "AbortError" ? "timeout" : "network" };
  } finally {
    clearTimeout(timer);
  }
}

/**
 * Process a single host hook event cleanly. Always resolves (never throws).
 *
 * @param {object} event
 * @param {object} deps
 * @returns {Promise<{context?: string, continueWith?: string}>}
 */
export async function handleHookEvent(event, deps = {}) {
  const env = deps.env || process.env;
  const now = typeof deps.now === "function" ? deps.now() : Date.now();
  const fetchImpl = deps.fetch || globalThis.fetch;
  const stateFile = deps.statePath || statePathFor(event.sessionId || event.session_id || "default", env);
  const saveFn = deps.save || ((s) => saveState(stateFile, s));
  const loadFn = deps.load || (() => loadState(stateFile));
  const forgetFn = deps.forget || (() => forgetState(stateFile));

  const eventName = event.hookEventName || event.hook_event_name || "";
  const toolName = event.toolName || event.tool_name || "";
  const toolResponse = event.toolResponse ?? event.tool_response ?? event.toolOutput ?? event.tool_output;
  const baseUrl = event.baseUrl || env.BRIDGE_URL || "http://localhost:8000";

  let state = loadFn();

  if (eventName === "PostToolUse") {
    const extracted = extractSignalFromToolResult(toolName, toolResponse);
    if (extracted) {
      const nextState = {
        ...extracted,
        baseUrl,
        continuations: 0,
        lastToolCheckAt: now,
        backoffUntil: 0,
      };
      saveFn(nextState);
      return {
        context:
          "ForgeLoopBridge: signal link saved (0600). You will be notified mid-turn on URGENT messages and continued at turn end when waking messages wait.",
      };
    }
    if (isRevokeSignalTool(toolName)) {
      forgetFn();
      return { context: "ForgeLoopBridge: signal link removed for this session." };
    }
  }

  if (!state || !state.url || !state.secret) {
    if (eventName === "SessionStart") {
      return {
        context:
          "ForgeLoopBridge hooks are installed. After register_participant, call create_signal with your participant_id so hooks can wake you on new messages.",
      };
    }
    return {};
  }

  if (state.expiresAt && state.expiresAt * 1000 <= now) {
    forgetFn();
    return {
      context:
        "ForgeLoopBridge: signal link expired. Call create_signal to restore hook notifications.",
    };
  }

  if (eventName === "SessionStart" || eventName === "UserPromptSubmit") {
    state = { ...state, continuations: 0 };
    saveFn(state);
    if (state.backoffUntil && now < state.backoffUntil) return {};
    const check = await fetchSignalCounts(state, 2000, fetchImpl);
    if (check.expired) {
      forgetFn();
      return {
        context:
          "ForgeLoopBridge: signal link expired or was revoked. Call create_signal again if you are still active.",
      };
    }
    if (!check.counts) {
      saveFn({ ...state, backoffUntil: now + BACKOFF_ON_ERROR_MS });
      return {};
    }
    if (Number(check.counts.pending || 0) > 0) {
      return { context: formatIdleNotice(check.counts) };
    }
    return {};
  }

  if (eventName === "PostToolUse") {
    if (state.backoffUntil && now < state.backoffUntil) return {};
    if (state.lastToolCheckAt && now - state.lastToolCheckAt < TOOL_CHECK_MIN_INTERVAL_MS) {
      return {};
    }
    const check = await fetchSignalCounts(state, 1500, fetchImpl);
    if (check.expired) {
      forgetFn();
      return {
        context:
          "ForgeLoopBridge: signal link expired or was revoked. Call create_signal again to keep receiving urgent interrupts.",
      };
    }
    if (!check.counts) {
      saveFn({ ...state, lastToolCheckAt: now, backoffUntil: now + BACKOFF_ON_ERROR_MS });
      return {};
    }
    saveFn({ ...state, lastToolCheckAt: now, backoffUntil: 0 });
    if (Number(check.counts.urgent || 0) > 0) {
      return { context: formatUrgentNotice(check.counts) };
    }
    return {};
  }

  if (eventName === "Stop") {
    if (state.backoffUntil && now < state.backoffUntil) return {};
    const maxContinuations = maxContinuationsFromEnv(env);
    const currentContinuations = Number(state.continuations || 0);
    if (currentContinuations >= maxContinuations) {
      return {};
    }
    const check = await fetchSignalCounts(state, 3000, fetchImpl);
    if (check.expired) {
      forgetFn();
      return {};
    }
    if (!check.counts) {
      saveFn({ ...state, backoffUntil: now + BACKOFF_ON_ERROR_MS });
      return {};
    }
    if (wakingOf(check.counts) > 0) {
      saveFn({ ...state, continuations: currentContinuations + 1, backoffUntil: 0 });
      return { continueWith: formatWaitingNotice(check.counts) };
    }
    if (currentContinuations !== 0) {
      saveFn({ ...state, continuations: 0, backoffUntil: 0 });
    }
    return {};
  }

  return {};
}
