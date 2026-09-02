"""Tests for shared.http.job_polling."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from shared.http import close_async_pool, close_pool
from shared.http.job_polling import (
    DEFAULT_TERMINAL_STATUSES,
    async_get_json,
    async_poll_until_terminal,
    async_post_json,
    get_json,
    get_json_with_status,
    poll_until_terminal,
    post_json,
)


@pytest.fixture(autouse=True)
def _clean_pool():
    close_pool()
    close_async_pool()
    yield
    close_pool()
    close_async_pool()


def _make_mock_client(
    *,
    is_async,
    response=None,
    raise_for_status_error=None,
    request_error=None,
    json_error=None,
):
    """Build a mock httpx client (sync or async) for job_polling tests.

    Parameters:
        is_async: When True, returns an ``AsyncMock`` whose ``post``/``get`` are
            awaitables (mirrors ``httpx.AsyncClient``); otherwise a ``MagicMock``
            mirroring ``httpx.Client``.
        response: Value returned by ``resp.json()`` on success.
        raise_for_status_error: If set, ``resp.raise_for_status()`` raises it
            (simulates a non-2xx status).
        request_error: If set, ``client.post``/``client.get`` raise it (simulates
            a transport failure) instead of returning a response.
        json_error: If set, ``resp.json()`` raises it (simulates an unparseable
            body).

    Returns:
        A configured mock client whose ``post``/``get`` yield the response.
    """
    client = AsyncMock() if is_async else MagicMock()
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
        client.post = AsyncMock(side_effect=request_error) if is_async else MagicMock(side_effect=request_error)
        client.get = AsyncMock(side_effect=request_error) if is_async else MagicMock(side_effect=request_error)
    else:
        client.post = AsyncMock(return_value=resp) if is_async else MagicMock(return_value=resp)
        client.get = AsyncMock(return_value=resp) if is_async else MagicMock(return_value=resp)
    return client


def _mock_client(**kwargs):
    """Sync mock client (``httpx.Client`` shape). See :func:`_make_mock_client`."""
    return _make_mock_client(is_async=False, **kwargs)


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


# --- get_json_with_status ---------------------------------------------------


def test_get_json_with_status_returns_status_and_body_on_success():
    client = _mock_client(response={"id": "b"})
    client.get.return_value.status_code = 200
    with patch("shared.http.job_polling.get_pooled_client", return_value=client):
        status_code, body = get_json_with_status("http://x/brand/1")
    assert (status_code, body) == (200, {"id": "b"})
    client.get.assert_called_once_with("http://x/brand/1")


def test_get_json_with_status_returns_status_without_raising_on_404():
    resp = MagicMock()
    resp.status_code = 404
    resp.json.return_value = None
    client = MagicMock()
    client.get = MagicMock(return_value=resp)
    with patch("shared.http.job_polling.get_pooled_client", return_value=client):
        status_code, body = get_json_with_status("http://x/brand/1")
    assert (status_code, body) == (404, None)


def test_get_json_with_status_returns_none_none_on_transport_error():
    client = _mock_client(request_error=httpx.ConnectError("refused"))
    with patch("shared.http.job_polling.get_pooled_client", return_value=client):
        assert get_json_with_status("http://x/brand/1") == (None, None)


def test_get_json_with_status_returns_status_with_none_body_on_bad_json():
    resp = MagicMock()
    resp.status_code = 200
    resp.json.side_effect = ValueError("bad json")
    client = MagicMock()
    client.get = MagicMock(return_value=resp)
    with patch("shared.http.job_polling.get_pooled_client", return_value=client):
        status_code, body = get_json_with_status("http://x/brand/1")
    assert (status_code, body) == (200, None)


def test_get_json_with_status_rejects_empty_url():
    with pytest.raises(AssertionError):
        get_json_with_status("")


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
    statuses = iter([{"status": "running", "n": 1}, {"status": "running", "n": 2}, {"status": "completed"}])
    poll_until_terminal(lambda: next(statuses), on_poll=seen.append, poll_interval=0.01, total_timeout=10)
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


def test_poll_short_circuits_on_on_poll_exception(monkeypatch):
    monkeypatch.setattr(
        "shared.http.job_polling.time.sleep",
        lambda *_: (_ for _ in ()).throw(AssertionError("must not sleep")),
    )

    def _on_poll(_status):
        raise RuntimeError("progress sink failed")

    result = poll_until_terminal(
        lambda: {"status": "running"},
        on_poll=_on_poll,
        poll_interval=0.01,
        total_timeout=10,
    )
    assert result == {"status": "failed", "error": "Progress callback failed"}


class _PauseSignal(Exception):
    """Stand-in for a caller's control-flow signal (e.g. a durable HITL pause)."""


