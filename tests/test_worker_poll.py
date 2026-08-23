from examples import worker_poll


def test_fetch_latest_message_id_uses_latest_mode(monkeypatch):
    captured = {}

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return [{"id": 42}]

    def fake_get(url, *, params, headers, timeout):
        captured["url"] = url
        captured["params"] = params
        captured["headers"] = headers
        return FakeResponse()

    monkeypatch.setattr(worker_poll.requests, "get", fake_get)

    assert worker_poll.fetch_latest_message_id() == 42
    assert captured["params"] == {"latest": "true", "limit": 1}
    assert "Bearer" in captured["headers"]["Authorization"]


def test_fetch_latest_message_id_returns_zero_for_empty_board(monkeypatch):
    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return []

    def fake_get(url, *, params, headers, timeout):
        return FakeResponse()

    monkeypatch.setattr(worker_poll.requests, "get", fake_get)

    assert worker_poll.fetch_latest_message_id() == 0
