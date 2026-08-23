from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def test_frontend_bootstrap_requests_latest_page():
    assert "params.set('latest', 'true')" in INDEX


def test_frontend_reconciles_after_sse_open():
    assert "await fetchMessages(false)" in INDEX


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

    assert "applyTaskFilter()" in handler
    assert "load-older" not in handler
    assert ".hidden" not in handler


def test_polling_connection_state_has_visible_style():
    assert ".conn-status.polling" in INDEX


def test_loaded_history_reapplies_active_task_filter():
    load_older_start = INDEX.index("async function loadOlder()")
    fetch_messages_start = INDEX.index("async function fetchMessages", load_older_start)
    load_older = INDEX[load_older_start:fetch_messages_start]

    prepend_pos = load_older.index("prependMessagesPreservingScroll(messages)")
    filter_pos = load_older.index("applyTaskFilter()")

    assert filter_pos > prepend_pos

