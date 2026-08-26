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

## Verification expectation

Use the current source, tests, and advertised ForgeLoop capabilities when
working in this repository. Do not infer compatibility or canonical state from
package versions, prose, copied badges, or this record alone.
