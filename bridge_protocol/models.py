"""Pydantic models for Master-Agent Bridge protocol.

Minimal, stable, and reliable models for exchanging orders (Master)
and execution reports (Agent) with idempotency and reply linkage.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

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
    def reject_control_characters(cls, value: Any) -> Any:
        if isinstance(value, str) and any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("typed string values must contain printable characters only")
        return value


Role = Literal["master", "agent"]
ExecutionStatus = Literal["PENDING", "RUNNING", "COMPLETED", "FAILED", "BLOCKED"]

TypedMessageKind = Literal[
    "TASK_REQUEST",
    "ORDER",
    "STATUS_UPDATE",
    "EXECUTION",
    "DECISION_REQUEST",
    "DECISION_RESPONSE",
    "DECISION_NOTICE",
    "BLOCKER",
    "REVIEW_RESULT",
]

TYPED_MESSAGE_KINDS = frozenset(
    {
        "TASK_REQUEST",
        "ORDER",
        "STATUS_UPDATE",
        "EXECUTION",
        "DECISION_REQUEST",
        "DECISION_RESPONSE",
        "DECISION_NOTICE",
        "BLOCKER",
        "REVIEW_RESULT",
    }
)
ORDER_TYPES = frozenset({"ORDER", "TASK", "INSTRUCTION", "DECISION", "CANCEL"})
EXECUTION_TYPES = frozenset({"EXECUTION", "STATUS", "PROGRESS", "RESULT", "BLOCKER"})
ALL_MESSAGE_TYPES = ORDER_TYPES | EXECUTION_TYPES | frozenset({"MESSAGE", "GENERAL"})
SUPPORTED_TYPED_SCHEMA_VERSIONS = (1,)


class CanonicalRef(BridgeModel):
    """An opaque reference for coordination (e.g. commit hash, task ID, external doc)."""

    kind: str = Field(min_length=1, max_length=64)
    ref: str = Field(min_length=1, max_length=500)


class TaskRequestPayload(BridgeModel):
    kind: Literal["TASK_REQUEST", "ORDER"] = "TASK_REQUEST"
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


class StatusUpdatePayload(BridgeModel):
    kind: Literal["STATUS_UPDATE", "EXECUTION"] = "STATUS_UPDATE"
    state: Literal[
        "RECEIVED",
        "IN_PROGRESS",
        "WAITING",
        "BLOCKED",
        "PARTIALLY_VERIFIED",
        "COMPLETE_REPORTED",
        "COMPLETED",
        "FAILED",
    ]
    summary: str = Field(min_length=1, max_length=10_000)
    progress: StatusProgress | None = None


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
    retryable: bool | None = None


class ReviewItem(BridgeModel):
    code: str = Field(min_length=1, max_length=100)
    description: str = Field(min_length=1, max_length=2_000)


class ReviewResultPayload(BridgeModel):
    kind: Literal["REVIEW_RESULT"] = "REVIEW_RESULT"
    result: Literal["APPROVED_PROJECT_DECISION", "CHANGES_REQUESTED", "REJECTED"]
    summary: str = Field(min_length=1, max_length=10_000)
    items: list[ReviewItem] = Field(default_factory=list, max_length=64)


TypedPayload = Annotated[
    TaskRequestPayload
    | StatusUpdatePayload
    | DecisionRequestPayload
    | DecisionResponsePayload
    | DecisionNoticePayload
    | BlockerPayload
    | ReviewResultPayload,
    Field(discriminator="kind"),
]


class TypedEnvelopeV1(BridgeModel):
    schema_version: Literal[1] = 1
    kind: TypedMessageKind
    message_key: str = Field(min_length=4, max_length=200)
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