def test_poll_propagates_a_declared_passthrough_exception(monkeypatch):
    """A caller can nominate exceptions ``on_poll`` raises as control flow, not
    failure. Folding those into a failed status silently disables whatever they
    drive, since the caller never sees the signal it is waiting for."""
    monkeypatch.setattr(
        "shared.http.job_polling.time.sleep",
        lambda *_: (_ for _ in ()).throw(AssertionError("must not sleep")),
    )

    def _on_poll(_status):
        raise _PauseSignal("waiting on a human")

    with pytest.raises(_PauseSignal, match="waiting on a human"):
        poll_until_terminal(
            lambda: {"status": "running"},
            on_poll=_on_poll,
            passthrough_exceptions=(_PauseSignal,),
            poll_interval=0.01,
            total_timeout=10,
        )


def test_poll_still_swallows_an_undeclared_exception_when_passthrough_is_set(monkeypatch):
    """The passthrough is a whitelist, not a switch: anything outside it keeps
    failing closed."""
    monkeypatch.setattr(
        "shared.http.job_polling.time.sleep",
        lambda *_: (_ for _ in ()).throw(AssertionError("must not sleep")),
    )

    def _on_poll(_status):
        raise RuntimeError("progress sink failed")

    result = poll_until_terminal(
        lambda: {"status": "running"},
        on_poll=_on_poll,
        passthrough_exceptions=(_PauseSignal,),
        poll_interval=0.01,
        total_timeout=10,
    )
    assert result == {"status": "failed", "error": "Progress callback failed"}


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


def _mock_async_client(**kwargs):
    """Async mock client (``httpx.AsyncClient`` shape). See :func:`_make_mock_client`."""
    return _make_mock_client(is_async=True, **kwargs)


@pytest.mark.asyncio
async def test_async_post_json_returns_parsed_body_on_success():
    client = _mock_async_client(response={"job_id": "abc"})
    with patch("shared.http.job_polling.get_pooled_async_client", return_value=client):
        out = await async_post_json("http://x/run", {"a": 1})
    assert out == {"job_id": "abc"}
    client.post.assert_awaited_once_with("http://x/run", json={"a": 1})


@pytest.mark.asyncio
async def test_async_post_json_returns_none_on_http_status_error():
    error = httpx.HTTPStatusError("boom", request=MagicMock(), response=MagicMock())
    client = _mock_async_client(raise_for_status_error=error)
    with patch("shared.http.job_polling.get_pooled_async_client", return_value=client):
        assert await async_post_json("http://x/run", {}) is None


@pytest.mark.asyncio
async def test_async_post_json_returns_none_on_transport_error():
    client = _mock_async_client(request_error=httpx.ConnectError("refused"))
    with patch("shared.http.job_polling.get_pooled_async_client", return_value=client):
        assert await async_post_json("http://x/run", {}) is None


@pytest.mark.asyncio
async def test_async_post_json_returns_none_on_json_parse_error():
    client = _mock_async_client(json_error=ValueError("not json"))
    with patch("shared.http.job_polling.get_pooled_async_client", return_value=client):
        assert await async_post_json("http://x/run", {}) is None


@pytest.mark.asyncio
async def test_async_post_json_rejects_empty_url():
    with pytest.raises(AssertionError):
        await async_post_json("", {})


@pytest.mark.asyncio
async def test_async_get_json_returns_parsed_body_on_success():
    client = _mock_async_client(response={"status": "running"})
    with patch("shared.http.job_polling.get_pooled_async_client", return_value=client):
        out = await async_get_json("http://x/status/1")
    assert out == {"status": "running"}
    client.get.assert_awaited_once_with("http://x/status/1")


