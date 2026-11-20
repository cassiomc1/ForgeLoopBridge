#!/usr/bin/env python3
"""Zero-dependency stdio-to-Streamable-HTTP MCP proxy for ForgeLoopBridge."""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request


def main() -> int:
    bridge_url = os.getenv("BRIDGE_URL", "http://localhost:8000").rstrip("/")
    token = os.getenv("BRIDGE_TOKEN") or os.getenv("AGENT_TOKEN") or os.getenv("MASTER_TOKEN") or ""
    if not token:
        sys.stderr.write("BRIDGE_TOKEN (or AGENT_TOKEN / MASTER_TOKEN) must be set.\n")
        return 1

    mcp_url = f"{bridge_url}/mcp"
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
    }

    for line in sys.stdin:
        raw = line.strip()
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            err = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": str(exc)}}
            sys.stdout.write(json.dumps(err) + "\n")
            sys.stdout.flush()
            continue

        req = urllib.request.Request(
            mcp_url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=65) as resp:
                body = resp.read().decode("utf-8").strip()
                if body:
                    sys.stdout.write(body + "\n")
                    sys.stdout.flush()
        except urllib.error.HTTPError as exc:
            err_body = exc.read().decode("utf-8", errors="replace")
            req_id = payload.get("id") if isinstance(payload, dict) else None
            if req_id is not None:
                err = {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32000, "message": f"HTTP {exc.code}: {err_body}"},
                }
                sys.stdout.write(json.dumps(err) + "\n")
                sys.stdout.flush()
        except Exception as exc:
            req_id = payload.get("id") if isinstance(payload, dict) else None
            if req_id is not None:
                err = {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32000, "message": str(exc)}}
                sys.stdout.write(json.dumps(err) + "\n")
                sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
