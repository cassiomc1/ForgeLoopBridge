"""Check the browser application's inline JavaScript with Node's parser."""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    html = (root / "static" / "index.html").read_text(encoding="utf-8")
    scripts = re.findall(
        r"<script(?P<attributes>[^>]*)>(?P<body>.*?)</script>",
        html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    inline_scripts = [
        body for attributes, body in scripts if "src=" not in attributes.lower() and body.strip()
    ]
    if len(inline_scripts) != 1:
        raise SystemExit(f"expected one non-empty inline script, found {len(inline_scripts)}")

    node = shutil.which("node")
    if node is None:
        raise SystemExit("node is required for frontend syntax validation")

    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", suffix=".js", delete=False
        ) as temporary:
            temporary.write(inline_scripts[0])
            temporary_path = Path(temporary.name)
        result = subprocess.run(
            [node, "--check", str(temporary_path)],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.stdout:
            print(result.stdout, end="")
        if result.stderr:
            print(result.stderr, end="")
        if result.returncode:
            raise SystemExit(result.returncode)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)

    print("Frontend inline JavaScript syntax is valid.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