@pytest.mark.asyncio
async def test_async_get_json_returns_none_on_http_status_error():
    error = httpx.HTTPStatusError("boom", request=MagicMock(), response=MagicMock())
    client = _mock_async_client(raise_for_status_error=error)
    with patch("shared.http.job_polling.get_pooled_async_client", return_value=client):
        assert await async_get_json("http://x/status/1") is None


@pytest.mark.asyncio
async def test_async_get_json_returns_none_on_transport_error():
    client = _mock_async_client(request_error=httpx.ConnectError("refused"))
    with patch("shared.http.job_polling.get_pooled_async_client", return_value=client):
        assert await async_get_json("http://x/status/1") is None


@pytest.mark.asyncio
async def test_async_get_json_rejects_empty_url():
    with pytest.raises(AssertionError):
        await async_get_json("")


@pytest.mark.asyncio
async def test_async_poll_terminal_immediately_no_sleep(monkeypatch):
    async def _must_not_sleep(*_):
        raise AssertionError("must not sleep")

    monkeypatch.setattr("shared.http.job_polling.asyncio.sleep", _must_not_sleep)

    async def _status():
        return {"status": "completed", "x": 1}

    result = await async_poll_until_terminal(_status)
    assert result == {"status": "completed", "x": 1}


@pytest.mark.asyncio
async def test_async_poll_terminal_after_n_polls(monkeypatch):
    async def _nosleep(*_):
        return None

    monkeypatch.setattr("shared.http.job_polling.asyncio.sleep", _nosleep)
    statuses = iter([{"status": "running"}, {"status": "running"}, {"status": "completed"}])

    async def _status():
        return next(statuses)

    result = await async_poll_until_terminal(_status, poll_interval=0.01, total_timeout=10)
    assert result == {"status": "completed"}


@pytest.mark.asyncio
async def test_async_poll_invokes_on_poll_for_each_non_terminal_status(monkeypatch):
    async def _nosleep(*_):
        return None

    monkeypatch.setattr("shared.http.job_polling.asyncio.sleep", _nosleep)
    seen: list[dict] = []
    statuses = iter([{"status": "running", "n": 1}, {"status": "running", "n": 2}, {"status": "completed"}])

    async def _status():
        return next(statuses)

    async def _on_poll(status: dict) -> None:
        seen.append(status)

    await async_poll_until_terminal(_status, on_poll=_on_poll, poll_interval=0.01, total_timeout=10)
    assert [s["n"] for s in seen] == [1, 2]


@pytest.mark.asyncio
async def test_async_poll_times_out():
    async def _status():
        return {"status": "running"}

    result = await async_poll_until_terminal(
        _status,
        poll_interval=0.01,
        total_timeout=0.05,
        log_context="widget build",
    )
    assert result == {"status": "failed", "error": "Timed out waiting for widget build"}


@pytest.mark.asyncio
async def test_async_poll_short_circuits_on_status_fn_none(monkeypatch):
    async def _must_not_sleep(*_):
        raise AssertionError("must not sleep")

    monkeypatch.setattr("shared.http.job_polling.asyncio.sleep", _must_not_sleep)
    calls = {"n": 0}

    async def _status_fn():
        calls["n"] += 1
        return None

    result = await async_poll_until_terminal(_status_fn, total_timeout=10)
    assert result == {"status": "failed", "error": "Failed to get status"}
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_async_poll_short_circuits_on_status_fn_exception(monkeypatch):
    async def _must_not_sleep(*_):
        raise AssertionError("must not sleep")

    monkeypatch.setattr("shared.http.job_polling.asyncio.sleep", _must_not_sleep)
    calls = {"n": 0}

    async def _status_fn():
        calls["n"] += 1
        raise httpx.ConnectError("refused")

    result = await async_poll_until_terminal(_status_fn, total_timeout=10)
    assert result == {"status": "failed", "error": "Failed to get status"}
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_async_poll_respects_custom_status_key_and_terminal_statuses():
    async def _status():
        return {"state": "cancelled"}

    result = await async_poll_until_terminal(
        _status,
        status_key="state",
        terminal_statuses=frozenset({"cancelled"}),
    )
    assert result == {"state": "cancelled"}


