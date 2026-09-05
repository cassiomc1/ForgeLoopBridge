#!/usr/bin/env bash
#
# §9.12 no-change proof — deterministic runner.
#
# Materializes the pinned ForgeLoop revision, builds a disposable project with
# it, regenerates the real canonical projection, then runs the three suites:
#
#   audit-side.mjs      §10.1 sibling A, ForgeLoopAudit through its own vendored
#                       runtime (skipped only when that repo is absent, because
#                       §10.1 requires neither sibling to depend on the other)
#   bridge-side.py      §10.2 invariants and §10.3 negative scenarios
#   no-change-proof.py  the §9.12 inventory determination
#
# Nothing is read from a stored fixture. If the pinned revision cannot be
# materialized, the run fails closed.
#
# Environment:
#   FORGELOOP_REPO          git checkout of ForgeLoop to archive the pinned SHA from
#   FORGELOOP_PACKAGE_ROOT  pre-materialized package root at the pinned revision
#   FORGELOOP_AUDIT_REPO    ForgeLoopAudit checkout (enables the Audit suite)
#   PYTHON                  interpreter with the Bridge requirements installed
#   KEEP_WORKDIR=1          keep the scratch directory for inspection

set -euo pipefail

# Determinism: stable collation and timestamps regardless of the caller's shell.
export TZ=UTC
export LC_ALL=C

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BRIDGE="$(cd "$HERE" && while [ ! -d bridge_protocol ] || [ ! -e .git ]; do cd ..; [ "$PWD" = / ] && { echo "run-proof: no ForgeLoopBridge checkout above $HERE" >&2; exit 2; }; done; pwd)"

read_pinned() {
  "${PYTHON:-python3}" -c "import json,sys;print(json.load(open(sys.argv[1]))[sys.argv[2]])" \
    "$HERE/pinned-revision.json" "$1"
}

PINNED_SHA="$(read_pinned forgeLoopGitCommit)"
PINNED_VERSION="$(read_pinned forgeLoopPackageVersion)"
TASK_ID="$(read_pinned taskId)"

PYTHON_BIN="${PYTHON:-}"
if [ -z "$PYTHON_BIN" ]; then
  if [ -x "$BRIDGE/.venv/bin/python" ]; then PYTHON_BIN="$BRIDGE/.venv/bin/python"; else PYTHON_BIN=python3; fi
fi

for tool in node git tar "$PYTHON_BIN"; do
  command -v "$tool" >/dev/null 2>&1 || { echo "run-proof: missing required tool: $tool" >&2; exit 2; }
done

WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/forgeloop-9-12-proof.XXXXXX")"
cleanup() {
  if [ "${KEEP_WORKDIR:-0}" = 1 ]; then
    echo "run-proof: workdir kept at $WORKDIR"
  else
    rm -rf "$WORKDIR"
  fi
}
trap cleanup EXIT

# ── materialize the pinned ForgeLoop revision ─────────────────────────────────
#
# git archive extracts the exact tree at the pinned SHA without registering a
# worktree, so the source checkout is never mutated. ForgeLoop declares no
# runtime dependencies, so the extracted tree runs as-is.

FL_ROOT=""
if [ -n "${FORGELOOP_PACKAGE_ROOT:-}" ]; then
  FL_ROOT="$FORGELOOP_PACKAGE_ROOT"
  echo "run-proof: using pre-materialized ForgeLoop at $FL_ROOT"
else
  FL_REPO="${FORGELOOP_REPO:-}"
  if [ -z "$FL_REPO" ]; then
    for candidate in "$BRIDGE/../forgeloop" "$HOME/Documents/github/forgeloop" "$HOME/forgeloop"; do
      if [ -e "$candidate/.git" ]; then FL_REPO="$candidate"; break; fi
    done
  fi
  if [ -z "$FL_REPO" ]; then
    cat >&2 <<EOF
