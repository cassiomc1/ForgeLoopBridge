#!/usr/bin/env python3
"""Zero-dependency stdio-to-Streamable-HTTP MCP proxy for ForgeLoopBridge.

Thin wrapper around ``bridge_hub.stdio_proxy`` (also installed as the
``forgeloopbridge-mcp`` console script).
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bridge_hub.stdio_proxy import cli, main  # noqa: E402

__all__ = ["cli", "main"]

if __name__ == "__main__":
    cli()

