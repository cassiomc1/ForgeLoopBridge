"""Zero-dependency stdio-to-Streamable-HTTP MCP proxy for ForgeLoopBridge.

Installed as the ``forgeloopbridge-mcp`` console script so stdio MCP hosts and
policy engines (such as HOL Guard) can launch a dedicated executable basename.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import urllib.error
import urllib.request
from typing import Any

_stdout_lock = threading.Lock()
_active_streams_lock = threading.Lock()
_active_streams: dict[Any, Any] = {}


def _write_stdout(line: str) -> None:
    with _stdout_lock:
        sys.stdout.write(line.rstrip("\n") + "\n")
        sys.stdout.flush()


def _run_stream_request(
    mcp_url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    req_id: Any,
) -> None:
    stream_headers = dict(headers)
    stream_headers["Accept"] = "text/event-stream, application/json"
    params = payload.get("params")
    if isinstance(params, dict) and isinstance(params.get("name"), str):
        stream_headers["Mcp-Name"] = params["name"]

    req = urllib.request.Request(
        mcp_url,
        data=json.dumps(payload).encode("utf-8"),
        headers=stream_headers,
        method="POST",
    )
    try:
        resp = urllib.request.urlopen(req, timeout=300)
        with _active_streams_lock:
            _active_streams[req_id] = resp
        ctype = (resp.headers.get("Content-Type") or "").lower()
        if "text/event-stream" in ctype:
            for raw_line in resp:
                with _active_streams_lock:
                    if req_id not in _active_streams:
                        break
                line = raw_line.decode("utf-8", errors="replace").strip()
                if line.startswith("data:"):
                    data_str = line[5:].strip()
                    if data_str:
                        _write_stdout(data_str)
        else:
            body = resp.read().decode("utf-8").strip()
            if body:
                _write_stdout(body)
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="replace")
        if req_id is not None:
            err = {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32000, "message": f"HTTP {exc.code}: {err_body}"},
            }
            _write_stdout(json.dumps(err))
    except Exception as exc:
        with _active_streams_lock:
            was_cancelled = req_id not in _active_streams
        if not was_cancelled and req_id is not None:
            err = {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32000, "message": str(exc)}}
            _write_stdout(json.dumps(err))
    finally:
        with _active_streams_lock:
            resp_obj = _active_streams.pop(req_id, None)
        if resp_obj is not None:
            try:
                resp_obj.close()
            except Exception:
                pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="forgeloopbridge-mcp",
        description="Stdio-to-Streamable-HTTP MCP proxy for ForgeLoopBridge.",
    )
    parser.add_argument(
        "--url",
        default=os.getenv("BRIDGE_URL", "http://localhost:8000"),
        help="Base URL of the ForgeLoopBridge server (default: $BRIDGE_URL or http://localhost:8000)",
    )
    parser.add_argument(
        "--token",
        default=None,
        help="Bearer token (defaults to $BRIDGE_TOKEN, $AGENT_TOKEN, or $MASTER_TOKEN)",
    )
    args = parser.parse_args(argv)

    bridge_url = (args.url or "http://localhost:8000").rstrip("/")
    token = (
        args.token
        or os.getenv("BRIDGE_TOKEN")
        or os.getenv("AGENT_TOKEN")
        or os.getenv("MASTER_TOKEN")
        or ""
    )
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
            _write_stdout(json.dumps(err))
            continue

        if isinstance(payload, dict):
            method = payload.get("method")
            params = payload.get("params") if isinstance(payload.get("params"), dict) else {}

            if method == "notifications/cancelled":
                target_id = params.get("requestId")
                with _active_streams_lock:
                    resp_obj = _active_streams.pop(target_id, None)
                if resp_obj is not None:
                    try:
                        resp_obj.close()
                    except Exception:
                        pass
                continue

            if method == "events/stream":
                req_id = payload.get("id")
                t = threading.Thread(
                    target=_run_stream_request,
                    args=(mcp_url, headers, payload, req_id),
                    daemon=True,
                )
                t.start()
                continue

        req_headers = dict(headers)
        if isinstance(payload, dict):
            params = payload.get("params")
            if isinstance(params, dict) and isinstance(params.get("name"), str):
                req_headers["Mcp-Name"] = params["name"]

        req = urllib.request.Request(
            mcp_url,
            data=json.dumps(payload).encode("utf-8"),
            headers=req_headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=65) as resp:
                body = resp.read().decode("utf-8").strip()
                if body:
                    _write_stdout(body)
        except urllib.error.HTTPError as exc:
            err_body = exc.read().decode("utf-8", errors="replace")
            req_id = payload.get("id") if isinstance(payload, dict) else None
            if req_id is not None:
                err = {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32000, "message": f"HTTP {exc.code}: {err_body}"},
                }
                _write_stdout(json.dumps(err))
        except Exception as exc:
            req_id = payload.get("id") if isinstance(payload, dict) else None
            if req_id is not None:
                err = {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32000, "message": str(exc)}}
                _write_stdout(json.dumps(err))
    return 0


def cli() -> None:
    raise SystemExit(main())


if __name__ == "__main__":
    cli()
