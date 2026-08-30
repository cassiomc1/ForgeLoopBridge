from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
README = (ROOT / "README.md").read_text(encoding="utf-8")
AUTONOMY = (ROOT / "examples" / "AUTONOMY.md").read_text(encoding="utf-8")


def test_readme_requires_protocol_handshake():
    assert "protocol-info --json" in README


def test_readme_mentions_structured_integration_preference():
    assert "structured integration" in README.lower()


def test_readme_requires_terminal_next_check():
    assert "nextAction" in README
    assert "terminal" in README


def test_autonomy_denies_self_issued_host_authority():
    text = AUTONOMY.lower()
    assert "host_attested" in text or "host-attested" in text
    assert "blocked" in text


def test_readme_does_not_forbid_all_engineer_forgeloop_reads():
    assert "You never execute code or run ForgeLoop yourself." not in README


def test_readme_feature_detects_verification_execution_isolation():
    assert "verificationExecutionIsolation" in README
    assert "observabilityStability" in README


def test_readme_feature_detects_all_forgeloop_164_capabilities():
    for capability in (
        "workspaceBinding",
        "canonicalHandoffs",
        "responsibilityConstraints",
        "differentialVerificationScope",
        "codeAttestation",
    ):
        assert capability in README


def test_readme_documents_optional_extension_trust_boundaries():
    text = f"{README}\n{AUTONOMY}".lower()
    for required in (
        "not delegation",
        "verification scope is not evidence",
        "verification scope is not revision coverage",
        "verified",
        "attested",
        "external signature",
        "workspace mismatch",
        "responsibility",
        "trusted scoped checker",
        "auto",
        "full",
        "changed",
        "claimed",
        "revision provider",
    ):
        assert required in text


def test_readme_rejects_new_bridge_authority_claims():
    text = f"{README}\n{AUTONOMY}".lower()
    for forbidden in (
        "bridge validates workspace binding",
        "bridge validates handoff",
        "bridge calculates verification scope",
        "bridge can attest code",
        "bridge approval creates attested status",
        "engineer approval overrides responsibility scope",
        "package 1.6.4 implies feature support",
    ):
        assert forbidden not in text


def test_readme_documents_verification_isolation_fail_closed():
    text = README.lower()
    assert "e_verification_isolation_unavailable" in text
    assert "e_verification_execution_invalid" in text
    assert "do not downgrade" in text
    assert "liveprojectwritable=false" in text


def test_readme_does_not_claim_bridge_provides_isolation():
    text = README.lower()
    forbidden = (
        "forgeloopbridge provides system_isolated",
        "bridge guarantees liveprojectwritable=false",
        "bridge attests verification isolation",
    )
    for phrase in forbidden:
        assert phrase not in text


def test_engineer_prompt_documents_read_only_isolation_review():
    engineer_section = README.split("### Engineer system prompt", 1)[1].split(
        "### Worker system prompt", 1
    )[0]
    text = engineer_section.lower()
    assert "verificationexecutionisolation" in text
    assert "protocolprojectroot" in text
    assert "execution cwd" in text
    assert "do not re-run `run-check`" in text


def test_worker_prompt_fails_closed_on_isolation_errors():
    worker_section = README.split("### Worker system prompt", 1)[1].split(
        "--- AUTONOMY CONTRACT", 1
    )[0]
    text = worker_section.lower()
    assert "verificationexecutionisolation" in text
    assert "e_verification_isolation_unavailable" in text
    assert "e_verification_execution_invalid" in text
    assert "do not downgrade" in text
    assert "synthetic evidence" in text


def test_readme_documents_recovery_invariants():
    assert "RESUME_RECOVERED_TASK" in README
    assert "RESOLVE_RECOVERY_INCONSISTENCY" in README
    assert "task-resume" in README


def test_readme_documents_capability_first_compatibility():
    assert "Protocol v1" in README
    assert "Integration API v1" in README
    assert "protocol-info --json" in README
    assert "protocolVersion" in README
    assert "durableActions" in README
    assert "features.durableActions" in README
    assert "package version alone" in README


