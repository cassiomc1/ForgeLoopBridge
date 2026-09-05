# ForgeLoopBridge §9.12 No-Change Determination — ForgeLoop 1.10.1

## 1. Determination

    Target:      ForgeLoop 1.10.1 @ b6802b8
    Audit side:  8/8 PASS
    Bridge side: 24/24 PASS
    No-change:   20/20 rows proven
    Verdict:     BRIDGE: NO_CHANGE_REQUIRED

ForgeLoop moved from 1.10.0 to 1.10.1. The only Bridge change the move
required was already made separately: the Bridge now declares the ForgeLoop
protocol and Integration API versions it supports and fails closed on anything
outside that set (`fix(forgeloop): declare the supported version set and fail
closed on it`, #34). This document determines that **no further Bridge change is
required** by any remaining 1.10.1 semantic, and records the evidence.

The determination is executable. Every number above is produced by the harness
in `proof/`, which regenerates the canonical projection from the pinned revision
on each run rather than reading a stored fixture. Nothing here rests on a
prose claim that a reviewer would have to take on trust.

## 2. Revisions

| Component | Value |
| --- | --- |
| ForgeLoop target | package 1.10.1, git `b6802b8b5d0cb7e8edbf811350d9a94f4cb1942d` (`v1.10.1`) |
| ForgeLoop boundary | protocolVersion 1, schemaVersion 1, integrationApi v1, 83 commands, 27 capability keys |
| ForgeLoopBridge | 2.1.3, base `ee596d35c70f448337c1e10950c9f669b4f25652` (#34) |
| Bridge declared support | `SUPPORTED_FORGELOOP_PROTOCOL_VERSIONS = (1,)`, `SUPPORTED_FORGELOOP_INTEGRATION_API_VERSIONS = (1,)` |
| ForgeLoopAudit | vendored ForgeLoop re-pinned to 1.10.1 @ b6802b8 (#90); fail-closed on task-context mismatch and ABORTED (#88) |
| Environment | macOS 26.6.2 (darwin, arm64), Node v26.8.1, Python 3.14.6 |
| Determined at | 2026-09-05 |

## 3. What the harness proves

The projection under test is real. The harness materializes ForgeLoop at the
pinned SHA, has that revision create a disposable project, writes the task
contract through ForgeLoop's own `writeContract` API, routes it, and then reads
`task/context` through ForgeLoop's own Integration API. The resulting canonical
state is fixed and reproducible:

    taskId=xrepo-1 schemaVersion=1 protocolVersion=1
    phase=ROUTED nextAction=SATISFY_GATES
    executionProfile: requested=auto floor=light resolved=light escalated=false
      reasons=[PROFILE_DEFAULT_AUTO, WORK_LOW_RISK_UI,
               NO_HIGH_RISK_SIGNAL, NARROW_DELIVERABLE_SCOPE]

### 3.1 audit-side — §10.1 sibling A, 8/8 PASS

ForgeLoopAudit is exercised through its **own vendored** ForgeLoop runtime, not
through the pinned source tree, so this tests the artifact Audit actually ships.

| # | Check | Evidence |
| --- | --- | --- |
| A1 | Vendored runtime matches the recorded lineage 1.10.1 @ b6802b8 | provenance=1.10.1, installed=1.10.1 |
| A2 | Protocol boundary identical between source revision and vendored runtime | byte-identical `protocol-info` |
| A3 | Capability set identical (27 keys), integrationApi v1 both sides | 27 keys |
| A4 | Task identity preserved through Audit's read-only projection | `taskId=xrepo-1 claimState=ACTIVE ownershipValid=true` |
| A5 | Lifecycle phase and ownership read without inference | `phase=ROUTED recovery=null` |
| A6 | Spawn allowlist contains no mutating command | 10 allowlisted commands, none mutating |
| A7 | Audit inspection mutated nothing under `.forgeloop` | tree digest identical before/after |
| A8 | Bridge is not required for Audit inspection | no Bridge in Audit runtime dependencies |

A7 is the one that matters: read-only in fact, not only by policy.

### 3.2 bridge-side — §10.2 invariants and §10.3 negatives, 24/24 PASS

ForgeLoopAudit is not involved anywhere in this suite (§10.1).

| # | Check |
| --- | --- |
| B1 | Real canonical projection accepted as canonical (`FORGELOOP_CANONICAL`) |
| B2 | Task identity preserved, not re-derived |
| B3 | Protocol version interpretation agrees with the canonical boundary |
| B4 | Resolved execution profile transported, never chosen locally |
| B5 | Lifecycle phase and next action are copies of canonical values |
| B6 | Verification semantics transported without promotion |
| B7 | Canonical invariants preserved verbatim; phase skipping refused |
| B8 | Capability present → feature enabled, canonical result preserved |
| B9 | Capability absent → explicit balanced compatibility, no false positive |
| B10 | Capability absent → no mutation fallback, no invented authority |
| B11 | Unsupported capability version → UNAVAILABLE, nothing inferred |
| B12 | Non-integer advertised version → UNAVAILABLE, not coerced |
| B13 | Unknown protocol version → fail closed, no balanced downgrade |
| B14 | Unknown schema version → fail closed |
| B15 | Projection from an unknown generation → fail closed |
| B16 | Capability advertised without a declared protocol version → fail closed |
| B17 | Supported legacy boundary → explicit compatibility mode, limited, no authority |
| B18 | Package version alone never decides compatibility |
| B19 | No raw-artifact ownership inference anywhere in Bridge runtime |
| B20 | Projection for a different task is refused |
| B21 | `COMMIT_UNKNOWN` → hard stop, reconciliation required, no retry |
| B22 | Bridge text saying "approved" never satisfies canonical approval |
| B23 | Missing evidence is never promoted to VALID/PASS |
| B24 | Audit is not required for Bridge compatibility |

B13 and B18 together are the point of #34: an unknown protocol version must fail
closed, and a package-version string must never be what decides compatibility.
B9/B10 confirm the other direction — when a capability is genuinely absent, the
Bridge degrades to an explicit, authority-free compatibility mode instead of
inventing state.

## 4. §9.12 obligations

| Obligation | Result | Evidence |
| --- | --- | --- |
| a. Declared support still covers the new target | NO CHANGE | declared protocol `(1,)` / API `(1,)` vs target 1 / 1; `forgeloop_boundary_status` returns SUPPORTED |
| b. Changed capabilities are not consumed, or are safely optional | NO CHANGE | read surface is `adaptiveExecutionProfiles`, `executionProfileContext`, `integrationApi` — all still v1 |
| c. No new next/recovery/approval/action/completion semantics invalidate Worker guidance | NO CHANGE | 21 commands named in Bridge guidance, all advertised by the target |
| d. Examples do not teach a now-invalid workflow | NO CHANGE | checked against the target's 83-command surface |

Obligations c and d are checked against the real command surface, not against
memory: every `forgeloop <command>` named in `README.md`,
`examples/worker_poll.py` and `examples/AUTONOMY.md` is intersected with the 83
commands `protocol-info` advertises at the pinned revision. Had 1.10.1 retired a
command the examples teach, that intersection would have failed.

## 5. §6 semantic inventory determination

Each row is a 1.10.1 change from the Gate A §6 inventory. "NO" means: not
consumed by the Bridge, or consumed in a way that is safely optional.

| # | 1.10.1 change | Result | Evidence |
| --- | --- | --- | --- |
| 1 | `protocol-info.packageVersion` bump | NO | no version-comparison machinery in RUNTIME or TESTS (no `parse_version`, `LooseVersion`, `packaging.version`, `semver`, `pkg_resources`) |
| 2 | `.txn/<id>/manifest.json` gains `lockTaskId` | NO | no `.txn` or `lockTaskId` in RUNTIME |
| 3 | Transaction status gains `ABORTED` | NO | no `ABORTED` / `ROLLED_BACK` / `ABANDONED` in RUNTIME |
| 4 | Transaction compaction; `stage`+`backup` absent | NO | no `compactedAt` / `E_TRANSACTION` in RUNTIME |
| 5 | `doctor`/`inspect` terminal `ROLLED_BACK`+`ABORTED` verdicts | NO | RUNTIME spawns `protocol-info` only; `doctor` appears nowhere in RUNTIME |
| 6 | `doctor --fix` no longer double-reports | NO | same evidence as row 5 |
| 7 | New fail-closed `E_TASK_CONTEXT_MISMATCH` | NO | no reference in PROSE |
| 8 | Recovery under task lock → `RECOVERY_FAILED` | NO | no raw recovery-artifact read in RUNTIME |
| 9 | Rollback replay uses symlink-aware `assertSafePath` | NO | no ForgeLoop rollback identifier in RUNTIME, and no RUNTIME write under `.forgeloop/`; path safety stays inside ForgeLoop (cf. B19) |
| 10 | `next` gains `commands` + `commandSpecs` | NO | the projection consumer reads named fields only; unknown additive fields are ignored, never rejected |
| 11 | `route` profile obligation signals normalized | NO | resolved profile transported opaquely via `model_dump`; no `E_ROUTE_STALE` / `E_ROUTE_GUIDE_MISMATCH` / local derivation |
| 12 | New export `FORGELOOP_INTEGRATION_RUNTIME_VERSION` | NO | no JS import of `@cassiomc1/forgeloop`; the Bridge is a Python consumer |
| 13 | `ForgeLoopStructuralQualityProvider` interface → union | NO | type-level change in a package the Bridge never imports |
| 14 | 4 retired modules excluded from the tarball | NO | no deep-path import of the package |
| 15 | Absent lifecycle receipt reported `NOT_VERIFIED` | NO | the Bridge's `NOT_VERIFIED` prose is attestation-scoped and remains correct |
| 16 | MCP dependency pinned exact + lockfile committed | NO | ForgeLoop-internal; no `modelcontextprotocol` in RUNTIME |

## 6. Scope model

A bare text search over the whole repository proves nothing about what the
Bridge consumes. It hits prose that *forbids* a behaviour, captured third-party
evidence, and the proof harness's own vocabulary. Every claim above is therefore
made against a named scope, defined once in `proof/bridge_scopes.py`:

| Scope | Contents | Size |
| --- | --- | --- |
| RUNTIME | executable Python the Bridge runs: root `*.py`, `bridge_protocol/`, `examples/`, `scripts/` | 12 files |
| TESTS | `tests/*.py` | 7 files |
| PROSE | tracked text, excluding the frozen scopes below | 39 files |

RUNTIME is an allowlist of real runtime paths, not a blocklist, so documentation
and proof code can never silently enter it.

Frozen scopes, excluded from PROSE by name:

- **`docs/audits/`** — captured evidence, deliberately not updated when ForgeLoop
  moves. It includes a verbatim copy of ForgeLoop's own `protocol-info` with all
  275 public error codes, so a text search would "find" every canonical error
  name. This directory also holds the proof harness itself, which necessarily
  names the very surfaces it proves absent from runtime.
- **`FORGELOOPBRIDGE_FORGELOOP_1_5_UPDATE_PLAN.md`** — marked HISTORICAL /
  SUPERSEDED in its own header; retained for lineage, not guidance.

## 7. Method note: lexical absence is not proof

The first pass of this proof reported 12 failures. All 12 were artifacts of
crude text matching, not real findings:

- `--fix` matched `--fixed` in a test environment variable.
- `E_TASK_CONTEXT_MISMATCH`, `E_ROUTE_STALE` and `recovery.json` matched inside
  `docs/audits/post-fix-regression/evidence/forgeloop-protocol-info.json`, a
  captured copy of ForgeLoop's own output correctly frozen at 1.10.0.
- `@cassiomc1/forgeloop` matched README prose whose purpose is to *forbid*
  deriving canonical state.
- `forgeloop artifacts` matched `.forgeloop artifacts` — a path, not a command.

Row 9 needed three attempts, because `rollback` and `replay` legitimately exist
in the Bridge: a database session rollback (`main.py:851`) and message-board
history replay (`examples/worker_poll.py:1219`). Line-scoped classification kept
producing false positives on docstring prose that wraps across lines. The fix
was to stop searching lexically and ask the structural question instead: does
RUNTIME write anything under `.forgeloop/` at all? It does not.

This section is recorded because the failure mode generalizes. A future revision
bump that re-runs this harness should treat a new FAIL as a question about
scoping before treating it as a finding about the Bridge — and should not relax
a scope merely to make a row pass.

## 8. Reproducing

    bash docs/audits/post-fix-regression/proof/run-proof.sh

The runner materializes the pinned revision with `git archive`, which extracts
the exact tree at the pinned SHA **without registering a worktree**, so the
ForgeLoop checkout is never mutated. ForgeLoop declares no runtime dependencies,
so the extracted tree runs as-is. The scratch directory is removed on exit.

| Variable | Purpose |
| --- | --- |
| `FORGELOOP_REPO` | ForgeLoop git checkout to archive the pinned SHA from (auto-discovered if adjacent) |
| `FORGELOOP_PACKAGE_ROOT` | pre-materialized package root at the pinned revision |
| `FORGELOOP_AUDIT_REPO` | ForgeLoopAudit checkout; enables the audit-side suite |
| `PYTHON` | interpreter with the Bridge requirements installed |
| `KEEP_WORKDIR=1` | keep the scratch directory for inspection |

Behaviour worth knowing:

- **Fails closed.** If the pinned commit cannot be materialized, or the
  materialized package reports a version other than 1.10.1, the run exits 2. It
  never substitutes a stored fixture for the real revision.
- **Deterministic.** `TZ=UTC` and `LC_ALL=C` are set by the runner; output is
  byte-identical across runs and across working directories, except the
  `.forgeloop` tree digest in A7, which is content-derived and only ever
  compared against itself.
- **Audit is optional.** §10.1 requires neither sibling to depend on the other,
  so when ForgeLoopAudit is absent the audit-side suite reports SKIPPED and the
  Bridge determination stands on its own. When it is present but its
  dependencies are not installed, that suite fails closed rather than degrading
  to a weaker check — its whole purpose is to test the vendored artifact.

## 9. Files

| Path | Role |
| --- | --- |
| `proof/run-proof.sh` | deterministic runner: materialize, build project, run three suites |
| `proof/pinned-revision.json` | single source of truth for the pinned revision and expected boundary |
| `proof/write-contract.mjs` | writes the task contract through ForgeLoop's own `writeContract` API |
| `proof/produce-canonical.mjs` | regenerates `protocol-info.json` and `task-context.json` from the pinned revision |
| `proof/audit-side.mjs` | §10.1 sibling A, 8 checks |
| `proof/bridge-side.py` | §10.2 / §10.3, 24 checks |
| `proof/no-change-proof.py` | §9.12 inventory determination, 20 rows |
| `proof/bridge_scopes.py` | the RUNTIME / TESTS / PROSE scope model and frozen exclusions |

## 10. What this determination does not claim

- It does not claim the Bridge exercises the new 1.10.1 behaviours. It claims the
  opposite: those behaviours sit inside ForgeLoop's own boundary, and the Bridge
  neither consumes nor contradicts them.
- It does not re-verify ForgeLoop's own correctness. `doctor`, transaction
  compaction and rollback path safety are ForgeLoop's to test.
- It does not extend to future ForgeLoop revisions. A new revision requires
  re-running the harness against a new pinned SHA; a protocol or schema version
  outside the declared set will fail closed by design, which is the intended
  signal to revisit this determination rather than to widen the set.
