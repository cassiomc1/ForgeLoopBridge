"""Capability-gated consumption of ForgeLoop's canonical task/context resource.

ForgeLoopBridge only transports and bounds the canonical projection. It never
classifies work, lowers a safety floor, or treats a Bridge message as authority.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from .models import ContextPolicyProjection, ExecutionProfileProjection

MAX_CONTEXT_TEXT = 10_000
MAX_CONTEXT_LIST = 64

_REQUIRED_INVARIANTS = (
    "lifecyclePhasesPreserved",
    "requiredGatesPreserved",
    "evidenceRequirementsPreserved",
    "verificationTruthPreserved",
    "authorityChecksPreserved",
    "provenancePreserved",
    "completionValidationPreserved",
    "safetyFloorPreserved",
    "lifecyclePhaseSkippingAllowed",
)

_BALANCED_POLICY = {
    "context_depth": "relevant",
    "output": "standard",
    "plan_depth": "standard",
    "guide_strategy": "relevant",
    "verification_strategy": "normal",
    "optional_artifacts": "lazy",
    "required_sections": ["objective", "scope", "implementation", "verification", "relevant-history"],
    "excluded_context": ["unrelated-repository-context"],
    "allowed_optional_context": ["task-history", "relevant-artifacts"],
}


def has_adaptive_context_capability(capabilities: dict[str, Any] | None) -> bool:
    """Return true only when the advertised canonical context contract exists."""
    if not isinstance(capabilities, dict):
        return False
    features = capabilities.get("features")
    if not isinstance(features, dict):
        return False
    adaptive = features.get("adaptiveExecutionProfiles")
    context = features.get("executionProfileContext")
    if not isinstance(adaptive, dict) or adaptive.get("supported") is not True:
        return False
    if not isinstance(context, dict) or context.get("supported") is not True:
        return False
    resources = capabilities.get("resources")
    if resources is None:
        return True
    if not isinstance(resources, list):
        return False
    return any(
        isinstance(resource, dict) and resource.get("name") == "task/context"
        for resource in resources
    )


def _bounded_text(value: Any, label: str, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    if len(value) > MAX_CONTEXT_TEXT:
        raise ValueError(f"{label} exceeds the bounded context limit")
    return value.strip()


def _bounded_list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list) or len(value) > MAX_CONTEXT_LIST:
        raise ValueError(f"{label} must be a bounded list")
    return deepcopy(value)


def _canonical_policy(raw: Any) -> ContextPolicyProjection:
    if not isinstance(raw, dict):
        raise ValueError("canonical task/context is missing contextPolicy")
    mapped = {
        "context_depth": raw.get("contextDepth"),
        "output": raw.get("output"),
        "plan_depth": raw.get("planDepth"),
        "guide_strategy": raw.get("guideStrategy"),
        "verification_strategy": raw.get("verificationStrategy"),
        "optional_artifacts": raw.get("optionalArtifacts"),
        "required_sections": raw.get("requiredSections", []),
        "excluded_context": raw.get("excludedContext", []),
        "allowed_optional_context": raw.get("allowedOptionalContext", []),
    }
    return ContextPolicyProjection.model_validate(mapped)


def _canonical_context(task_context: dict[str, Any], expected_task_id: str | None) -> dict[str, Any]:
    task_id = _bounded_text(task_context.get("taskId"), "taskId")
    if expected_task_id is not None and task_id != expected_task_id:
        raise ValueError("canonical task/context taskId does not match the requested task")
    profile = ExecutionProfileProjection.model_validate(task_context.get("executionProfile"))
    policy = _canonical_policy(task_context.get("contextPolicy"))
    phase = _bounded_text(task_context.get("phase"), "phase")
    next_action = _bounded_text(task_context.get("nextAction"), "nextAction", nullable=True)
    objective = _bounded_text(task_context.get("objective"), "objective", nullable=True)
    deliverables = _bounded_list(task_context.get("deliverables", []), "deliverables")
    constraints = _bounded_list(task_context.get("constraints", []), "constraints")
    selected_guides = _bounded_list(task_context.get("selectedGuideIds", []), "selectedGuideIds")
    requirements = _bounded_list(
        task_context.get("verificationRequirements", []),
        "verificationRequirements",
    )
    optional_context = task_context.get("optionalContext")
    if not isinstance(optional_context, dict):
        raise ValueError("canonical task/context is missing optionalContext")
    available = _bounded_list(optional_context.get("available", []), "optionalContext.available")
    loaded = _bounded_list(optional_context.get("loaded", []), "optionalContext.loaded")
    invariants = task_context.get("invariants")
    if not isinstance(invariants, dict):
        raise ValueError("canonical task/context is missing invariants")
    if any(key not in invariants or not isinstance(invariants[key], bool) for key in _REQUIRED_INVARIANTS):
        raise ValueError("canonical task/context invariants are incomplete")
    if invariants["lifecyclePhaseSkippingAllowed"] is not False:
        raise ValueError("canonical task/context permits lifecycle phase skipping")

    return {
        "status": "CANONICAL",
        "source": "FORGELOOP_CANONICAL",
        "task_id": task_id,
        "execution_profile": profile.model_dump(mode="json"),
        "context_policy": policy.model_dump(mode="json"),
        "phase": phase,
        "next_action": next_action,
        "context": {
            "objective": objective,
            "deliverables": deliverables,
            "constraints": constraints,
            "selected_guide_ids": selected_guides,
            "verification_requirements": requirements,
        },
        "optional_context": {"available": available, "loaded": loaded},
        "invariants": deepcopy(invariants),
    }


def balanced_compatibility_context(reason: str) -> dict[str, Any]:
    """Return the explicit balanced fallback for older ForgeLoop installations."""
    return {
        "status": "COMPATIBILITY_FALLBACK",
        "source": "BALANCED_COMPATIBILITY",
        "reason": reason,
        "task_id": None,
        "execution_profile": {
            "requested": "balanced",
            "floor": "balanced",
            "resolved": "balanced",
            "reasons": ["LEGACY_ROUTE_COMPATIBILITY"],
            "escalated": False,
        },
        "context_policy": deepcopy(_BALANCED_POLICY),
        "phase": None,
        "next_action": None,
        "context": {
            "objective": None,
            "deliverables": [],
            "constraints": [],
            "selected_guide_ids": [],
            "verification_requirements": [],
        },
        "optional_context": {"available": [], "loaded": []},
        "invariants": {
            "lifecyclePhasesPreserved": True,
            "requiredGatesPreserved": True,
            "evidenceRequirementsPreserved": True,
            "verificationTruthPreserved": True,
            "authorityChecksPreserved": True,
            "provenancePreserved": True,
            "completionValidationPreserved": True,
            "safetyFloorPreserved": True,
            "lifecyclePhaseSkippingAllowed": False,
        },
    }


def unavailable_context(reason: str) -> dict[str, Any]:
    return {
        "status": "UNAVAILABLE",
        "source": "FORGELOOP_CANONICAL",
        "reason": reason,
        "fallback": "NONE",
    }


def consume_task_context(
    capabilities: dict[str, Any] | None,
    task_context: dict[str, Any] | None,
    *,
    expected_task_id: str | None = None,
) -> dict[str, Any]:
    """Consume a canonical projection without deriving a profile locally."""
    if not has_adaptive_context_capability(capabilities):
        return balanced_compatibility_context(
            "ForgeLoop did not advertise adaptive execution profiles and task/context."
        )
    if task_context is None:
        return unavailable_context("The advertised task/context resource returned no projection.")
    if not isinstance(task_context, dict):
        return unavailable_context("The advertised task/context resource returned a non-object.")
    try:
        return _canonical_context(task_context, expected_task_id)
    except (TypeError, ValueError) as error:
        return unavailable_context(f"The canonical task/context projection is invalid: {error}")