def test_current_docs_cover_control_boundaries_and_diagnostics():
    text = f"{README}\n{AUTONOMY}".lower()
    for required in (
        "feature-detect",
        "commit_unknown",
        "do not retry",
        "require_approval",
        "host_attested",
        "policy",
        "action-reconcile",
        "action-verify",
        "structured",
        "trace",
        "reflect",
        "terminal",
        "nextaction",
    ):
        assert required in text


def test_current_docs_reject_authority_and_state_inference():
    text = f"{README}\n{AUTONOMY}".lower()
    for forbidden in (
        "engineer approved == forgeloop approval",
        "bridge approval grants host_attested",
        "retry commit_unknown",
        "committed means externally verified",
        "complete valid alone means done",
        "bridge determines action state",
        "bridge determines approval staleness",
    ):
        assert forbidden not in text


def test_readme_does_not_gate_capabilities_on_patch_version():
    text = README.lower()
    for forbidden in (
        "if forgeloop_version",
        "forgeloop version >=",
        "package 1.6.0 always means durableactions",
    ):
        assert forbidden not in text
    assert "package version alone" in text


def test_worker_prompt_keeps_control_paths_non_authoritative():
    worker_section = README.split("### Worker system prompt", 1)[1]
    control_section = worker_section.split("--- AUTONOMY CONTRACT", 1)[0].lower()
    assert "resolve_action_approval" in control_section
    assert "trusted execution-host capability" in control_section
    assert "reconcile_action" in control_section
    assert "comMIT_UNKNOWN".lower() in control_section
    assert "do not retry" in control_section
    assert "terminal" in control_section
    assert "nextaction" in control_section
    assert "pending required approval" in README.lower()


def test_historical_plan_is_marked_superseded():
    historical = (ROOT / "FORGELOOPBRIDGE_FORGELOOP_1_5_UPDATE_PLAN.md").read_text(
        encoding="utf-8"
    )
    assert "Historical plan" in historical
    assert "FORGELOOPBRIDGE_CURRENT_FORGELOOP_SYNC_UPDATE_PLAN.md" in historical


def test_current_sync_record_exists_and_states_the_boundary():
    current = ROOT / "FORGELOOPBRIDGE_CURRENT_FORGELOOP_SYNC_UPDATE_PLAN.md"
    assert current.exists()
    text = current.read_text(encoding="utf-8")
    assert "Protocol v1" in text
    assert "Integration API v1" in text
    assert "ForgeLoop remains the sole authority" in text
    assert "after_id" in text


def test_current_sync_record_documents_verification_isolation():
    current = ROOT / "FORGELOOPBRIDGE_CURRENT_FORGELOOP_SYNC_UPDATE_PLAN.md"
    text = current.read_text(encoding="utf-8")
    assert "verificationExecutionIsolation" in text
    assert "E_VERIFICATION_ISOLATION_UNAVAILABLE" in text
    assert "E_VERIFICATION_EXECUTION_INVALID" in text
    assert "observabilityStability" in text
    assert "protocolProjectRoot" in text
    assert "execution cwd" in text


def test_current_sync_record_is_current_for_forgeloop_164():
    current = (ROOT / "FORGELOOPBRIDGE_CURRENT_FORGELOOP_SYNC_UPDATE_PLAN.md").read_text(
        encoding="utf-8"
    )
    for capability in (
        "workspaceBinding",
        "canonicalHandoffs",
        "responsibilityConstraints",
        "differentialVerificationScope",
        "codeAttestation",
    ):
        assert capability in current
    assert "Observed synchronization baseline: ForgeLoop package 1.6.4" in current
    assert "package version" in current.lower()
    assert "does not implement, validate, infer, or attest" in current
    assert "trusted scoped checker" in current.lower()
    assert "VERIFIED" in current
    assert "ATTESTED" in current


WORKER_POLL = (ROOT / "examples" / "worker_poll.py").read_text(encoding="utf-8")