@pytest.mark.asyncio
async def test_async_poll_rejects_non_positive_poll_interval():
    async def _status():
        return {"status": "completed"}

    with pytest.raises(AssertionError):
        await async_poll_until_terminal(_status, poll_interval=0)


@pytest.mark.asyncio
async def test_async_poll_rejects_non_positive_total_timeout():
    async def _status():
        return {"status": "completed"}

    with pytest.raises(AssertionError):
        await async_poll_until_terminal(_status, total_timeout=0)


@pytest.mark.asyncio
async def test_async_poll_times_out_when_status_fn_stalls():
    """A status_fn that never returns must still honor total_timeout."""

    async def _stall():
        await asyncio.sleep(60.0)
        return {"status": "running"}

    result = await async_poll_until_terminal(
        _stall,
        poll_interval=0.01,
        total_timeout=0.05,
        log_context="stalled job",
    )
    assert result == {"status": "failed", "error": "Timed out waiting for stalled job"}


@pytest.mark.asyncio
async def test_async_poll_budget_timeout_ignores_late_success_after_cancel():
    """Slow cancel cleanup + late terminal result must not beat the deadline."""

    async def _suppress_cancel_then_complete():
        try:
            await asyncio.sleep(60.0)
        except asyncio.CancelledError:
            await asyncio.sleep(0.2)  # longer than cancel grace
            return {"status": "completed"}
        return {"status": "completed"}

    result = await async_poll_until_terminal(
        _suppress_cancel_then_complete,
        poll_interval=0.01,
        total_timeout=0.05,
        log_context="late success",
    )
    assert result == {"status": "failed", "error": "Timed out waiting for late success"}
    # Let the detached (cancellation-suppressing) task run to completion so its
    # result is consumed by the discard callback rather than leaking.
    await asyncio.sleep(0.3)


@pytest.mark.asyncio
async def test_async_poll_budget_timeout_discards_late_exception_from_detached_task():
    """A cancellation-suppressing callback that later raises must not leak; the
    poller returns the budget timeout and the late exception is discarded."""

    async def _suppress_cancel_then_raise():
        try:
            await asyncio.sleep(60.0)
        except asyncio.CancelledError:
            await asyncio.sleep(0.2)  # longer than cancel grace
            raise RuntimeError("late failure after deadline")
        return {"status": "completed"}

    result = await async_poll_until_terminal(
        _suppress_cancel_then_raise,
        poll_interval=0.01,
        total_timeout=0.05,
        log_context="late failure",
    )
    assert result == {"status": "failed", "error": "Timed out waiting for late failure"}
    # Drain the detached task so its late exception is retrieved (not orphaned).
    await asyncio.sleep(0.3)


@pytest.mark.asyncio
async def test_async_poll_callback_timeout_error_is_status_failure_not_budget():
    """status_fn raising TimeoutError is callback failure, not budget expiry."""

    async def _status_fn():
        raise asyncio.TimeoutError("per-request timed out")

    result = await async_poll_until_terminal(
        _status_fn,
        poll_interval=0.01,
        total_timeout=10.0,
        log_context="should not appear",
    )
    assert result == {"status": "failed", "error": "Failed to get status"}


@pytest.mark.asyncio
async def test_async_poll_on_poll_timeout_error_is_callback_failure_not_budget():
    """on_poll raising TimeoutError is a callback failure, not budget expiry,
    and short-circuits before any sleep — so the inter-poll sleep is never
    reached and does not need patching."""

    async def _status():
        return {"status": "running"}

    async def _on_poll(_ctx: dict) -> None:
        raise asyncio.TimeoutError("progress sink timed out")

    result = await async_poll_until_terminal(
        _status,
        on_poll=_on_poll,
        poll_interval=0.01,
        total_timeout=10.0,
        log_context="should not appear",
    )
    assert result == {"status": "failed", "error": "Progress callback failed"}


