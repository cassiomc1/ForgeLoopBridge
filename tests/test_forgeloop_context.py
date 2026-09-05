from bridge_protocol.forgeloop_context import (
    BOUNDARY_SUPPORTED,
    BOUNDARY_UNDECLARED,
    BOUNDARY_UNSUPPORTED,
    SUPPORTED_FORGELOOP_CONTEXT_FEATURE_VERSIONS,
    SUPPORTED_FORGELOOP_CONTEXT_SCHEMA_VERSIONS,
    SUPPORTED_FORGELOOP_INTEGRATION_API_VERSIONS,
    SUPPORTED_FORGELOOP_PROTOCOL_VERSIONS,
    balanced_compatibility_context,
    consume_task_context,
    forgeloop_boundary_status,
    has_adaptive_context_capability,
)


def capabilities() -> dict:
    """Mirror the version-bearing shape of a real `protocol-info --json` payload."""
    return {
        "packageVersion": "1.10.1",
        "protocolVersion": 1,
        "readsProtocol": [1],
        "writesProtocol": [1],
        "compatibility": {"protocolVersion": 1, "schemaVersion": 1},
        "features": {
            "integrationApi": {"version": 1},
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
    assert "returned no projection" in consumed["reason"]


def test_invalid_canonical_context_fails_closed():
    invalid = canonical_context()
    invalid["invariants"]["lifecyclePhaseSkippingAllowed"] = True
    consumed = consume_task_context(capabilities(), invalid)
    assert consumed["status"] == "UNAVAILABLE"
    assert "phase skipping" in consumed["reason"]


def test_supported_forgeloop_version_sets_are_declared_in_code():
    """The supported boundary must be an explicit code constant, not prose."""
    for supported in (
        SUPPORTED_FORGELOOP_PROTOCOL_VERSIONS,
        SUPPORTED_FORGELOOP_INTEGRATION_API_VERSIONS,
        SUPPORTED_FORGELOOP_CONTEXT_SCHEMA_VERSIONS,
        SUPPORTED_FORGELOOP_CONTEXT_FEATURE_VERSIONS,
    ):
        assert isinstance(supported, tuple)
        assert supported == (1,)


def test_real_published_boundary_is_supported():
    assert forgeloop_boundary_status(capabilities()) == (BOUNDARY_SUPPORTED, None)


def test_package_version_alone_is_never_a_compatibility_decision():
    """A wildly different package version must not change the verdict either way."""
    future_package = capabilities()
    future_package["packageVersion"] = "99.0.0"
    assert forgeloop_boundary_status(future_package)[0] == BOUNDARY_SUPPORTED
    consumed = consume_task_context(
        future_package, canonical_context(), expected_task_id="task-context-1"
    )
    assert consumed["status"] == "CANONICAL"


def test_unsupported_protocol_version_fails_closed():
    future = capabilities()
    future["protocolVersion"] = 2
    future["readsProtocol"] = [2]
    future["writesProtocol"] = [2]
    future["compatibility"]["protocolVersion"] = 2

    status, reason = forgeloop_boundary_status(future)
    assert status == BOUNDARY_UNSUPPORTED
    assert "protocolVersion 2" in reason

    consumed = consume_task_context(future, canonical_context(), expected_task_id="task-context-1")
    assert consumed["status"] == "UNAVAILABLE"
    assert consumed["fallback"] == "NONE"
    assert "unsupported compatibility boundary" in consumed["reason"]


def test_unsupported_integration_api_version_fails_closed():
    future = capabilities()
    future["features"]["integrationApi"]["version"] = 2
    status, reason = forgeloop_boundary_status(future)
    assert status == BOUNDARY_UNSUPPORTED
    assert "features.integrationApi.version 2" in reason
    assert consume_task_context(future, canonical_context())["status"] == "UNAVAILABLE"


def test_unsupported_consumed_feature_version_fails_closed():
    for feature in ("adaptiveExecutionProfiles", "executionProfileContext"):
        future = capabilities()
        future["features"][feature]["version"] = 2
        status, reason = forgeloop_boundary_status(future)
        assert status == BOUNDARY_UNSUPPORTED
        assert f"features.{feature}.version 2" in reason
        assert consume_task_context(future, canonical_context())["status"] == "UNAVAILABLE"


def test_unsupported_schema_version_fails_closed():
    future = capabilities()
    future["compatibility"]["schemaVersion"] = 2
    status, reason = forgeloop_boundary_status(future)
    assert status == BOUNDARY_UNSUPPORTED
    assert "compatibility.schemaVersion 2" in reason
    assert consume_task_context(future, canonical_context())["status"] == "UNAVAILABLE"


def test_non_integer_declared_version_fails_closed():
    malformed = capabilities()
    malformed["protocolVersion"] = "1"
    status, reason = forgeloop_boundary_status(malformed)
    assert status == BOUNDARY_UNSUPPORTED
    assert "not an integer version" in reason


def test_capability_advertised_without_a_declared_protocol_version_fails_closed():
    """An Integration API or feature version does not stand in for the protocol version."""
    undeclared = capabilities()
    for key in ("protocolVersion", "readsProtocol", "writesProtocol", "compatibility"):
        undeclared.pop(key)

    assert forgeloop_boundary_status(undeclared) == (BOUNDARY_UNDECLARED, None)
    assert has_adaptive_context_capability(undeclared) is True

    consumed = consume_task_context(undeclared, canonical_context())
    assert consumed["status"] == "UNAVAILABLE"
    assert consumed["fallback"] == "NONE"
    assert "without declaring a supported protocol version" in consumed["reason"]


def test_reads_or_writes_protocol_without_a_supported_version_fails_closed():
    for key in ("readsProtocol", "writesProtocol"):
        future = capabilities()
        future[key] = [2, 3]
        status, reason = forgeloop_boundary_status(future)
        assert status == BOUNDARY_UNSUPPORTED
        assert f"{key} declares no supported protocol version" == reason
        assert consume_task_context(future, canonical_context())["status"] == "UNAVAILABLE"


def test_projection_declaring_an_unsupported_version_fails_closed():
    for key in ("schemaVersion", "protocolVersion"):
        future = canonical_context()
        future[key] = 2
        consumed = consume_task_context(capabilities(), future)
        assert consumed["status"] == "UNAVAILABLE"
        assert f"canonical task/context {key} 2" in consumed["reason"]


def test_projection_without_a_declared_version_fails_closed():
    for key in ("schemaVersion", "protocolVersion"):
        incomplete = canonical_context()
        incomplete.pop(key)
        consumed = consume_task_context(capabilities(), incomplete)
        assert consumed["status"] == "UNAVAILABLE"
        assert f"missing an integer {key}" in consumed["reason"]


def test_unsupported_boundary_is_not_downgraded_to_the_balanced_fallback():
    """An unsupported host must fail closed, never proceed under a local profile."""
    future = capabilities()
    future["protocolVersion"] = 2
    consumed = consume_task_context(future, canonical_context())
    assert consumed["status"] != "COMPATIBILITY_FALLBACK"
    assert consumed["source"] == "FORGELOOP_CANONICAL"
    assert consumed["fallback"] == "NONE"