def test_engineer_verification_does_not_instruct_complete_mutation():
    engineer_section = README.split("### Engineer system prompt", 1)[1].split(
        "### Worker system prompt", 1
    )[0]
    assert "Does canonical `forgeloop complete` return VALID?" not in engineer_section
    assert "do not re-run `complete`" in engineer_section.lower()


def test_worker_prompt_respects_forgeloop_git_publication_policy():
    assert "Open a Pull Request including code changes and `.forgeloop/` artifacts." not in README
    assert "work-state.json" in README
    assert "executions/" in README
    assert "force-add" in README


def test_worker_poller_docs_use_task_scoped_complete():
    assert "forgeloop complete --json" not in WORKER_POLL
    assert "forgeloop complete --task <task-id> --json" in WORKER_POLL


def test_worker_poller_feature_detects_verification_isolation():
    assert "verificationExecutionIsolation" in WORKER_POLL


def test_worker_poller_knows_isolation_failure_codes():
    assert "E_VERIFICATION_ISOLATION_UNAVAILABLE" in WORKER_POLL
    assert "E_VERIFICATION_EXECUTION_INVALID" in WORKER_POLL


def test_autonomy_does_not_allow_self_attested_isolation():
    text = AUTONOMY.lower()
    assert "project_isolated" in text
    assert "system_isolated" in text
    assert "self-attest" in text or "self-attested" in text
    assert "liveprojectwritable=false" in text


def test_readme_documents_safe_evidence_publication():
    text = README.lower()
    assert "absolute local paths" in text
    assert "raw credentials" in text
    assert "complete private `.forgeloop/` state" in text


def test_env_example_exists_and_covers_required_tokens():
    env_example_path = ROOT / ".env.example"
    assert env_example_path.exists()
    content = env_example_path.read_text(encoding="utf-8")
    assert "ENGINEER_TOKEN=" in content
    assert "WORKER_TOKEN=" in content
    assert "PORT=" in content
    assert "SSE_QUEUE_SIZE=" in content
    assert "MAX_TYPED_ENVELOPE_BYTES=" in content


def test_readme_documents_architecture_correct_banner_and_reported_metadata():
    assert 'src="assets/banner.webp"' in README
    assert "action_id" in README
    assert "approval_id" in README
    assert "next_action" in README
    assert "reason_code" in README
    assert "reported copies" in README
    assert "/healthz" in README


def test_pyproject_version_matches_app_version():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'version = "2.1.1"' in pyproject
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "## 2.1.1 - 2026-08-30" in changelog


def test_current_sync_record_documents_stream_close_before_rest_recovery():
    current = (ROOT / "FORGELOOPBRIDGE_CURRENT_FORGELOOP_SYNC_UPDATE_PLAN.md").read_text(
        encoding="utf-8"
    )
    assert "explicitly closes the affected stream" in current
    assert "fresh SSE ticket" in current
    assert "bridge_api_version: 2.1.1" in current


def test_readme_documents_realtime_topology_and_worker_start_policy():
    text = README.lower()
    assert "one application worker" in text
    assert "shared broadcast backend" in text
    assert "authorization: bearer" in text
    assert "legacy" in text
    assert ".worker_last_seen" in text
    assert "--start-mode now" in text
    assert "sse_ticket_rate_limit" in text


def test_readme_documents_typed_bridge_protocol_contract():
    text = README.lower()
    assert "bridge typed message schema v1" in text
    assert "typed_message_versions" in text
    assert "correlation_id" in text
    assert "reply_to_id" in text
    assert "e_bridge_idempotency_conflict" in text
    assert "decision_notice" in text
    assert "requested_scope_mode" in text
    assert "resolved_scope_mode" in text
    assert "typed_integrity" in text
    assert "e_bridge_persisted_typed_invalid" in text
    assert "e_bridge_typed_payload_too_large" in text
    assert "permanent 4xx" in text
    assert "atomic replacement" in text
    assert "transport delivery" in text
    assert "durable-action idempotency" in text
