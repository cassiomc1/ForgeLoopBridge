"""Bridge-owned typed communication protocol."""

from .errors import BridgeProtocolError
from .models import (
    SUPPORTED_TYPED_SCHEMA_VERSIONS,
    TYPED_MESSAGE_KINDS,
    CanonicalRef,
    TypedEnvelopeV1,
    TypedMessageKind,
    TypedPayload,
)
from .validation import envelope_to_dict, parse_typed_envelope

__all__ = [
    "BridgeProtocolError",
    "CanonicalRef",
    "SUPPORTED_TYPED_SCHEMA_VERSIONS",
    "TYPED_MESSAGE_KINDS",
    "TypedEnvelopeV1",
    "TypedMessageKind",
    "TypedPayload",
    "envelope_to_dict",
    "parse_typed_envelope",
]
