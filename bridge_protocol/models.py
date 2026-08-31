"""Pydantic models for ForgeLoopBridge Typed Message Schema v1.

These models describe coordination intent and copied references only. They do
not model or authorize ForgeLoop lifecycle, policy, verification, or
attestation state.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def _validate_printable_string_list(values: list[str]) -> list[str]:
    if any(any(ord(char) < 32 or ord(char) == 127 for char in value) for value in values):
        raise ValueError("typed string values must contain printable characters only")
    return values


class BridgeModel(BaseModel):
    """Strict base model shared by all typed protocol values."""

    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        str_strip_whitespace=True,
        validate_default=True,
    )

    @field_validator("*", mode="after")
    @classmethod
    def reject_control_characters(cls, value):
        if isinstance(value, str) and any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("typed string values must contain printable characters only")
        return value


TypedMessageKind = Literal[
    "TASK_REQUEST",
    "STATUS_UPDATE",
    "DECISION_REQUEST",
    "DECISION_RESPONSE",
    "DECISION_NOTICE",
    "BLOCKER",
    "REVIEW_RESULT",
    "CONTROL_NOTICE",
    "HANDOFF_NOTICE",
    "VERIFICATION_REPORT",
    "ATTESTATION_REPORT",
]

TYPED_MESSAGE_KINDS = frozenset(
    {
        "TASK_REQUEST",
        "STATUS_UPDATE",
        "DECISION_REQUEST",
        "DECISION_RESPONSE",
        "DECISION_NOTICE",
        "BLOCKER",
        "REVIEW_RESULT",
        "CONTROL_NOTICE",
        "HANDOFF_NOTICE",
        "VERIFICATION_REPORT",
        "ATTESTATION_REPORT",
    }
)
SUPPORTED_TYPED_SCHEMA_VERSIONS = (1,)


class CanonicalRef(BridgeModel):
    """An opaque reference copied from a canonical ForgeLoop result."""

    kind: str = Field(min_length=1, max_length=64)
    ref: str = Field(min_length=1, max_length=500)


class TaskRequestPayload(BridgeModel):
    kind: Literal["TASK_REQUEST"] = "TASK_REQUEST"
    goal: str = Field(min_length=1, max_length=10_000)
    acceptance_criteria: list[str] = Field(default_factory=list, max_length=64)
    preferred_work_type: str | None = Field(default=None, min_length=1, max_length=100)
    priority: Literal["LOW", "NORMAL", "HIGH", "URGENT"] = "NORMAL"

    @field_validator("acceptance_criteria")
    @classmethod
    def validate_acceptance_criteria(cls, value: list[str]) -> list[str]:
        _validate_printable_string_list(value)
        if any(not item for item in value):
            raise ValueError("acceptance criteria must not contain empty values")
        if any(len(item) > 2_000 for item in value):
            raise ValueError("acceptance criteria values must be at most 2000 characters")
        return value


class StatusProgress(BridgeModel):
    completed: int = Field(ge=0)
    total: int = Field(ge=0)

    @model_validator(mode="after")
    def completed_cannot_exceed_total(self):
        if self.completed > self.total:
            raise ValueError("completed progress cannot exceed total progress")
        return self


class ExecutionProfileProjection(BridgeModel):
    """Opaque canonical execution-profile values copied for host presentation."""

    requested: Literal["auto", "light", "balanced", "full"] | None = None
    floor: Literal["light", "balanced", "full"]
    resolved: Literal["light", "balanced", "full"]
    reasons: list[str] = Field(default_factory=list, max_length=32)
    escalated: bool

    @field_validator("reasons")
    @classmethod
    def validate_reasons(cls, value: list[str]) -> list[str]:
        return _validate_printable_string_list(value)


class ContextPolicyProjection(BridgeModel):
    """Bounded presentation policy copied from canonical task/context."""

    context_depth: str = Field(min_length=1, max_length=100)
    output: str = Field(min_length=1, max_length=100)
    plan_depth: str = Field(min_length=1, max_length=100)
    guide_strategy: str = Field(min_length=1, max_length=100)
    verification_strategy: str = Field(min_length=1, max_length=100)
    optional_artifacts: str = Field(min_length=1, max_length=100)
    required_sections: list[str] = Field(default_factory=list, max_length=32)
    excluded_context: list[str] = Field(default_factory=list, max_length=64)
    allowed_optional_context: list[str] = Field(default_factory=list, max_length=64)

    @field_validator("required_sections", "excluded_context", "allowed_optional_context")
    @classmethod
    def validate_policy_lists(cls, value: list[str]) -> list[str]:
        return _validate_printable_string_list(value)


class ContextUsageItems(BridgeModel):
    task_context: int | None = Field(default=None, ge=0)
    guides: int | None = Field(default=None, ge=0)
    history: int | None = Field(default=None, ge=0)
    protocol_instructions: int | None = Field(default=None, ge=0)
    repository_context: int | None = Field(default=None, ge=0)
    other: int | None = Field(default=None, ge=0)


class ContextUsageReport(BridgeModel):
    """Host-observed context usage; UNKNOWN is never treated as zero."""

    source: Literal["HOST_REPORTED", "UNKNOWN"]
    profile: Literal["light", "balanced", "full"] | None = None
    items: ContextUsageItems

    @model_validator(mode="after")
    def unknown_usage_is_null(self):
        if self.source == "UNKNOWN" and any(
            value is not None for value in self.items.model_dump().values()
        ):
            raise ValueError("UNKNOWN context usage must keep every item null")
        return self


class StatusUpdatePayload(BridgeModel):
    kind: Literal["STATUS_UPDATE"] = "STATUS_UPDATE"
    state: Literal[
        "RECEIVED",
        "IN_PROGRESS",
        "WAITING",
        "BLOCKED",
        "PARTIALLY_VERIFIED",
        "COMPLETE_REPORTED",
    ]
    summary: str = Field(min_length=1, max_length=10_000)
    progress: StatusProgress | None = None
    execution_profile: ExecutionProfileProjection | None = None
    context_policy: ContextPolicyProjection | None = None
    context_usage: ContextUsageReport | None = None


class DecisionOption(BridgeModel):
    id: str = Field(min_length=1, max_length=100)
    label: str = Field(min_length=1, max_length=500)
    rationale: str | None = Field(default=None, min_length=1, max_length=2_000)


class DecisionRequestPayload(BridgeModel):
    kind: Literal["DECISION_REQUEST"] = "DECISION_REQUEST"
    question: str = Field(min_length=1, max_length=10_000)
    options: list[DecisionOption] = Field(min_length=1, max_length=32)
    recommended_option: str | None = Field(default=None, min_length=1, max_length=100)
    decision_class: Literal["REVERSIBLE", "IRREVERSIBLE", "POLICY_SENSITIVE"] = "REVERSIBLE"

    @model_validator(mode="after")
    def validate_option_references(self):
        option_ids = [option.id for option in self.options]
        if len(option_ids) != len(set(option_ids)):
            raise ValueError("decision option IDs must be unique")
        if self.recommended_option is not None and self.recommended_option not in option_ids:
            raise ValueError("recommended_option must reference a declared option")
        return self


class DecisionResponsePayload(BridgeModel):
    kind: Literal["DECISION_RESPONSE"] = "DECISION_RESPONSE"
    decision: str = Field(min_length=1, max_length=200)
    rationale: str = Field(min_length=1, max_length=10_000)


class DecisionNoticePayload(BridgeModel):
    kind: Literal["DECISION_NOTICE"] = "DECISION_NOTICE"
    decision: str = Field(min_length=1, max_length=200)
    rationale: str = Field(min_length=1, max_length=10_000)
    decision_class: Literal["REVERSIBLE", "IRREVERSIBLE", "POLICY_SENSITIVE"] = "REVERSIBLE"


class BlockerPayload(BridgeModel):
    kind: Literal["BLOCKER"] = "BLOCKER"
    category: str = Field(min_length=1, max_length=100)
    summary: str = Field(min_length=1, max_length=10_000)
    canonical_reason_code: str | None = Field(default=None, min_length=1, max_length=160)
    canonical_next_action: str | None = Field(default=None, min_length=1, max_length=200)
    retryable: bool | None = None


class ReviewItem(BridgeModel):
    code: str = Field(min_length=1, max_length=100)
    description: str = Field(min_length=1, max_length=2_000)


class ReviewResultPayload(BridgeModel):
    kind: Literal["REVIEW_RESULT"] = "REVIEW_RESULT"
    result: Literal["APPROVED_PROJECT_DECISION", "CHANGES_REQUESTED", "REJECTED"]
    summary: str = Field(min_length=1, max_length=10_000)
    items: list[ReviewItem] = Field(default_factory=list, max_length=64)


class ControlNoticePayload(BridgeModel):
    kind: Literal["CONTROL_NOTICE"] = "CONTROL_NOTICE"
    canonical_next_action: str | None = Field(default=None, min_length=1, max_length=200)
    canonical_reason_codes: list[str] = Field(default_factory=list, max_length=64)
    authority_required: bool = False
    approval_required: bool = False
    host_action_required: bool = False
    reconciliation_authority_required: bool = False

    @field_validator("canonical_reason_codes")
    @classmethod
    def validate_reason_codes(cls, value: list[str]) -> list[str]:
        return _validate_printable_string_list(value)


class HandoffNoticePayload(BridgeModel):
    kind: Literal["HANDOFF_NOTICE"] = "HANDOFF_NOTICE"
    handoff_ref: str = Field(min_length=1, max_length=500)
    summary: str = Field(min_length=1, max_length=10_000)


class VerificationReportPayload(BridgeModel):
    kind: Literal["VERIFICATION_REPORT"] = "VERIFICATION_REPORT"
    canonical_result: str = Field(min_length=1, max_length=100)
    # Deprecated v1 compatibility field. New clients should use the precise
    # requested/resolved fields below.
    scope_mode: Literal["AUTO", "CHANGED", "CLAIMED", "FULL"] | None = None
    requested_scope_mode: Literal["AUTO", "CHANGED", "CLAIMED", "FULL"] | None = None
    resolved_scope_mode: Literal["CHANGED", "CLAIMED", "FULL", "UNRESOLVED"] | None = None
    scope_ref: str | None = Field(default=None, min_length=1, max_length=500)
    checker_id: str | None = Field(default=None, min_length=1, max_length=200)
    execution_ref: str | None = Field(default=None, min_length=1, max_length=500)
    summary: str = Field(min_length=1, max_length=10_000)

    @model_validator(mode="after")
    def validate_scope_representation(self):
        if self.scope_mode is None and self.requested_scope_mode is None and self.resolved_scope_mode is None:
            raise ValueError("verification report must include a scope representation")
        if (
            self.scope_mode is not None
            and self.requested_scope_mode is not None
            and self.scope_mode != self.requested_scope_mode
        ):
            raise ValueError("scope_mode must match requested_scope_mode when both are present")
        return self


class AttestationReportPayload(BridgeModel):
    kind: Literal["ATTESTATION_REPORT"] = "ATTESTATION_REPORT"
    canonical_status: str = Field(min_length=1, max_length=100)
    attestation_ref: str | None = Field(default=None, min_length=1, max_length=500)
    revision_ref: str | None = Field(default=None, min_length=1, max_length=500)
    range_status: str = Field(min_length=1, max_length=100)
    signature_status: str = Field(min_length=1, max_length=100)


TypedPayload = Annotated[
    TaskRequestPayload
    | StatusUpdatePayload
    | DecisionRequestPayload
    | DecisionResponsePayload
    | DecisionNoticePayload
    | BlockerPayload
    | ReviewResultPayload
    | ControlNoticePayload
    | HandoffNoticePayload
    | VerificationReportPayload
    | AttestationReportPayload,
    Field(discriminator="kind"),
]


class TypedEnvelopeV1(BridgeModel):
    schema_version: Literal[1] = 1
    kind: TypedMessageKind
    message_key: str = Field(min_length=8, max_length=200)
    correlation_id: str | None = Field(default=None, min_length=1, max_length=200)
    reply_to_id: int | None = Field(default=None, ge=1)
    expects_reply: bool = False
    payload: TypedPayload
    canonical_refs: list[CanonicalRef] = Field(default_factory=list, max_length=32)

    @model_validator(mode="before")
    @classmethod
    def fill_payload_discriminator(cls, value):
        if not isinstance(value, dict):
            return value
        payload = value.get("payload")
        if isinstance(payload, dict) and "kind" not in payload and "kind" in value:
            normalized = dict(value)
            normalized["payload"] = {"kind": value["kind"], **payload}
            if value["kind"] == "DECISION_REQUEST" and "expects_reply" not in normalized:
                normalized["expects_reply"] = True
            return normalized
        if value.get("kind") == "DECISION_REQUEST" and "expects_reply" not in value:
            normalized = dict(value)
            normalized["expects_reply"] = True
            return normalized
        return value

    @model_validator(mode="after")
    def validate_envelope_relationships(self):
        if self.kind != self.payload.kind:
            raise ValueError("envelope.kind must match payload.kind")
        if self.kind in {"DECISION_REQUEST", "DECISION_RESPONSE"} and self.correlation_id is None:
            raise ValueError("correlation_id is required for decision messages")
        if self.kind == "DECISION_REQUEST" and not self.expects_reply:
            raise ValueError("DECISION_REQUEST must expect a reply")
        if self.kind in {"DECISION_RESPONSE", "DECISION_NOTICE"} and self.expects_reply:
            raise ValueError(f"{self.kind} must not expect a reply")
        return self
