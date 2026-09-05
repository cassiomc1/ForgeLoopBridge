"""Bridge-owned typed communication protocol."""

from .errors import BridgeProtocolError
from .forgeloop_context import (
    SUPPORTED_FORGELOOP_CONTEXT_FEATURE_VERSIONS,
    SUPPORTED_FORGELOOP_CONTEXT_SCHEMA_VERSIONS,
    SUPPORTED_FORGELOOP_INTEGRATION_API_VERSIONS,
    SUPPORTED_FORGELOOP_PROTOCOL_VERSIONS,
    forgeloop_boundary_status,
)
from .models import (
    SUPPORTED_TYPED_SCHEMA_VERSIONS,
    TYPED_MESSAGE_KINDS,
    CanonicalRef,
    ContextPolicyProjection,
    ContextUsageItems,
    ContextUsageReport,
    ExecutionProfileProjection,
    TypedEnvelopeV1,
    TypedMessageKind,
    TypedPayload,
)
from .validation import envelope_to_dict, parse_typed_envelope

__all__ = [
    "BridgeProtocolError",
    "CanonicalRef",
    "ContextPolicyProjection",
    "ContextUsageItems",
    "ContextUsageReport",
    "ExecutionProfileProjection",
    "SUPPORTED_FORGELOOP_CONTEXT_FEATURE_VERSIONS",
    "SUPPORTED_FORGELOOP_CONTEXT_SCHEMA_VERSIONS",
    "SUPPORTED_FORGELOOP_INTEGRATION_API_VERSIONS",
    "SUPPORTED_FORGELOOP_PROTOCOL_VERSIONS",
    "SUPPORTED_TYPED_SCHEMA_VERSIONS",
    "TYPED_MESSAGE_KINDS",
    "TypedEnvelopeV1",
    "TypedMessageKind",
    "TypedPayload",
    "envelope_to_dict",
    "forgeloop_boundary_status",
    "parse_typed_envelope",
]
