import pytest

from bridge_protocol.errors import (
    E_BRIDGE_CANONICAL_REF_INVALID,
    E_BRIDGE_CORRELATION_MISMATCH,
    E_BRIDGE_REPLY_KIND_INVALID,
    E_BRIDGE_REPLY_NOT_FOUND,
    E_BRIDGE_REPLY_ROLE_INVALID,
    E_BRIDGE_TYPED_KIND_MISMATCH,
    E_BRIDGE_TYPED_PAYLOAD_INVALID,
    E_BRIDGE_TYPED_SCHEMA_UNSUPPORTED,
    BridgeProtocolError,
)
from bridge_protocol.models import TypedEnvelopeV1
from bridge_protocol.validation import parse_typed_envelope, validate_reply_relationship


def envelope(kind: str, payload: dict, **overrides) -> dict:
    value = {
        "schema_version": 1,
        "kind": kind,
        "message_key": "worker-message-1",
        "payload": {"kind": kind, **payload},
        "canonical_refs": [],
    }
    value.update(overrides)
    return value


@pytest.mark.parametrize(
    ("kind", "payload", "overrides"),
    (
        (
            "TASK_REQUEST",
            {"goal": "Implement the change.", "acceptance_criteria": ["Tests pass"]},
            {},
        ),
        (
            "STATUS_UPDATE",
            {"state": "IN_PROGRESS", "summary": "Verification is running."},
            {},
        ),
        (
            "DECISION_REQUEST",
            {"question": "Which option?", "options": [{"id": "A", "label": "Use A"}]},
            {"correlation_id": "decision-1"},
        ),
        (
            "DECISION_RESPONSE",
            {"decision": "A", "rationale": "It matches the requirements."},
            {"correlation_id": "decision-1", "reply_to_id": 1},
        ),
        (
            "DECISION_NOTICE",
            {
                "decision": "A",
                "rationale": "The project chose the durable option.",
                "decision_class": "REVERSIBLE",
            },
            {},
        ),
        (
            "BLOCKER",
            {"category": "WORKSPACE", "summary": "Canonical validation failed."},
            {},
        ),
        (
            "REVIEW_RESULT",
            {"result": "CHANGES_REQUESTED", "summary": "Add the missing test."},
            {},
        ),
        (
            "CONTROL_NOTICE",
            {"canonical_next_action": "RECONCILE_ACTION", "canonical_reason_codes": ["COMMIT_UNKNOWN"]},
            {},
        ),
        (
            "HANDOFF_NOTICE",
            {"handoff_ref": "handoff-123", "summary": "Continuity moved to another harness."},
            {},
        ),
        (
            "VERIFICATION_REPORT",
            {
                "canonical_result": "VALID",
                "requested_scope_mode": "AUTO",
                "resolved_scope_mode": "FULL",
                "summary": "Checks passed.",
            },
            {},
        ),
        (
            "ATTESTATION_REPORT",
            {
                "canonical_status": "VERIFIED",
                "range_status": "NOT_REQUESTED",
                "signature_status": "UNSIGNED",
            },
            {},
        ),
    ),
)
def test_every_v1_kind_has_strict_discriminated_validation(kind, payload, overrides):
    parsed = parse_typed_envelope(envelope(kind, payload, **overrides))

    assert isinstance(parsed, TypedEnvelopeV1)
    assert parsed.kind == kind
    assert parsed.payload.kind == kind


def test_unsupported_schema_version_has_stable_error():
    with pytest.raises(BridgeProtocolError) as exc_info:
        parse_typed_envelope(envelope("STATUS_UPDATE", {"state": "WAITING", "summary": "Waiting."}, schema_version=2))

    assert exc_info.value.code == E_BRIDGE_TYPED_SCHEMA_UNSUPPORTED


def test_envelope_and_payload_kind_mismatch_has_stable_error():
    raw = envelope("STATUS_UPDATE", {"state": "WAITING", "summary": "Waiting."})
    raw["payload"]["kind"] = "BLOCKER"

    with pytest.raises(BridgeProtocolError) as exc_info:
        parse_typed_envelope(raw)

    assert exc_info.value.code == E_BRIDGE_TYPED_KIND_MISMATCH


