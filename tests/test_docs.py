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
        "package 1.6.0 always means durableactions",
    ):
        assert forbidden not in text


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


def test_env_example_exists_and_covers_required_tokens():
    env_example_path = ROOT / ".env.example"
    assert env_example_path.exists()
    content = env_example_path.read_text(encoding="utf-8")
    assert "ENGINEER_TOKEN=" in content
    assert "WORKER_TOKEN=" in content
    assert "PORT=" in content
    assert "SSE_QUEUE_SIZE=" in content


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
    assert 'version = "2.0.0"' in pyproject


def test_current_sync_record_documents_stream_close_before_rest_recovery():
    current = (ROOT / "FORGELOOPBRIDGE_CURRENT_FORGELOOP_SYNC_UPDATE_PLAN.md").read_text(
        encoding="utf-8"
    )
    assert "explicitly closes the affected stream" in current
    assert "fresh SSE ticket" in current


def test_readme_documents_realtime_topology_and_worker_start_policy():
    text = README.lower()
    assert "one application worker" in text
    assert "shared broadcast backend" in text
    assert "authorization: bearer" in text
    assert "legacy" in text
    assert ".worker_last_seen" in text
    assert "--start-mode now" in text
    assert "sse_ticket_rate_limit" in text