@pytest.mark.asyncio
async def test_async_poll_short_circuits_on_on_poll_exception(monkeypatch):
    async def _must_not_sleep(*_):
        raise AssertionError("must not sleep")

    monkeypatch.setattr("shared.http.job_polling.asyncio.sleep", _must_not_sleep)

    async def _status():
        return {"status": "running"}

    async def _on_poll(_ctx: dict) -> None:
        raise RuntimeError("progress sink failed")

    result = await async_poll_until_terminal(_status, on_poll=_on_poll, poll_interval=0.01, total_timeout=10)
    assert result == {"status": "failed", "error": "Progress callback failed"}


def test_default_terminal_statuses_shared_singleton_for_async_default():
    """Async poll default must be the same frozenset object as the module constant."""
    import inspect

    from shared.http import job_polling as jp

    sig = inspect.signature(jp.async_poll_until_terminal)
    assert sig.parameters["terminal_statuses"].default is DEFAULT_TERMINAL_STATUSES


@pytest.mark.asyncio
async def test_async_poll_cancellation_does_not_strand_status_fn():
    """Cancelling the poller must cancel the in-flight status_fn, not leave it
    running detached with an unretrieved outcome."""
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def _status():
        started.set()
        try:
            await asyncio.sleep(60.0)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return {"status": "completed"}

    poll = asyncio.ensure_future(async_poll_until_terminal(_status, poll_interval=0.01, total_timeout=30.0))
    await asyncio.wait_for(started.wait(), timeout=5.0)
    poll.cancel()
    with pytest.raises(asyncio.CancelledError):
        await poll
    await asyncio.wait_for(cancelled.wait(), timeout=5.0)


@pytest.mark.asyncio
async def test_async_poll_cancellation_discards_late_callback_exception():
    """A status_fn that raises while being cancelled must not surface an
    'exception was never retrieved' orphan after the poller is cancelled."""
    started = asyncio.Event()
    done = asyncio.Event()

    async def _status():
        started.set()
        try:
            await asyncio.sleep(60.0)
        except asyncio.CancelledError:
            done.set()
            raise RuntimeError("late failure during cancellation")
        return {"status": "completed"}

    poll = asyncio.ensure_future(async_poll_until_terminal(_status, poll_interval=0.01, total_timeout=30.0))
    await asyncio.wait_for(started.wait(), timeout=5.0)
    poll.cancel()
    with pytest.raises(asyncio.CancelledError):
        await poll
    await asyncio.wait_for(done.wait(), timeout=5.0)
    await asyncio.sleep(0)  # let the discard done-callback consume the exception


@pytest.mark.asyncio
async def test_async_get_json_returns_none_when_client_closed_mid_flight():
    """httpx raises RuntimeError (not HTTPError) on a closed client; the
    never-raises contract must still hold."""
    client = _mock_async_client()
    client.get = AsyncMock(side_effect=RuntimeError("Cannot send a request, as the client has been closed."))
    with patch("shared.http.job_polling.get_pooled_async_client", return_value=client):
        assert await async_get_json("http://x/status/1") is None


def test_get_json_returns_none_when_client_closed_mid_flight():
    """Sync counterpart: a closed pooled client must not raise out of get_json."""
    client = _mock_client()
    client.get = MagicMock(side_effect=RuntimeError("Cannot send a request, as the client has been closed."))
    with patch("shared.http.job_polling.get_pooled_client", return_value=client):
        assert get_json("http://x/status/1") is None


def test_poll_on_poll_failure_is_distinct_from_status_failure():
    """A broken progress sink must not be reported as an unreadable status."""

    def _on_poll(_status):
        raise RuntimeError("progress sink failed")

    on_poll_result = poll_until_terminal(
        lambda: {"status": "running"},
        on_poll=_on_poll,
        poll_interval=0.01,
        total_timeout=10,
    )
    status_result = poll_until_terminal(
        lambda: None,
        poll_interval=0.01,
        total_timeout=10,
    )
    assert on_poll_result["error"] != status_result["error"]
    assert on_poll_result == {"status": "failed", "error": "Progress callback failed"}
    assert status_result == {"status": "failed", "error": "Failed to get status"}


