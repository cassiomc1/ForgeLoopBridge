#!/usr/bin/env python3
"""Master-Agent Bridge — Agent Worker Runner.

Convenience entry point for the Agent Worker polling process.
"""

import sys
from pathlib import Path

_EXAMPLES_DIR = str(Path(__file__).resolve().parent)
if _EXAMPLES_DIR not in sys.path:
    sys.path.insert(0, _EXAMPLES_DIR)

try:
    from worker_poll import main
except ImportError:
    from .worker_poll import main  # type: ignore[no-redef]

if __name__ == "__main__":
    sys.exit(main())

