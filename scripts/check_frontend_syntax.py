"""Check the browser application's inline JavaScript with Node's parser."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from html.parser import HTMLParser
from pathlib import Path


class _InlineScriptExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.in_inline_script = False
        self.current_buffer: list[str] = []
        self.inline_scripts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "script":
            attr_dict = {k.lower(): v for k, v in attrs}
            if "src" not in attr_dict:
                self.in_inline_script = True
                self.current_buffer = []

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "script" and self.in_inline_script:
            self.in_inline_script = False
            script_text = "".join(self.current_buffer).strip()
            if script_text:
                self.inline_scripts.append(script_text)

    def handle_data(self, data: str) -> None:
        if self.in_inline_script:
            self.current_buffer.append(data)


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    html = (root / "static" / "index.html").read_text(encoding="utf-8")
    extractor = _InlineScriptExtractor()
    extractor.feed(html)
    inline_scripts = extractor.inline_scripts
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
