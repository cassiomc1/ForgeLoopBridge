from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def test_frontend_bootstrap_requests_latest_page():
    assert "params.set('latest', 'true')" in INDEX


def test_frontend_waits_for_authentication_before_protected_bootstrap():
    boot = INDEX[INDEX.index("// Boot:"):]
    assert "if (token && myRole)" in boot
    assert "bootAuthenticatedSession();" in boot
    assert "fetchMessages(true).then(() => startStream())" not in INDEX
    assert "location.reload()" not in INDEX
    assert "if (!token || !myRole)" in INDEX


def test_frontend_reconciles_after_sse_open():
    assert "await fetchMessages(false)" in INDEX


def test_frontend_uses_short_lived_sse_ticket_instead_of_bearer_url():
    assert "'/api/stream-ticket'" in INDEX
    assert "method: 'POST'" in INDEX
    assert "'/api/stream?ticket='" in INDEX
    assert "'/api/stream?token=" not in INDEX


def test_frontend_exposes_history_pagination():
    assert 'id="load-older"' in INDEX
    assert "before_id" in INDEX


def test_visible_brand_is_forgeloopbridge():
    assert "ForgeLoop<span>Bridge</span>" in INDEX
    assert "Forge<span>Bridge</span>" not in INDEX


def test_history_pagination_does_not_scope_before_id_to_task_filter():
    load_older_start = INDEX.index("async function loadOlder()")
    fetch_messages_start = INDEX.index("async function fetchMessages", load_older_start)
    load_older = INDEX[load_older_start:fetch_messages_start]

    assert "before_id" in load_older
    assert "currentTaskFilter" not in load_older
    assert "params.set('task_id'" not in load_older


def test_task_filter_change_does_not_mutate_history_button_state():
    marker = "document.getElementById('task-filter').addEventListener('change'"
    start = INDEX.index(marker)
    end = INDEX.index("// Boot:", start)
    handler = INDEX[start:end]

    assert "applyFilters()" in handler
    assert "load-older" not in handler
    assert ".hidden" not in handler


def test_polling_connection_state_has_visible_style():
    assert ".conn-status.polling" in INDEX


def test_loaded_history_reapplies_active_task_filter():
    load_older_start = INDEX.index("async function loadOlder()")
    fetch_messages_start = INDEX.index("async function fetchMessages", load_older_start)
    load_older = INDEX[load_older_start:fetch_messages_start]

    prepend_pos = load_older.index("prependMessagesPreservingScroll(messages)")
    filter_pos = load_older.index("applyFilters()")

    assert filter_pos > prepend_pos


def test_new_message_types_appear_in_composer_and_filters():
    for message_type in (
        "ACTION_REQUIRED",
        "APPROVAL_REQUIRED",
        "AUTHORITY_REQUIRED",
        "ACTION_RECONCILIATION_REQUIRED",
        "ACTION_RECONCILED",
        "DIAGNOSTIC",
        "POLICY_BLOCKED",
    ):
        assert f'value="{message_type}"' in INDEX

    assert 'id="message-type-filter"' in INDEX


def test_action_and_approval_metadata_have_safe_rendering():
    assert 'id="composer-action-id"' in INDEX
    assert 'id="composer-approval-id"' in INDEX
    assert "msg.action_id" in INDEX
    assert "msg.approval_id" in INDEX
    assert "msg.next_action" in INDEX
    assert "msg.reason_code" in INDEX
    assert "badge.textContent = text" in INDEX
    assert "createBadge('action'" in INDEX
    assert "createBadge('approval'" in INDEX
    assert "reported action:" in INDEX
    assert "reported approval:" in INDEX
    assert "reported next:" in INDEX
    assert "reported reason:" in INDEX
    assert "Coordination metadata reported by an agent" in INDEX
    assert "DOMPurify.sanitize" in INDEX


def test_typed_messages_have_filters_and_safe_structured_rendering():
    assert 'id="typed-kind-filter"' in INDEX
    assert 'id="correlation-filter"' in INDEX
    assert 'id="composer-typed-kind"' in INDEX
    assert 'id="composer-typed-payload"' in INDEX
    assert "msg.typed" in INDEX
    assert "typed.kind" in INDEX
    assert "buildTypedDetails" in INDEX
    assert "textContent = displayTypedValue(value)" in INDEX
    assert "typed-fields" in INDEX
    assert "currentTypedKindFilter" in INDEX
    assert "currentCorrelationFilter" in INDEX
    assert 'value="DECISION_NOTICE"' in INDEX
    assert "typed_integrity" in INDEX
    assert "buildTypedIntegrityDetails" in INDEX
    assert "const payload = { content };" in INDEX
    assert "const payload = { token, content };" not in INDEX


def test_typed_values_do_not_bypass_html_sanitization():
    typed_start = INDEX.index("function buildTypedDetails")
    typed_end = INDEX.index("function buildMessageElement", typed_start)
    typed_renderer = INDEX[typed_start:typed_end]
    assert "innerHTML" not in typed_renderer
    assert "textContent" in typed_renderer
    assert "JSON.stringify" in INDEX[INDEX.index("function displayTypedValue"):typed_start]


def test_frontend_keeps_typed_schema_separate_from_forgeloop_protocol():
    assert "schema_version: 1" in INDEX
    assert "canonical_refs" in INDEX
    assert "message_key" in INDEX
    assert "reply_to_id" in INDEX
    assert "correlation_id" in INDEX


def test_filter_scope_is_explicitly_loaded_messages_only():
    assert "Filters apply to loaded messages." in INDEX


def test_metadata_filters_keep_authenticated_message_requests():
    assert 'id="action-filter"' in INDEX
    assert 'id="approval-filter"' in INDEX
    assert "currentActionFilter" in INDEX
    assert "currentApprovalFilter" in INDEX
    assert "headers: authHeaders()" in INDEX


def test_browser_has_no_host_authority_secret_controls():
    lowered = INDEX.lower()
    assert "host_grant_token" not in lowered
    assert "authority_secret" not in lowered
    assert "approval_secret" not in lowered
