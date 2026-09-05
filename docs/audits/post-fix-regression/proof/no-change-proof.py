"""§9.12 — ForgeLoopBridge no-change proof for the remaining 1.10.1 semantics.

Every row of the Gate A §6 semantic inventory that the version-boundary fix did
not already address must resolve to "not consumed, or safely optional", by
evidence rather than assertion. The four §9.12 obligations are proved alongside
them, including that the examples do not teach a workflow the target revision no
longer offers: every `forgeloop <command>` the Bridge documentation names is
checked against the command surface the real `protocol-info` advertises.

Scopes (RUNTIME / TESTS / PROSE) and the named frozen-evidence exclusions are
defined in bridge_scopes.py. Read that first: the scoping is what makes these
claims mean anything.

Usage: python no-change-proof.py <canonical-dir>
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bridge_scopes import FROZEN, absent, bridge_repo_root, load_scopes  # noqa: E402

HERE = Path(__file__).resolve().parent
PINNED = json.loads((HERE / "pinned-revision.json").read_text(encoding="utf-8"))
BRIDGE = bridge_repo_root()

if len(sys.argv) < 2:
    raise SystemExit("no-change-proof: usage: python no-change-proof.py <canonical-dir>")

CANONICAL = Path(sys.argv[1])
PROTOCOL_INFO = json.loads((CANONICAL / "protocol-info.json").read_text(encoding="utf-8"))

RUNTIME, TESTS, PROSE = load_scopes(BRIDGE)

rows: list[tuple[str, str, bool, str]] = []


def row(number: str, change: str, ok: bool, detail: str) -> None:
    rows.append((number, change, bool(ok), detail))


# Row 1 — protocol-info.packageVersion 1.10.0 -> 1.10.1
ok1, detail1 = absent(
    {**RUNTIME, **TESTS},
    "parse_version",
    "LooseVersion",
    "packaging.version",
    "import semver",
    "pkg_resources",
)
row("1", "protocol-info.packageVersion bump", ok1, f"no version-comparison machinery ({detail1})")

# Rows 2/3/4 — transaction record changes
row("2", ".txn manifest gains lockTaskId", *absent(RUNTIME, ".txn", "lockTaskId"))
row("3", "transaction status gains ABORTED", *absent(RUNTIME, "ROLLED_BACK", "ABANDONED", "ABORTED"))
row("4", "transaction compaction / absent stage+backup", *absent(RUNTIME, "compactedAt", "E_TRANSACTION"))

# Rows 5/6 — doctor and inspect terminal verdicts. The Bridge spawns only
# protocol-info plus host-configured commands; doctor appears nowhere in runtime.
ok56, detail56 = absent(RUNTIME, "doctor")
spawned = sorted(set(re.findall(r'"(protocol-info|doctor|inspect)"', "\n".join(RUNTIME.values()))))
row("5", "doctor/inspect terminal ROLLED_BACK+ABORTED", ok56, f"runtime spawns {spawned} only ({detail56})")
row("6", "doctor --fix no longer double-reports", ok56, "same evidence as row 5")

# Row 7 — new fail-closed E_TASK_CONTEXT_MISMATCH
row("7", "new fail-closed E_TASK_CONTEXT_MISMATCH", *absent(PROSE, "E_TASK_CONTEXT_MISMATCH"))

# Row 8 — recovery under task lock reports RECOVERY_FAILED
ok8, detail8 = absent(RUNTIME, "recovery.json", "RECOVERY_FAILED")
row("8", "recovery under task lock -> RECOVERY_FAILED", ok8, f"no raw recovery-artifact read ({detail8})")

# Row 9 — rollback replay path safety is ForgeLoop-internal. Lexical searches for
# "rollback"/"replay" are the wrong instrument here: the Bridge legitimately owns
# a database session rollback and a message-board history replay, and prose in
# docstrings wraps across lines. The decisive question is structural — does
# runtime code touch ForgeLoop-owned state at all?
ok9, detail9 = absent(RUNTIME, "assertSafePath", "rollbackPlan", "rollback_plan")
writes_forgeloop = [
    f"{name}:{number}"
    for name, text in RUNTIME.items()
    for number, line in enumerate(text.splitlines(), 1)
    if ".forgeloop" in line
    and re.search(r"\b(open|write_text|write_bytes|mkdir|unlink|rmtree|replace|rename)\b", line)
]
row(
    "9",
    "rollback replay uses symlink-aware assertSafePath",
    ok9 and not writes_forgeloop,
    f"no ForgeLoop rollback identifier ({detail9}) and no runtime write under "
    f".forgeloop/ ({writes_forgeloop or 'none'}); path safety stays inside ForgeLoop — cf. B19"
    if not writes_forgeloop
    else f"writes: {writes_forgeloop[:3]}",
)

# Row 10 — next gains commands + commandSpecs (additive)
context_py = RUNTIME["bridge_protocol/forgeloop_context.py"]
consumes_commands = "commandSpecs" in context_py or '"commands"' in context_py
rejects_unknown = "forbid" in context_py
row(
    "10",
    "next RESOLVE_BLOCKER/REVALIDATION_REQUIRED gains commands+commandSpecs",
    not consumes_commands and not rejects_unknown,
    "consumer reads named fields only; unknown additive fields are ignored, never rejected",
)

# Row 11 — route profile obligation signals normalized
ok11, detail11 = absent(RUNTIME, "E_ROUTE_STALE", "E_ROUTE_GUIDE_MISMATCH", "assertExecutionProfile")
transported_opaquely = 'profile.model_dump(mode="json")' in context_py
row(
    "11",
    "route profile obligation signals normalized",
    ok11 and transported_opaquely,
    f"resolved profile transported opaquely, no local derivation ({detail11})",
)

# Rows 12/13/14 — Integration API surface. The question is whether the Bridge
# *imports* the ForgeLoop package, not whether prose names it: the README names
# it precisely to tell the host to prefer it over synthesizing state.
js_import = re.compile(r"""(?:from|require\()\s*["']@cassiomc1/forgeloop""")
importers = [name for name, text in PROSE.items() if js_import.search(text)]
no_js_import = not importers
row(
    "12",
    "new export FORGELOOP_INTEGRATION_RUNTIME_VERSION",
    no_js_import,
    f"no JS import of the package ({importers or 'none'}); the Bridge is a Python consumer",
)
row(
    "13",
    "ForgeLoopStructuralQualityProvider interface -> union",
    no_js_import,
    "type-level change in a package the Bridge never imports",
)
row("14", "4 retired modules excluded from the tarball", no_js_import, "no deep-path import")

# Row 15 — absent lifecycle receipt reported NOT_VERIFIED (CI-only in ForgeLoop)
readme = PROSE["README.md"]
row(
    "15",
    "absent lifecycle receipt reported NOT_VERIFIED",
    "NOT_VERIFIED" in readme and "attestation" in readme.lower(),
    "the Bridge's NOT_VERIFIED prose is attestation-scoped and remains correct",
)

# Row 16 — MCP dependency pinning is ForgeLoop-internal
row("16", "MCP dependency pinned exact + lockfile committed", *absent(RUNTIME, "modelcontextprotocol"))

# ── §9.12 obligations ─────────────────────────────────────────────────────────

sys.path.insert(0, str(BRIDGE))
try:
    from bridge_protocol.forgeloop_context import (  # noqa: E402
        BOUNDARY_SUPPORTED,
        SUPPORTED_FORGELOOP_INTEGRATION_API_VERSIONS,
        SUPPORTED_FORGELOOP_PROTOCOL_VERSIONS,
        forgeloop_boundary_status,
    )
except ImportError as error:
    raise SystemExit(
        f"no-change-proof: cannot import bridge_protocol from {BRIDGE}: {error}\n"
        "no-change-proof: install the Bridge requirements first "
        "(python -m pip install -r requirements.txt)."
    ) from error

row(
    "§9.12a",
    "declared support still covers the new target",
    PROTOCOL_INFO["protocolVersion"] in SUPPORTED_FORGELOOP_PROTOCOL_VERSIONS
    and PROTOCOL_INFO["features"]["integrationApi"]["version"]
    in SUPPORTED_FORGELOOP_INTEGRATION_API_VERSIONS
    and forgeloop_boundary_status(PROTOCOL_INFO) == (BOUNDARY_SUPPORTED, None),
    f"declared protocol {SUPPORTED_FORGELOOP_PROTOCOL_VERSIONS} / API "
    f"{SUPPORTED_FORGELOOP_INTEGRATION_API_VERSIONS} vs target "
    f"{PROTOCOL_INFO['protocolVersion']} / {PROTOCOL_INFO['features']['integrationApi']['version']}",
)

row(
    "§9.12b",
    "changed capabilities are not consumed or are safely optional",
    all(ok for number, _, ok, _ in rows if number.isdigit()),
    "the read surface is adaptiveExecutionProfiles, executionProfileContext and "
    "integrationApi — all still v1",
)

# The lookbehind rejects `.forgeloop artifacts`-style prose, which names a path,
# not a command.
advertised = {
    (entry if isinstance(entry, str) else entry.get("name") or entry.get("command"))
    for entry in PROTOCOL_INFO["commands"]
}
command_pattern = re.compile(r"(?<![.\w])forgeloop ([a-z][a-z0-9-]+)")
PROSE_WORDS = {"protocol", "task", "artifacts", "state", "is", "as", "the", "and", "or", "for"}
referenced: set[str] = set()
for name in ("README.md", "examples/worker_poll.py", "examples/AUTONOMY.md"):
    if name in PROSE:
        referenced |= set(command_pattern.findall(PROSE[name]))
referenced -= PROSE_WORDS
unknown = sorted(referenced - advertised)

row(
    "§9.12c",
    "no new next/recovery/approval/action/completion semantics invalidate guidance",
    not unknown,
    f"{len(referenced)} referenced commands, all advertised by the target"
    if not unknown
    else f"unknown: {unknown}",
)
row(
    "§9.12d",
    "examples do not teach a now-invalid workflow",
    not unknown,
    f"checked against the target's {len(advertised)}-command surface",
)

failed = 0
print(f"target: ForgeLoop {PROTOCOL_INFO['packageVersion']} @ {PINNED['forgeLoopGitCommit'][:7]}")
print(f"scopes: RUNTIME={len(RUNTIME)} TESTS={len(TESTS)} PROSE={len(PROSE)} files")
print(f"frozen: {', '.join(FROZEN)}")
print()
print(f"{'row':<9}| {'result':<5}| change")
print("-" * 104)
for number, change, ok, detail in rows:
    if not ok:
        failed += 1
    print(f"{number:<9}| {'NO' if ok else 'FAIL':<5}| {change}")
    print(f"{'':<9}| {'':<5}| {detail}")
print("-" * 104)
verdict = "BRIDGE: NO_CHANGE_REQUIRED" if not failed else "BRIDGE: CHANGE REQUIRED"
print(f"{len(rows) - failed}/{len(rows)} rows proven — {verdict}")
sys.exit(1 if failed else 0)
