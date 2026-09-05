"""§10 cross-repository contract testing — ForgeLoopBridge sibling.

Consumes the REAL `protocol-info` and the REAL canonical `task/context`
projection produced by the pinned ForgeLoop revision against a disposable
project the harness created itself. ForgeLoopAudit is not involved anywhere in
this file (§10.1).

Covers the §10.2 shared invariants and every applicable §10.3 negative scenario.

Usage: python bridge-side.py <canonical-dir> [task-id]
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bridge_scopes import absent, bridge_repo_root, load_scopes  # noqa: E402

HERE = Path(__file__).resolve().parent
PINNED = json.loads((HERE / "pinned-revision.json").read_text(encoding="utf-8"))
BRIDGE = bridge_repo_root()
sys.path.insert(0, str(BRIDGE))

try:
    from bridge_protocol.forgeloop_context import (  # noqa: E402
        BOUNDARY_SUPPORTED,
        SUPPORTED_FORGELOOP_INTEGRATION_API_VERSIONS,
        SUPPORTED_FORGELOOP_PROTOCOL_VERSIONS,
        consume_task_context,
        forgeloop_boundary_status,
    )
except ImportError as error:  # fail closed, never skip silently
    raise SystemExit(
        f"bridge-side: cannot import bridge_protocol from {BRIDGE}: {error}\n"
        "bridge-side: install the Bridge requirements first "
        "(python -m pip install -r requirements.txt)."
    ) from error

if len(sys.argv) < 2:
    raise SystemExit("bridge-side: usage: python bridge-side.py <canonical-dir> [task-id]")

CANONICAL = Path(sys.argv[1])
TASK_ID = sys.argv[2] if len(sys.argv) > 2 else PINNED["taskId"]

PROTOCOL_INFO = json.loads((CANONICAL / "protocol-info.json").read_text(encoding="utf-8"))
CONTEXT_RESOURCE = json.loads((CANONICAL / "task-context.json").read_text(encoding="utf-8"))
PROJECTION = CONTEXT_RESOURCE["data"]

RUNTIME, _TESTS, _PROSE = load_scopes(BRIDGE)

results: list[tuple[str, bool, str]] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    results.append((label, bool(condition), detail))


# ── §10.2 shared invariants ────────────────────────────────────────────────────

consumed = consume_task_context(PROTOCOL_INFO, PROJECTION, expected_task_id=TASK_ID)

check(
    "B1 real canonical projection is accepted as canonical",
    consumed["status"] == "CANONICAL" and consumed["source"] == "FORGELOOP_CANONICAL",
    f"status={consumed['status']} source={consumed['source']}",
)
check(
    "B2 task identity preserved, not re-derived",
    consumed["task_id"] == TASK_ID == PROJECTION["taskId"],
    f"task_id={consumed['task_id']}",
)
check(
    "B3 protocol version interpretation agrees with the canonical boundary",
    PROTOCOL_INFO["protocolVersion"] in SUPPORTED_FORGELOOP_PROTOCOL_VERSIONS
    and PROJECTION["protocolVersion"] in SUPPORTED_FORGELOOP_PROTOCOL_VERSIONS
    and PROTOCOL_INFO["features"]["integrationApi"]["version"]
    in SUPPORTED_FORGELOOP_INTEGRATION_API_VERSIONS
    and forgeloop_boundary_status(PROTOCOL_INFO) == (BOUNDARY_SUPPORTED, None),
    f"protocol={PROTOCOL_INFO['protocolVersion']} "
    f"api={PROTOCOL_INFO['features']['integrationApi']['version']}",
)
check(
    "B4 resolved execution profile is transported, never chosen locally",
    consumed["execution_profile"] == PROJECTION["executionProfile"],
    f"resolved={consumed['execution_profile']['resolved']} "
    f"escalated={consumed['execution_profile']['escalated']}",
)
check(
    "B5 lifecycle phase and next action are copies of canonical values",
    consumed["phase"] == PROJECTION["phase"]
    and consumed["next_action"] == PROJECTION["nextAction"],
    f"phase={consumed['phase']} next_action={consumed['next_action']}",
)
check(
    "B6 verification semantics transported without promotion",
    consumed["context"]["verification_requirements"] == PROJECTION["verificationRequirements"],
    f"{len(consumed['context']['verification_requirements'])} requirement(s)",
)
check(
    "B7 canonical invariants preserved verbatim, phase skipping refused",
    consumed["invariants"] == PROJECTION["invariants"]
    and consumed["invariants"]["lifecyclePhaseSkippingAllowed"] is False,
)

# ── §10.3 capability present ───────────────────────────────────────────────────

check(
    "B8 capability present -> feature enabled, canonical result preserved",
    PROTOCOL_INFO["features"]["adaptiveExecutionProfiles"]["supported"] is True
    and PROTOCOL_INFO["features"]["executionProfileContext"]["supported"] is True
    and consumed["status"] == "CANONICAL",
)

# ── §10.3 capability absent ────────────────────────────────────────────────────

absent_boundary = copy.deepcopy(PROTOCOL_INFO)
absent_boundary["features"]["adaptiveExecutionProfiles"]["supported"] = False
absent_boundary["features"]["executionProfileContext"]["supported"] = False
absent_result = consume_task_context(absent_boundary, PROJECTION, expected_task_id=TASK_ID)
check(
    "B9 capability absent -> explicit balanced compatibility, no false positive",
    absent_result["status"] == "COMPATIBILITY_FALLBACK"
    and absent_result["source"] == "BALANCED_COMPATIBILITY"
    and absent_result["task_id"] is None
    and absent_result["execution_profile"]["resolved"] == "balanced",
    f"status={absent_result['status']} resolved={absent_result['execution_profile']['resolved']}",
)
check(
    "B10 capability absent -> no mutation fallback, no invented authority",
    absent_result["execution_profile"]["reasons"] == ["LEGACY_ROUTE_COMPATIBILITY"]
    and absent_result["invariants"]["safetyFloorPreserved"] is True
    and absent_result["invariants"]["lifecyclePhaseSkippingAllowed"] is False
    and "phase" in absent_result
    and absent_result["phase"] is None,
)

# ── §10.3 capability malformed / unsupported version ──────────────────────────

malformed = copy.deepcopy(PROTOCOL_INFO)
malformed["features"]["executionProfileContext"]["version"] = 2
malformed_result = consume_task_context(malformed, PROJECTION, expected_task_id=TASK_ID)
check(
    "B11 malformed/unsupported capability version -> unavailable, nothing inferred",
    malformed_result["status"] == "UNAVAILABLE"
    and malformed_result["fallback"] == "NONE"
    and "executionProfileContext.version 2" in malformed_result["reason"],
    malformed_result["reason"],
)

non_integer = copy.deepcopy(PROTOCOL_INFO)
non_integer["features"]["integrationApi"]["version"] = "1"
non_integer_result = consume_task_context(non_integer, PROJECTION)
check(
    "B12 non-integer advertised version -> unavailable, not coerced",
    non_integer_result["status"] == "UNAVAILABLE"
    and "not an integer version" in non_integer_result["reason"],
)

# ── §10.3 unsupported protocol / schema ───────────────────────────────────────

future = copy.deepcopy(PROTOCOL_INFO)
future["protocolVersion"] = 2
future["readsProtocol"] = [2]
future["writesProtocol"] = [2]
future["compatibility"]["protocolVersion"] = 2
future_result = consume_task_context(future, PROJECTION, expected_task_id=TASK_ID)
check(
    "B13 unknown protocol version -> fail closed, no balanced downgrade",
    future_result["status"] == "UNAVAILABLE"
    and future_result["fallback"] == "NONE"
    and future_result["status"] != "COMPATIBILITY_FALLBACK",
    future_result["reason"],
)

future_schema = copy.deepcopy(PROTOCOL_INFO)
future_schema["compatibility"]["schemaVersion"] = 2
check(
    "B14 unknown schema version -> fail closed",
    consume_task_context(future_schema, PROJECTION)["status"] == "UNAVAILABLE",
)

future_projection = copy.deepcopy(PROJECTION)
future_projection["schemaVersion"] = 2
future_projection_result = consume_task_context(PROTOCOL_INFO, future_projection)
check(
    "B15 projection from an unknown generation -> fail closed",
    future_projection_result["status"] == "UNAVAILABLE"
    and "schemaVersion 2" in future_projection_result["reason"],
)

undeclared = copy.deepcopy(PROTOCOL_INFO)
for key in ("protocolVersion", "readsProtocol", "writesProtocol", "compatibility"):
    undeclared.pop(key, None)
check(
    "B16 capability advertised without a declared protocol version -> fail closed",
    consume_task_context(undeclared, PROJECTION)["status"] == "UNAVAILABLE",
)

# ── §10.3 supported legacy / degraded mode ────────────────────────────────────

legacy = {"protocolVersion": 1, "features": {}}
legacy_result = consume_task_context(legacy, PROJECTION, expected_task_id=TASK_ID)
check(
    "B17 supported legacy boundary -> explicit compatibility mode, limited, no authority",
    legacy_result["status"] == "COMPATIBILITY_FALLBACK"
    and legacy_result["context_policy"]["context_depth"] == "relevant"
    and legacy_result["context"]["objective"] is None
    and legacy_result["optional_context"] == {"available": [], "loaded": []},
    f"reason={legacy_result['reason']}",
)
check(
    "B18 package version alone never decides compatibility",
    forgeloop_boundary_status({**PROTOCOL_INFO, "packageVersion": "99.0.0"})[0]
    == BOUNDARY_SUPPORTED
    and consume_task_context(
        {**copy.deepcopy(PROTOCOL_INFO), "packageVersion": "0.0.1"},
        PROJECTION,
        expected_task_id=TASK_ID,
    )["status"]
    == "CANONICAL",
)

# ── §10.3 recovery ────────────────────────────────────────────────────────────

# Scoped to RUNTIME: the Bridge's own documentation legitimately names these
# artifacts in order to forbid reading them.
ownership_ok, ownership_detail = absent(RUNTIME, "recovery.json", 'task.json"', "'task.json'")
check(
    "B19 recovery: no raw-artifact ownership inference anywhere in Bridge runtime",
    ownership_ok,
    ownership_detail,
)

# ── §10.3 task identity mismatch ──────────────────────────────────────────────

check(
    "B20 projection for a different task is refused",
    consume_task_context(PROTOCOL_INFO, PROJECTION, expected_task_id="other-task")["status"]
    == "UNAVAILABLE",
)

# ── §10.3 durable action uncertainty / authority / evidence ───────────────────

worker_poll = (BRIDGE / "examples" / "worker_poll.py").read_text(encoding="utf-8")
check(
    "B21 COMMIT_UNKNOWN -> hard stop, reconciliation required, no retry",
    "COMMIT_UNKNOWN_REASON_CODES" in worker_poll
    and "HARD STOP: COMMIT_UNKNOWN; do not retry this action." in worker_poll
    and "reconcile" in worker_poll.lower(),
)
readme = (BRIDGE / "README.md").read_text(encoding="utf-8")
check(
    "B22 Bridge text saying approved never satisfies canonical approval",
    "cannot satisfy them" in readme and "not ForgeLoop authority" in worker_poll,
)
check(
    "B23 missing evidence is never promoted to VALID/PASS",
    "NOT_VERIFIED" in worker_poll and "never" in worker_poll.lower(),
)
check(
    "B24 Audit is not required for Bridge compatibility",
    not any(
        "forgeloopaudit" in path.read_text(encoding="utf-8").lower()
        for path in [BRIDGE / "bridge_protocol" / "forgeloop_context.py", BRIDGE / "requirements.txt"]
        if path.exists()
    ),
)

failed = 0
for label, ok, detail in results:
    if not ok:
        failed += 1
    print(f"{'PASS' if ok else 'FAIL'} | {label}" + (f"\n     | {detail}" if detail else ""))
print(f"bridge-side: {len(results) - failed}/{len(results)} PASS")
sys.exit(1 if failed else 0)
