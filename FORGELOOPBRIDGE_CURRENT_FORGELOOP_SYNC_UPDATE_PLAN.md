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
Observed synchronization baseline: ForgeLoop package 1.10.1
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

## Optional ForgeLoop capability boundary (families introduced in ForgeLoop 1.6.4)

The optional capability families first documented with ForgeLoop 1.6.4 remain
additive Protocol v1 capabilities. ForgeLoop Protocol v1 may advertise
`workspaceBinding`, `canonicalHandoffs`, `responsibilityConstraints`,
`differentialVerificationScope`, and `codeAttestation`, alongside the existing
`verificationExecutionIsolation` and `observabilityStability` surfaces.

ForgeLoopBridge coordinates hosts that may use these ForgeLoop capabilities. It
does not implement, validate, infer, or attest them. The Bridge transports only
task IDs, opaque artifact references, copied canonical results/reason codes,
bounded summaries, and relevant PR/publication URLs.

### Structural Quality

ForgeLoop 1.10.0 may advertise the provider-neutral `structuralQuality` feature,
the canonical `task/structural-quality` resource, and the
`quality-baseline`, `quality-verify`, and `quality-status` command identities.
Bridge preserves these as capability metadata or opaque canonical results when
advertised. `quality-status` is read-only; observation is invoked only through
the authorized canonical ForgeLoop execution boundary. Bridge never calculates
quality, runs Sentrux as a hidden authority, self-attests freshness, turns
`NOT_OBSERVED` into PASS, invents provider compatibility, or infers support
from package version.

### Workspace binding

Workspace identity and binding are canonical ForgeLoop facts. A path, branch,
copied checkout, container name, or Bridge message cannot prove a match. When
ForgeLoop reports `E_WORKSPACE_IDENTITY_UNAVAILABLE`,
`E_WORKSPACE_BINDING_INVALID`, or `E_WORKSPACE_BINDING_MISMATCH`, report the
exact blocker, do not silently rebind, and follow canonical `next` guidance.

### Canonical handoffs

`canonicalHandoffs` v2 handoffs are immutable continuity snapshots, not
delegation authority, identity proof, approval, completion evidence, or
verification evidence. The canonical statuses are `OPEN`, `ACCEPTED`, `UNBOUND`,
and `INCONSISTENT`; exactly-once acceptance is controlled by ForgeLoop's
`handoff-accept` command. The Bridge may carry an opaque handoff reference and
copy the canonical result, but must not validate its digest, infer acceptance,
or treat the reference as authority.

`HANDOFF_NOTICE` is not handoff acceptance. Only the receiving harness that
actually consumes the canonical handoff may invoke `handoff-accept`. Receiving a
Bridge message, opening the Bridge UI, or advancing a Bridge cursor must never
append `HANDOFF_ACCEPTED`. Canonical acceptance is an operational receipt only:
`authority: OPERATIONAL_RECEIPT_ONLY`, `evidence: NONE`, and no claims
transferred. Bridge does not create authority/evidence from acceptance.

Relevant failures include `E_HANDOFF_INVALID`,
`E_HANDOFF_STATE_UNAVAILABLE`, `E_HANDOFF_TAMPERED`, `E_HANDOFF_NOT_FOUND`,
`E_HANDOFF_ACCEPTANCE_UNBOUND`, `E_HANDOFF_STALE`,
`E_HANDOFF_ALREADY_ACCEPTED`, and `E_HANDOFF_ACCEPTANCE_INCONSISTENT`.

### Advisory context providers

ForgeLoop 1.10.0 may advertise the optional `advisoryContextProviders` v1
capability with this trust contract:

```text
version: 1
providerNeutral: true
integrationApiOnly: true
lazy: true
optIn: true
persistedByForgeLoop: false
lifecycleAuthority: false
evidenceAuthority: false
executable: false
```

Bridge never creates a provider, auto-recalls context when a message arrives,
turns a message into a provider result, or persists raw provider output as
canonical ForgeLoop state. A bounded host-produced summary may be transported
as ordinary coordination text, but it remains non-authoritative,
non-evidence, and non-executable. There is no Bridge memory or recall endpoint;
a provider adapter requires a separate design and release.

### Continuity diagnostics

The Worker may use `forgeloop reconcile-continuity --task <id> --json` as a
read-only resume diagnostic. `CONTINUITY_REMAINING_ALREADY_COMPLETED`,
`CONTINUITY_FOCUS_ALREADY_COMPLETED`, `CONTINUITY_ITEM_ROLE_CONFLICT`,
`CONTINUITY_INSPECT_PATH_MISSING`, and `CONTINUITY_EMPTY_HINT_SET` are
non-authoritative operational lint results. A lint warning is not a Bridge
blocker, verification failure, or completion failure; actual lifecycle action
always follows canonical `forgeloop next`.

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
`bridge_api_version: 2.1.3` with `typed_message_versions: [1]` and the
`typed_features` capability map in the public status response. Typed messages
retain mandatory Markdown content and carry typed coordination intent,
correlation/reply metadata, idempotency keys, and opaque canonical references.
Strict validation, role-scoped message idempotency, cross-role reply linkage,
and persisted typed-integrity reporting belong to Bridge transport; they do not
create ForgeLoop task lifecycle, approval, verification, or attestation
authority.

The typed surface includes `DECISION_NOTICE` for a unilateral project decision.
`DECISION_REQUEST` expects a reply, while `DECISION_RESPONSE` and
`DECISION_NOTICE` do not. Decision option IDs are unique and recommendations
must reference declared options. Verification reports preserve deprecated v1
`scope_mode` while new clients may distinguish `requested_scope_mode` from
`resolved_scope_mode`; `AUTO` is a request, never a resolved value.

`MessageOut` explicitly reports `typed_integrity` as `VALID`,
`NOT_APPLICABLE`, or `INVALID`. Invalid persisted typed data keeps Markdown
visible for diagnosis but must not be dispatched as Markdown fallback, and a
Worker must not advance its cursor. Typed envelopes are capped at 65,536
normalized UTF-8 JSON bytes before database insertion or SSE broadcast.

The example Worker's typed outbox stores only the exact request and stable
message key, never authentication, cookie, SSE-ticket, signing, or OIDC
material. It atomically replaces a bounded file, replays it before polling,
retains network, 408, 425, 429, and 5xx failures, honors bounded
`Retry-After`/backoff without blocking normal polling, and quarantines only
permanent transport/protocol rejection and corrupt outbox files.

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
- The public status response advertises `idempotency`, `correlation`,
  `reply_linkage`, `canonical_refs`, `outbox_safe_retry`, and
  `typed_integrity_status` as Bridge transport features.
- Posting and SSE-ticket rate-limit responses use HTTP 429 with a bounded
  integer `Retry-After` value. A 429 is Bridge transport backpressure, not a
  ForgeLoop task failure or canonical blocker; clients retain the original
  typed `message_key` when retrying delivery.

## Verification expectation

Use the current source, tests, and advertised ForgeLoop capabilities when
working in this repository. Do not infer compatibility or canonical state from
package versions, prose, copied badges, or this record alone.
