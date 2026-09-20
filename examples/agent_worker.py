#!/usr/bin/env python3
"""Master-Agent Bridge — Agent Worker Runner.

Convenience entry point for the Agent Worker polling process.
"""

from worker_poll import main

if __name__ == "__main__":
    import sys
    sys.exit(main())