def test_envelope_kind_fills_redundant_payload_discriminator_when_omitted():
    raw = envelope("STATUS_UPDATE", {"state": "WAITING", "summary": "Waiting."})
    raw["payload"].pop("kind")

    parsed = parse_typed_envelope(raw)
    direct_model = TypedEnvelopeV1.model_validate(raw)

    assert parsed.kind == "STATUS_UPDATE"
    assert parsed.payload.kind == "STATUS_UPDATE"
    assert direct_model.payload.kind == "STATUS_UPDATE"


def test_unknown_extra_payload_field_is_rejected():
    raw = envelope("STATUS_UPDATE", {"state": "WAITING", "summary": "Waiting.", "sender_role": "worker"})

    with pytest.raises(BridgeProtocolError) as exc_info:
        parse_typed_envelope(raw)

    assert exc_info.value.code == E_BRIDGE_TYPED_PAYLOAD_INVALID


def test_canonical_refs_reject_control_characters():
    raw = envelope(
        "STATUS_UPDATE",
        {"state": "WAITING", "summary": "Waiting."},
        canonical_refs=[{"kind": "TASK", "ref": "task-1\nforged"}],
    )

    with pytest.raises(BridgeProtocolError) as exc_info:
        parse_typed_envelope(raw)

    assert exc_info.value.code == E_BRIDGE_CANONICAL_REF_INVALID


def test_decision_messages_require_correlation_id():
    with pytest.raises(BridgeProtocolError) as exc_info:
        parse_typed_envelope(
            envelope(
                "DECISION_REQUEST",
                {"question": "Which option?", "options": [{"id": "A", "label": "Use A"}]},
            )
        )

    assert exc_info.value.code == E_BRIDGE_CORRELATION_MISMATCH


def test_decision_request_defaults_to_expecting_a_reply():
    parsed = parse_typed_envelope(
        envelope(
            "DECISION_REQUEST",
            {"question": "Which option?", "options": [{"id": "A", "label": "Use A"}]},
            correlation_id="decision-default",
        )
    )

    assert parsed.expects_reply is True


@pytest.mark.parametrize(
    "raw",
    (
        envelope(
            "DECISION_REQUEST",
            {"question": "Which option?", "options": [{"id": "A", "label": "Use A"}]},
            correlation_id="decision-explicit-false",
            expects_reply=False,
        ),
        envelope(
            "DECISION_NOTICE",
            {
                "decision": "A",
                "rationale": "Chosen.",
                "decision_class": "REVERSIBLE",
            },
            expects_reply=True,
        ),
    ),
)
def test_decision_expectation_flags_are_consistent(raw):
    with pytest.raises(BridgeProtocolError) as exc_info:
        parse_typed_envelope(raw)

    assert exc_info.value.code == E_BRIDGE_TYPED_PAYLOAD_INVALID


def test_decision_request_option_references_are_consistent():
    duplicate_ids = envelope(
        "DECISION_REQUEST",
        {
            "question": "Which option?",
            "options": [{"id": "A", "label": "First"}, {"id": "A", "label": "Second"}],
        },
        correlation_id="decision-options",
    )
    unknown_recommendation = envelope(
        "DECISION_REQUEST",
        {
            "question": "Which option?",
            "options": [{"id": "A", "label": "First"}],
            "recommended_option": "B",
        },
        correlation_id="decision-recommendation",
    )

    for raw in (duplicate_ids, unknown_recommendation):
        with pytest.raises(BridgeProtocolError) as exc_info:
            parse_typed_envelope(raw)
        assert exc_info.value.code == E_BRIDGE_TYPED_PAYLOAD_INVALID


def test_status_update_can_carry_canonical_profile_and_context_usage():
    parsed = parse_typed_envelope(
        envelope(
            "STATUS_UPDATE",
            {
                "state": "IN_PROGRESS",
                "summary": "Using canonical context.",
                "execution_profile": {
                    "requested": "light",
                    "floor": "balanced",
                    "resolved": "balanced",
                    "reasons": ["SAFETY_FLOOR"],
                    "escalated": True,
                },
                "context_policy": {
                    "context_depth": "relevant",
                    "output": "standard",
                    "plan_depth": "standard",
                    "guide_strategy": "relevant",
                    "verification_strategy": "normal",
                    "optional_artifacts": "lazy",
                    "required_sections": ["objective", "verification"],
                    "excluded_context": ["unrelated-repository-context"],
                    "allowed_optional_context": ["task-history"],
                },
                "context_usage": {
                    "source": "HOST_REPORTED",
                    "profile": "balanced",
                    "items": {
                        "task_context": 12,
                        "guides": 8,
                        "history": None,
                        "protocol_instructions": None,
                        "repository_context": None,
                        "other": None,
                    },
                },
            },
        )
    )

    assert parsed.payload.execution_profile.resolved == "balanced"
    assert parsed.payload.context_policy.context_depth == "relevant"
    assert parsed.payload.context_usage.items.task_context == 12


