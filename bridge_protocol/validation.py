"""Parsing and relationship validation for Master-Agent Bridge messages."""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from .errors import (
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
from .models import TypedEnvelopeV1

KIND_MAP = {
    "TASK": "TASK_REQUEST",
    "ORDER": "ORDER",
    "STATUS": "STATUS_UPDATE",
    "EXECUTION": "EXECUTION",
    "DECISION_NEEDED": "DECISION_REQUEST",
    "DECISION_RESOLVED": "DECISION_RESPONSE",
    "DECISION_TAKEN": "DECISION_NOTICE",
    "BLOCKED": "BLOCKER",
    "REVIEW": "REVIEW_RESULT",
}

MASTER_ROLES = frozenset({"master", "engineer"})
AGENT_ROLES = frozenset({"agent", "worker"})


def _role_category(role: str) -> str:
    r = role.lower()
    if r in MASTER_ROLES:
        return "master"
    if r in AGENT_ROLES:
        return "agent"
    return r


def parse_typed_envelope(raw: Any) -> TypedEnvelopeV1:
    """Parse a client envelope and expose stable Bridge error codes."""
    if not isinstance(raw, dict):
        raise BridgeProtocolError(
            E_BRIDGE_TYPED_PAYLOAD_INVALID,
            "typed must be an object containing a schema_version, kind, and payload",
        )

    schema_version = raw.get("schema_version")
    if isinstance(schema_version, bool) or not isinstance(schema_version, int):
        raise BridgeProtocolError(
            E_BRIDGE_TYPED_PAYLOAD_INVALID,
            "typed.schema_version must be an integer",
        )
    if schema_version != 1:
        raise BridgeProtocolError(
            E_BRIDGE_TYPED_SCHEMA_UNSUPPORTED,
            f"typed schema version {schema_version} is not supported",
        )

    payload = raw.get("payload")
    if isinstance(payload, dict) and "kind" in raw and "kind" in payload and raw["kind"] != payload["kind"]:
        raise BridgeProtocolError(
            E_BRIDGE_TYPED_KIND_MISMATCH,
            "typed envelope kind must match the payload kind",
        )

    normalized = dict(raw)
    if isinstance(payload, dict) and "kind" not in payload and "kind" in raw:
        normalized["payload"] = {"kind": raw["kind"], **payload}

    try:
        return TypedEnvelopeV1.model_validate(normalized)
    except ValidationError as exc:
        errors = exc.errors()
        locations = {tuple(error.get("loc", ())) for error in errors}
        message = str(exc)
        if any(location and location[0] == "canonical_refs" for location in locations):
            raise BridgeProtocolError(
                E_BRIDGE_CANONICAL_REF_INVALID,
                "typed canonical references are invalid",
            ) from exc
        if "envelope.kind must match payload.kind" in message:
            raise BridgeProtocolError(
                E_BRIDGE_TYPED_KIND_MISMATCH,
                "typed envelope kind must match the payload kind",
            ) from exc
        if "correlation_id is required" in message:
            raise BridgeProtocolError(
                E_BRIDGE_CORRELATION_MISMATCH,
                "decision messages require correlation_id",
            ) from exc
        raise BridgeProtocolError(
            E_BRIDGE_TYPED_PAYLOAD_INVALID,
            "typed message does not match the supported schema",
        ) from exc


def envelope_to_dict(envelope: TypedEnvelopeV1) -> dict[str, Any]:
    """Return the normalized JSON-compatible envelope used by REST/SSE/DB."""
    return envelope.model_dump(mode="json", exclude_none=False)


def validate_legacy_kind_consistency(message_type: str | None, envelope: TypedEnvelopeV1) -> None:
    """Reject only explicit, unambiguous legacy/typed kind disagreements."""
    if message_type is None:
        return
    expected_kind = KIND_MAP.get(message_type.upper())
    if expected_kind is not None and expected_kind != envelope.kind:
        raise BridgeProtocolError(
            E_BRIDGE_TYPED_KIND_MISMATCH,
            f"message_type {message_type} is not consistent with typed kind {envelope.kind}",
        )


def validate_reply_relationship(
    envelope: TypedEnvelopeV1,
    role: str,
    target: dict[str, Any] | None,
    target_typed: TypedEnvelopeV1 | None,
) -> None:
    """Validate transport-level reply linkage."""
    if envelope.reply_to_id is None:
        if envelope.kind == "DECISION_RESPONSE":
            raise BridgeProtocolError(
                E_BRIDGE_REPLY_NOT_FOUND,
                "DECISION_RESPONSE must reference a DECISION_REQUEST with reply_to_id",
            )
        return

    if target is None:
        raise BridgeProtocolError(
            E_BRIDGE_REPLY_NOT_FOUND,
            f"reply target {envelope.reply_to_id} was not found",
        )
    target_role = target.get("role") if hasattr(target, "get") else target["role"]
    if _role_category(target_role) == _role_category(role):
        raise BridgeProtocolError(
            E_BRIDGE_REPLY_ROLE_INVALID,
            "typed replies must target a message authored by the opposite role",
        )
    if envelope.kind == "DECISION_RESPONSE":
        if target_typed is None or target_typed.kind != "DECISION_REQUEST":
            raise BridgeProtocolError(
                E_BRIDGE_REPLY_KIND_INVALID,
                "DECISION_RESPONSE must reply to a DECISION_REQUEST",
            )
    if (
        envelope.correlation_id is not None
        and target_typed is not None
        and target_typed.correlation_id is not None
        and envelope.correlation_id != target_typed.correlation_id
    ):
        raise BridgeProtocolError(
            E_BRIDGE_CORRELATION_MISMATCH,
            "reply and target correlation_id values must match",
        )
