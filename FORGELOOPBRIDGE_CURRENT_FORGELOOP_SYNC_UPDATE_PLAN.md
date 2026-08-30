# ForgeLoopBridge — Current ForgeLoop Synchronization Record

> **Current record:** this document is the source-of-truth index for the
> Bridge's present ForgeLoop compatibility boundary. The original
> `FORGELOOPBRIDGE_FORGELOOP_1_5_UPDATE_PLAN.md` remains a historical plan and
> points here for current alignment.

## Compatibility target

ForgeLoopBridge coordinates Engineer and Worker agents against **ForgeLoop
Protocol v1** and **Integration API v1**. Before creating or resuming canonical
ForgeLoop state, the active host must inspect `forgeloop protocol-info --json`
or the equivalent official structured integration capability response.

```text
Protocol compatibility target: ForgeLoop Protocol v1
Integration API compatibility target: Integration API v1
Observed synchronization baseline: ForgeLoop package 1.6.4
```

The package baseline is informational only. Capability support comes from the
canonical protocol-info or structured integration response, never from a
package version comparison.

The Bridge accepts additive coordination references such as `task_id`,
`message_type`, `action_id`, `approval_id`, `next_action`, and `reason_code`.
These values are reported copies and opaque references. They are not canonical
ForgeLoop state, approvals, authority grants, lifecycle transitions, or
verification evidence.

## Authority boundary

ForgeLoop remains the sole authority for lifecycle, claims, recovery, actions,
approvals, capability policy, diagnostics, evidence, and completion. The Bridge
only transports Markdown conversation, status summaries, decision records,
blockers, and pull-request references. An Engineer/Worker agreement on the
Bridge cannot manufacture trusted host authority or canonical approval.

## ForgeLoop 1.6.4 capability boundary

ForgeLoop Protocol v1 may advertise the additive optional capabilities
`workspaceBinding`, `canonicalHandoffs`, `responsibilityConstraints`,
`differentialVerificationScope`, and `codeAttestation`, alongside the existing
`verificationExecutionIsolation` and `observabilityStability` surfaces.

ForgeLoopBridge coordinates hosts that may use these ForgeLoop capabilities. It
does not implement, validate, infer, or attest them. The Bridge transports only
task IDs, opaque artifact references, copied canonical results/reason codes,
bounded summaries, and relevant PR/publication URLs.

### Workspace binding

Workspace identity and binding are canonical ForgeLoop facts. A path, branch,
copied checkout, container name, or Bridge message cannot prove a match. When
ForgeLoop reports `E_WORKSPACE_IDENTITY_UNAVAILABLE`,
`E_WORKSPACE_BINDING_INVALID`, or `E_WORKSPACE_BINDING_MISMATCH`, report the
exact blocker, do not silently rebind, and follow canonical `next` guidance.

### Canonical handoffs

Handoffs are immutable continuity snapshots, not delegation authority, identity
proof, approval, completion evidence, or verification evidence. The Bridge may
carry an opaque handoff reference, but must not validate its digest or treat the
reference as authority. Relevant failures include `E_HANDOFF_INVALID`,
`E_HANDOFF_STATE_UNAVAILABLE`, `E_HANDOFF_TAMPERED`, and
`E_HANDOFF_NOT_FOUND`.

### Responsibility constraints

Responsibility contracts and their allowed/read-only paths, required checks, and
frozen-input fingerprints remain canonical ForgeLoop state. A board decision
cannot waive `E_RESPONSIBILITY_SCOPE_VIOLATION`,
`E_RESPONSIBILITY_FROZEN_INPUT_DRIFT`, or
`E_RESPONSIBILITY_REQUIRED_CHECK_MISSING`.

### Differential verification scope

Only ForgeLoop may calculate `AUTO`, `CHANGED`, `CLAIMED`, or `FULL` scope. A
trusted scoped checker is required for narrow `CHANGED`/`CLAIMED` execution;
without it, canonical `AUTO` falls back to `FULL`, while an explicit narrow
request fails closed. Verification scope is not verification evidence and is not
revision-range coverage. The Bridge never calculates impacted paths or creates
an executable argv. It reports `E_VERIFICATION_SCOPE_INVALID`,
`E_VERIFICATION_SCOPE_STALE`, and `E_VERIFICATION_SCOPE_UNRESOLVED` as copied
canonical results.

