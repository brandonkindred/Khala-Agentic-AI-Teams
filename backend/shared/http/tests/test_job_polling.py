"""Tests for shared.http.job_polling."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from shared.http import close_pool
from shared.http.job_polling import (
    DEFAULT_TERMINAL_STATUSES,
    get_json,
    poll_until_terminal,
    post_json,
)


@pytest.fixture(autouse=True)
def _clean_pool():
    close_pool()
    yield
    close_pool()


def _mock_client(response=None, raise_for_status_error=None, request_error=None, json_error=None):
    client = MagicMock()
    resp = MagicMock()
    if raise_for_status_error is not None:
        resp.raise_for_status.side_effect = raise_for_status_error
    else:
        resp.raise_for_status = MagicMock()
    if json_error is not None:
        resp.json.side_effect = json_error
    else:
        resp.json.return_value = response
    if request_error is not None:
        client.post.side_effect = request_error
        client.get.side_effect = request_error
    else:
        client.post.return_value = resp
        client.get.return_value = resp
    return client


# --- post_json -----------------------------------------------------------


def test_post_json_returns_parsed_body_on_success():
    client = _mock_client(response={"job_id": "abc"})
    with patch("shared.http.job_polling.get_pooled_client", return_value=client):
        out = post_json("http://x/run", {"a": 1})
    assert out == {"job_id": "abc"}
    client.post.assert_called_once_with("http://x/run", json={"a": 1})


def test_post_json_returns_none_on_http_status_error():
    error = httpx.HTTPStatusError("boom", request=MagicMock(), response=MagicMock())
    client = _mock_client(raise_for_status_error=error)
    with patch("shared.http.job_polling.get_pooled_client", return_value=client):
        assert post_json("http://x/run", {}) is None


def test_post_json_returns_none_on_transport_error():
    client = _mock_client(request_error=httpx.ConnectError("refused"))
    with patch("shared.http.job_polling.get_pooled_client", return_value=client):
        assert post_json("http://x/run", {}) is None


def test_post_json_returns_none_on_json_parse_error():
    client = _mock_client(json_error=ValueError("not json"))
    with patch("shared.http.job_polling.get_pooled_client", return_value=client):
        assert post_json("http://x/run", {}) is None


def test_post_json_rejects_empty_url():
    with pytest.raises(AssertionError):
        post_json("", {})


# --- get_json --------------------------------------------------------------


def test_get_json_returns_parsed_body_on_success():
    client = _mock_client(response={"status": "running"})
    with patch("shared.http.job_polling.get_pooled_client", return_value=client):
        out = get_json("http://x/status/1")
    assert out == {"status": "running"}
    client.get.assert_called_once_with("http://x/status/1")


def test_get_json_returns_none_on_http_status_error():
    error = httpx.HTTPStatusError("boom", request=MagicMock(), response=MagicMock())
    client = _mock_client(raise_for_status_error=error)
    with patch("shared.http.job_polling.get_pooled_client", return_value=client):
        assert get_json("http://x/status/1") is None


def test_get_json_returns_none_on_transport_error():
    client = _mock_client(request_error=httpx.ConnectError("refused"))
    with patch("shared.http.job_polling.get_pooled_client", return_value=client):
        assert get_json("http://x/status/1") is None


def test_get_json_rejects_empty_url():
    with pytest.raises(AssertionError):
        get_json("")


# --- poll_until_terminal -----------------------------------------------------


def test_poll_terminal_immediately_no_sleep(monkeypatch):
    monkeypatch.setattr(
        "shared.http.job_polling.time.sleep",
        lambda *_: (_ for _ in ()).throw(AssertionError("must not sleep")),
    )
    result = poll_until_terminal(lambda: {"status": "completed", "x": 1})
    assert result == {"status": "completed", "x": 1}


def test_poll_terminal_after_n_polls(monkeypatch):
    monkeypatch.setattr("shared.http.job_polling.time.sleep", lambda *_: None)
    statuses = iter([{"status": "running"}, {"status": "running"}, {"status": "completed"}])
    result = poll_until_terminal(lambda: next(statuses), poll_interval=0.01, total_timeout=10)
    assert result == {"status": "completed"}


def test_poll_invokes_on_poll_for_each_non_terminal_status(monkeypatch):
    monkeypatch.setattr("shared.http.job_polling.time.sleep", lambda *_: None)
    seen = []
    statuses = iter(
        [{"status": "running", "n": 1}, {"status": "running", "n": 2}, {"status": "completed"}]
    )
    poll_until_terminal(
        lambda: next(statuses), on_poll=seen.append, poll_interval=0.01, total_timeout=10
    )
    assert [s["n"] for s in seen] == [1, 2]


def test_poll_times_out():
    result = poll_until_terminal(
        lambda: {"status": "running"},
        poll_interval=0.01,
        total_timeout=0.05,
        log_context="widget build",
    )
    assert result == {"status": "failed", "error": "Timed out waiting for widget build"}


def test_poll_short_circuits_on_status_fn_none(monkeypatch):
    monkeypatch.setattr(
        "shared.http.job_polling.time.sleep",
        lambda *_: (_ for _ in ()).throw(AssertionError("must not sleep")),
    )
    calls = {"n": 0}

    def _status_fn():
        calls["n"] += 1
        return None

    result = poll_until_terminal(_status_fn, total_timeout=10)
    assert result == {"status": "failed", "error": "Failed to get status"}
    assert calls["n"] == 1


def test_poll_short_circuits_on_status_fn_exception(monkeypatch):
    monkeypatch.setattr(
        "shared.http.job_polling.time.sleep",
        lambda *_: (_ for _ in ()).throw(AssertionError("must not sleep")),
    )
    calls = {"n": 0}

    def _status_fn():
        calls["n"] += 1
        raise httpx.ConnectError("refused")

    result = poll_until_terminal(_status_fn, total_timeout=10)
    assert result == {"status": "failed", "error": "Failed to get status"}
    assert calls["n"] == 1


def test_poll_respects_custom_status_key_and_terminal_statuses():
    result = poll_until_terminal(
        lambda: {"state": "cancelled"},
        status_key="state",
        terminal_statuses=frozenset({"cancelled"}),
    )
    assert result == {"state": "cancelled"}


def test_poll_rejects_non_positive_poll_interval():
    with pytest.raises(AssertionError):
        poll_until_terminal(lambda: {"status": "completed"}, poll_interval=0)


def test_poll_rejects_non_positive_total_timeout():
    with pytest.raises(AssertionError):
        poll_until_terminal(lambda: {"status": "completed"}, total_timeout=0)


def test_default_terminal_statuses_include_completed_failed_cancelled():
    assert DEFAULT_TERMINAL_STATUSES == frozenset({"completed", "failed", "cancelled"})
