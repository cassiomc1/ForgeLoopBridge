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
