from bridge_protocol.forgeloop_context import (
    balanced_compatibility_context,
    consume_task_context,
    has_adaptive_context_capability,
)


def capabilities() -> dict:
    return {
        "features": {
            "adaptiveExecutionProfiles": {"version": 1, "supported": True},
            "executionProfileContext": {
                "version": 1,
                "supported": True,
                "resource": "task/context",
            },
        },
        "resources": [{"name": "task/context", "scope": "TASK"}],
    }


def canonical_context(resolved: str = "light", requested: str = "auto") -> dict:
    return {
        "schemaVersion": 1,
        "protocolVersion": 1,
        "taskId": "task-context-1",
        "executionProfile": {
            "requested": requested,
            "floor": "balanced" if resolved == "full" else resolved,
            "resolved": resolved,
            "reasons": ["WORK_COMPLETE_WEBSITE"],
            "escalated": resolved == "full",
        },
        "phase": "EXECUTING",
        "nextAction": "START_VERIFICATION",
        "objective": "Build the local page.",
        "deliverables": ["index.html"],
        "constraints": ["No external services."],
        "selectedGuideIds": ["clean", "test"],
        "verificationRequirements": [{"id": "html", "text": "HTML checks"}],
        "contextPolicy": {
            "contextDepth": "targeted",
            "output": "compact",
            "planDepth": "short",
            "guideStrategy": "targeted",
            "verificationStrategy": "focused",
            "optionalArtifacts": "lazy",
            "requiredSections": ["objective", "scope", "implementation", "verification"],
            "excludedContext": ["full-history"],
            "allowedOptionalContext": [],
        },
        "optionalContext": {"available": [], "loaded": []},
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


def test_capability_detection_requires_canonical_resource():
    assert has_adaptive_context_capability(capabilities()) is True
    missing_resource = capabilities()
    missing_resource["resources"] = []
    assert has_adaptive_context_capability(missing_resource) is False


def test_consumer_uses_resolved_profile_and_bounded_policy():
    consumed = consume_task_context(
        capabilities(), canonical_context(resolved="full", requested="light"), expected_task_id="task-context-1"
    )

    assert consumed["status"] == "CANONICAL"
    assert consumed["execution_profile"]["resolved"] == "full"
    assert consumed["execution_profile"]["floor"] == "balanced"
    assert consumed["context_policy"]["context_depth"] == "targeted"
    assert consumed["invariants"]["lifecyclePhaseSkippingAllowed"] is False


def test_older_forgeloop_falls_back_to_balanced_compatibility_only():
    consumed = consume_task_context({}, canonical_context())
    assert consumed == balanced_compatibility_context(
        "ForgeLoop did not advertise adaptive execution profiles and task/context."
    )
    assert consumed["execution_profile"]["resolved"] == "balanced"


def test_advertised_but_missing_context_does_not_invent_a_light_projection():
    consumed = consume_task_context(capabilities(), None, expected_task_id="task-context-1")
    assert consumed["status"] == "UNAVAILABLE"
    assert consumed["fallback"] == "NONE"


def test_invalid_canonical_context_fails_closed():
    invalid = canonical_context()
    invalid["invariants"]["lifecyclePhaseSkippingAllowed"] = True
    consumed = consume_task_context(capabilities(), invalid)
    assert consumed["status"] == "UNAVAILABLE"
    assert "phase skipping" in consumed["reason"]