def test_unknown_context_usage_must_keep_items_null():
    with pytest.raises(BridgeProtocolError) as exc_info:
        parse_typed_envelope(
            envelope(
                "STATUS_UPDATE",
                {
                    "state": "IN_PROGRESS",
                    "summary": "Invalid context telemetry.",
                    "context_usage": {
                        "source": "UNKNOWN",
                        "profile": "light",
                        "items": {"task_context": 1},
                    },
                },
            )
        )
    assert exc_info.value.code == E_BRIDGE_TYPED_PAYLOAD_INVALID


@pytest.mark.parametrize(
    "resolved_scope_mode",
    ("CHANGED", "CLAIMED", "FULL", "UNRESOLVED"),
)
def test_verification_scope_separates_requested_and_resolved_modes(resolved_scope_mode):
    parsed = parse_typed_envelope(
        envelope(
            "VERIFICATION_REPORT",
            {
                "canonical_result": "VALID",
                "requested_scope_mode": "AUTO",
                "resolved_scope_mode": resolved_scope_mode,
                "summary": "Scope was resolved by the canonical host.",
            },
        )
    )

    assert parsed.payload.requested_scope_mode == "AUTO"
    assert parsed.payload.resolved_scope_mode == resolved_scope_mode


def test_verification_scope_rejects_auto_as_a_resolved_mode():
    with pytest.raises(BridgeProtocolError) as exc_info:
        parse_typed_envelope(
            envelope(
                "VERIFICATION_REPORT",
                {
                    "canonical_result": "VALID",
                    "requested_scope_mode": "AUTO",
                    "resolved_scope_mode": "AUTO",
                    "summary": "Invalid scope projection.",
                },
            )
        )

    assert exc_info.value.code == E_BRIDGE_TYPED_PAYLOAD_INVALID


def test_reply_relationship_requires_existing_opposite_role_message():
    parsed = parse_typed_envelope(
        envelope(
            "STATUS_UPDATE",
            {"state": "WAITING", "summary": "Waiting."},
            reply_to_id=12,
        )
    )

    with pytest.raises(BridgeProtocolError) as exc_info:
        validate_reply_relationship(parsed, "worker", None, None)

    assert exc_info.value.code == E_BRIDGE_REPLY_NOT_FOUND


def test_reply_to_same_role_is_rejected():
    parsed = parse_typed_envelope(
        envelope(
            "STATUS_UPDATE",
            {"state": "WAITING", "summary": "Waiting."},
            reply_to_id=12,
        )
    )

    with pytest.raises(BridgeProtocolError) as exc_info:
        validate_reply_relationship(parsed, "worker", {"id": 12, "role": "worker"}, None)

    assert exc_info.value.code == E_BRIDGE_REPLY_ROLE_INVALID


def test_decision_response_requires_decision_request_target():
    parsed = parse_typed_envelope(
        envelope(
            "DECISION_RESPONSE",
            {"decision": "A", "rationale": "It fits."},
            correlation_id="decision-1",
            reply_to_id=12,
        )
    )

    target = parse_typed_envelope(
        envelope("STATUS_UPDATE", {"state": "WAITING", "summary": "Waiting."}, correlation_id="decision-1")
    )
    with pytest.raises(BridgeProtocolError) as exc_info:
        validate_reply_relationship(parsed, "worker", {"id": 12, "role": "engineer"}, target)

    assert exc_info.value.code == E_BRIDGE_REPLY_KIND_INVALID


def test_reply_correlation_mismatch_is_rejected():
    parsed = parse_typed_envelope(
        envelope(
            "STATUS_UPDATE",
            {"state": "WAITING", "summary": "Waiting."},
            correlation_id="exchange-2",
            reply_to_id=12,
        )
    )
    target = parse_typed_envelope(
        envelope("STATUS_UPDATE", {"state": "IN_PROGRESS", "summary": "Running."}, correlation_id="exchange-1")
    )

    with pytest.raises(BridgeProtocolError) as exc_info:
        validate_reply_relationship(parsed, "worker", {"id": 12, "role": "engineer"}, target)

    assert exc_info.value.code == E_BRIDGE_CORRELATION_MISMATCH