run-proof: cannot locate a ForgeLoop checkout to materialize ${PINNED_VERSION} @ ${PINNED_SHA:0:7}.
run-proof: set FORGELOOP_REPO=/path/to/forgeloop (a git checkout containing the
run-proof: pinned commit) or FORGELOOP_PACKAGE_ROOT=/path/to/extracted/package.
run-proof: this harness will not substitute a stored fixture for the real revision.
EOF
    exit 2
  fi
  git -C "$FL_REPO" cat-file -e "${PINNED_SHA}^{commit}" 2>/dev/null || {
    echo "run-proof: $FL_REPO does not contain the pinned commit $PINNED_SHA (fetch it first)" >&2
    exit 2
  }
  FL_ROOT="$WORKDIR/forgeloop"
  mkdir -p "$FL_ROOT"
  git -C "$FL_REPO" archive "$PINNED_SHA" | tar -x -C "$FL_ROOT"
  echo "run-proof: materialized ForgeLoop $PINNED_SHA from $FL_REPO"
fi

ACTUAL_VERSION="$("$PYTHON_BIN" -c "import json,sys;print(json.load(open(sys.argv[1]))['version'])" "$FL_ROOT/package.json")"
if [ "$ACTUAL_VERSION" != "$PINNED_VERSION" ]; then
  echo "run-proof: materialized ForgeLoop is $ACTUAL_VERSION, expected $PINNED_VERSION — refusing to proceed" >&2
  exit 2
fi

FL_CLI="$FL_ROOT/src/cli.js"
PROJECT="$WORKDIR/project"
CANONICAL="$WORKDIR/canonical"
mkdir -p "$PROJECT" "$CANONICAL"

# ── build the disposable project with the pinned revision ─────────────────────

fl() { node "$FL_CLI" "$@" --path "$PROJECT"; }

echo "run-proof: preparing the disposable project"
fl init >/dev/null
fl task-create --task "$TASK_ID" >/dev/null
node "$HERE/write-contract.mjs" "$FL_ROOT" "$PROJECT" "$TASK_ID"
fl route --task "$TASK_ID" --work test-only --surface ui --platform web >/dev/null
# Preflight is expected to block: the task has unsatisfied gates, which is
# exactly the state the projection under test should describe.
fl preflight --task "$TASK_ID" --json >/dev/null || true

node "$HERE/produce-canonical.mjs" "$FL_ROOT" "$PROJECT" "$TASK_ID" "$CANONICAL"

# ── run the suites ────────────────────────────────────────────────────────────

status=0
run_suite() {
  local title="$1"; shift
  echo
  echo "═══ $title ═══"
  if "$@"; then :; else status=1; fi
}

AUDIT_REPO="${FORGELOOP_AUDIT_REPO:-}"
if [ -z "$AUDIT_REPO" ]; then
  for candidate in "$BRIDGE/../ForgeLoopAudit" "$HOME/Documents/github/ForgeLoopAudit"; do
    if [ -e "$candidate/.git" ]; then AUDIT_REPO="$candidate"; break; fi
  done
fi

if [ -n "$AUDIT_REPO" ]; then
  run_suite "audit-side (§10.1 sibling A)" node "$HERE/audit-side.mjs" "$AUDIT_REPO" "$PROJECT" "$TASK_ID" "$CANONICAL"
else
  echo
  echo "═══ audit-side (§10.1 sibling A) ═══"
  echo "SKIPPED | ForgeLoopAudit not found; set FORGELOOP_AUDIT_REPO to run it."
  echo "SKIPPED | §10.1 requires neither sibling to depend on the other, so the"
  echo "SKIPPED | Bridge determination below stands on its own."
fi

run_suite "bridge-side (§10.2 / §10.3)" "$PYTHON_BIN" "$HERE/bridge-side.py" "$CANONICAL" "$TASK_ID"
run_suite "no-change proof (§9.12)" "$PYTHON_BIN" "$HERE/no-change-proof.py" "$CANONICAL"

echo
if [ "$status" -eq 0 ]; then
  echo "run-proof: all executed suites passed"
else
  echo "run-proof: at least one suite failed" >&2
fi
exit "$status"