### Revision providers, attestation, and signing

Worker and Engineer prompts use the canonical revision provider and opaque
revision IDs rather than assuming Git. ForgeLoop owns code manifests,
attestation statements, revision-range verification, and trust status. The
Bridge distinguishes `NOT_VERIFIED`, `VERIFIED`, and `ATTESTED` but cannot
promote one state to another. `ATTESTED` requires the canonical external
signature boundary; a Bridge `ATTESTED` message is only a copied report. Private
keys, OIDC credentials, access tokens, and signing-provider secrets never enter
Bridge messages or storage.

Revision boundary errors include `E_REVISION_PROVIDER_UNAVAILABLE`,
`E_REVISION_PROVIDER_AMBIGUOUS`, `E_REVISION_PROVIDER_INVALID`,
`E_REVISION_NOT_FOUND`, and `E_REVISION_CONTENT_UNAVAILABLE`. Attestation
errors such as `E_ATTESTATION_CONTENT_MISMATCH`,
`E_ATTESTATION_COVERAGE_GAP`, `E_ATTESTATION_SIGNATURE_INVALID`, and
`E_ATTESTATION_IDENTITY_UNTRUSTED` remain canonical; the Bridge reports them
without adding recovery or verification authority.

## Verification execution isolation boundary

The current ForgeLoop Protocol v1 may advertise
`features.verificationExecutionIsolation` and
`features.observabilityStability`. ForgeLoopBridge does not provide, infer, or
attest verification isolation; it only coordinates hosts that may support the
trusted ForgeLoop execution adapter.

The canonical adapter distinguishes `NATIVE_PROJECT`, `PROJECT_ISOLATED`, and
`SYSTEM_ISOLATED`. `protocolProjectRoot` is not necessarily the verification
execution cwd, and a separate cwd or copied directory is not proof that the
live project is protected. `liveProjectWritable=false` is a trusted host
guarantee, not a Bridge assertion.

When canonical ForgeLoop reports:

- `E_VERIFICATION_ISOLATION_UNAVAILABLE`
- `E_VERIFICATION_EXECUTION_INVALID`

the Bridge reports the exact blocker and agents follow canonical ForgeLoop
recovery/next guidance. They must not weaken isolation, switch execution modes
to bypass a requirement, synthesize execution provenance, edit ForgeLoop-owned
artifacts, or treat Bridge agreement as trusted adapter evidence.

## Typed Bridge coordination surface

ForgeLoopBridge additionally exposes **Bridge Typed Message Schema v1**. This
is separate from ForgeLoop Protocol v1 and is advertised as
`bridge_api_version: 2.1.0` with `typed_message_versions: [1]` in the public
status response. Typed messages retain mandatory Markdown content and carry
typed coordination intent, correlation/reply metadata, idempotency keys, and
opaque canonical references. Strict validation, role-scoped message
idempotency, and cross-role reply linkage belong to Bridge transport; they do
not create ForgeLoop task lifecycle, approval, verification, or attestation
authority.

## Current coordination surfaces

- REST messages use authenticated Bearer access and cursor-based
  `after_id`/`before_id` recovery.
- Browser SSE uses a short-lived stream ticket; SSE is a low-latency hint, not
  the canonical delivery journal.
- Every SSE subscriber has bounded buffering.
- Queue overflow explicitly closes the affected stream.
- The browser then falls back to REST `after_id` reconciliation and requests a
  fresh SSE ticket.
- Worker instructions are processed before their local cursor advances, so
  failures result in safe at-least-once redelivery.
- `COMMIT_UNKNOWN` handling is triggered only by explicit reported control
  metadata and always remains a hard stop pending canonical reconciliation.
- Typed REST and SSE messages use the same normalized envelope. REST remains
  the reconciliation source after SSE overflow; typed data is recovered with
  `after_id` and is never interpreted as canonical ForgeLoop truth.

## Verification expectation

Use the current source, tests, and advertised ForgeLoop capabilities when
working in this repository. Do not infer compatibility or canonical state from
package versions, prose, copied badges, or this record alone.
