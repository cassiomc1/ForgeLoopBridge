"""Bridge protocol for Master-Agent order and execution exchange."""

from .errors import BridgeProtocolError
from .models import (
    ALL_MESSAGE_TYPES,
    EXECUTION_TYPES,
    ORDER_TYPES,
    SUPPORTED_TYPED_SCHEMA_VERSIONS,
    TYPED_MESSAGE_KINDS,
    CanonicalRef,
    DecisionNoticePayload,
    DecisionOption,
    DecisionRequestPayload,
    DecisionResponsePayload,
    ExecutionStatus,
    Role,
    StatusProgress,
    StatusUpdatePayload,
    TaskRequestPayload,
    TypedEnvelopeV1,
    TypedMessageKind,
    TypedPayload,
)
from .validation import envelope_to_dict, parse_typed_envelope

__all__ = [
    "ALL_MESSAGE_TYPES",
    "BridgeProtocolError",
    "CanonicalRef",
    "DecisionNoticePayload",
    "DecisionOption",
    "DecisionRequestPayload",
    "DecisionResponsePayload",
    "EXECUTION_TYPES",
    "ExecutionStatus",
    "ORDER_TYPES",
    "Role",
    "SUPPORTED_TYPED_SCHEMA_VERSIONS",
    "StatusProgress",
    "StatusUpdatePayload",
    "TYPED_MESSAGE_KINDS",
    "TaskRequestPayload",
    "TypedEnvelopeV1",
    "TypedMessageKind",
    "TypedPayload",
    "envelope_to_dict",
    "parse_typed_envelope",
]
