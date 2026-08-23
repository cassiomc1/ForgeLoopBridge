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


def test_readme_documents_version_dimensions():
    assert "1.5.x" in README
    assert "protocol `1`" in README or "protocol 1" in README
    assert "Integration API `1`" in README or "Integration API 1" in README


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