@pytest.mark.asyncio
async def test_async_poll_discards_exception_raised_inside_cancel_grace():
    """A callback that raises *within* the 0.05s cancel grace must still have its
    exception consumed — it lands in `done`, so the detach path is not taken.

    An unretrieved task exception is only reported when the task is garbage
    collected, so this forces a collection and asserts the loop's exception
    handler never saw it.
    """
    import gc

    handled: list[dict] = []
    asyncio.get_running_loop().set_exception_handler(lambda _loop, ctx: handled.append(ctx))

    async def _raise_during_cancel():
        try:
            await asyncio.sleep(60.0)
        except asyncio.CancelledError:
            raise RuntimeError("raised inside the grace window") from None

    result = await async_poll_until_terminal(
        _raise_during_cancel,
        poll_interval=0.01,
        total_timeout=0.05,
        log_context="grace raise",
    )
    assert result == {"status": "failed", "error": "Timed out waiting for grace raise"}

    gc.collect()
    await asyncio.sleep(0)
    assert not [c for c in handled if "never retrieved" in c.get("message", "")], handled


def test_get_json_propagates_unrelated_runtime_error():
    """Only httpx's closed-client RuntimeError is swallowed; a genuine bug in the
    helper must not be reported as a benign request failure."""
    client = _mock_client()
    client.get = MagicMock(side_effect=RuntimeError("something is actually broken"))
    with patch("shared.http.job_polling.get_pooled_client", return_value=client):
        with pytest.raises(RuntimeError, match="actually broken"):
            get_json("http://x/status/1")


@pytest.mark.asyncio
async def test_async_get_json_propagates_unrelated_runtime_error():
    client = _mock_async_client()
    client.get = AsyncMock(side_effect=RuntimeError("something is actually broken"))
    with patch("shared.http.job_polling.get_pooled_async_client", return_value=client):
        with pytest.raises(RuntimeError, match="actually broken"):
            await async_get_json("http://x/status/1")


def test_post_json_propagates_unrelated_runtime_error():
    client = _mock_client()
    client.post = MagicMock(side_effect=RuntimeError("something is actually broken"))
    with patch("shared.http.job_polling.get_pooled_client", return_value=client):
        with pytest.raises(RuntimeError, match="actually broken"):
            post_json("http://x/run", {})


@pytest.mark.asyncio
async def test_async_post_json_returns_none_when_client_closed_mid_flight():
    client = _mock_async_client()
    client.post = AsyncMock(side_effect=RuntimeError("Cannot send a request, as the client has been closed."))
    with patch("shared.http.job_polling.get_pooled_async_client", return_value=client):
        assert await async_post_json("http://x/run", {}) is None


def test_poll_rejects_a_non_exception_passthrough_entry():
    """A bad entry would otherwise surface as a TypeError chained onto whatever
    on_poll actually raised, mid-poll and far from the call site — the failure
    mode the file's other preconditions use asserts to avoid."""
    with pytest.raises(AssertionError, match="passthrough_exceptions"):
        poll_until_terminal(
            lambda: {"status": "running"},
            passthrough_exceptions=(str,),  # type: ignore[arg-type]
        )


def test_poll_rejects_a_bare_class_passed_as_passthrough_exceptions():
    """The parameter is a tuple of types, not a single class.

    The precondition rejects a bare class before polling starts, so the mistake
    surfaces at the call site rather than at the except clause mid-poll.
    """
    with pytest.raises(AssertionError, match="passthrough_exceptions"):
        poll_until_terminal(
            lambda: {"status": "running"},
            passthrough_exceptions=_PauseSignal,  # type: ignore[arg-type]
        )


def test_poll_rejects_a_list_of_exception_types():
    """A LIST of real exception types is the input an element-only check lets
    through — and the only one that still reaches ``except passthrough_exceptions:``,
    where it raises TypeError chained onto whatever on_poll actually threw."""
    with pytest.raises(AssertionError, match="passthrough_exceptions"):
        poll_until_terminal(
            lambda: {"status": "running"},
            passthrough_exceptions=[_PauseSignal],  # type: ignore[arg-type]
        )
