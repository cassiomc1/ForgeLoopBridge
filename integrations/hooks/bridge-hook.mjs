#!/usr/bin/env node
import fs from "node:fs";
import { handleHookEvent, saveState, statePathFor } from "./core.mjs";

function parseArgs(argv) {
  const out = { host: "claude", saveUrl: null, saveSecret: null, sessionId: "default" };
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg === "--host" && argv[i + 1]) {
      out.host = argv[++i].toLowerCase();
    } else if (arg === "--session" && argv[i + 1]) {
      out.sessionId = argv[++i];
    } else if (arg === "--save" && argv[i + 1] && argv[i + 2]) {
      out.saveUrl = argv[++i];
      out.saveSecret = argv[++i];
    }
  }
  return out;
}

function readStdinSyncSafe() {
  try {
    return fs.readFileSync(0, "utf8");
  } catch {
    return "";
  }
}

function formatHostReply(host, eventName, reply) {
  if (!reply || (!reply.context && !reply.continueWith)) {
    return null;
  }

  if (reply.continueWith) {
    if (host === "cursor") {
      return { followup_message: reply.continueWith };
    }
    return {
      decision: "block",
      reason: reply.continueWith,
    };
  }

  if (reply.context) {
    if (host === "gemini") {
      return {
        hookSpecificOutput: {
          hookEventName: eventName || "PostToolUse",
          additionalContext: reply.context,
        },
      };
    }
    return {
      hookSpecificOutput: {
        hookEventName: eventName || "PostToolUse",
        additionalContext: reply.context,
      },
    };
  }
  return null;
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  if (args.saveUrl && args.saveSecret) {
    const stateFile = statePathFor(args.sessionId);
    saveState(stateFile, {
      url: args.saveUrl,
      secret: args.saveSecret,
      baseUrl: process.env.BRIDGE_URL || "http://localhost:8000",
      continuations: 0,
      backoffUntil: 0,
    });
    process.stdout.write(JSON.stringify({ saved: true, path: stateFile }) + "\n");
    return;
  }

  const rawStdin = readStdinSyncSafe().trim();
  let event = {};
  if (rawStdin) {
    try {
      event = JSON.parse(rawStdin);
    } catch {
      process.exit(0);
    }
  }

  const eventName = event.hookEventName || event.hook_event_name || "";
  const reply = await handleHookEvent(event);
  const formatted = formatHostReply(args.host, eventName, reply);
  if (formatted) {
    process.stdout.write(JSON.stringify(formatted) + "\n");
  }
}

main().catch(() => {
  process.exit(0);
});
