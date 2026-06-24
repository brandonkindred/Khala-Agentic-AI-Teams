"""Tests for the shared pooled HTTP client."""

from __future__ import annotations

import httpx
import pytest

import shared_http
from shared_http import (
    _DEFAULT_KEEPALIVE_EXPIRY_S,
    DEFAULT_LIMITS,
    _keepalive_expiry_seconds,
    close_pool,
    get_pooled_client,
)


@pytest.fixture(autouse=True)
def _clean_pool():
    close_pool()
    yield
    close_pool()


def test_same_timeout_returns_same_instance():
    a = get_pooled_client(30.0)
    b = get_pooled_client(30.0)
    assert a is b


def test_close_terminates_existing_client_and_replaces_on_next_get():
    """A pooled client returned before ``close_pool`` is closed; the next get
    yields a fresh live client (a closed client must never be handed out)."""
    a = get_pooled_client(30.0)
    close_pool()
    assert a.is_closed
    b = get_pooled_client(30.0)
    assert b is not a
    assert not b.is_closed


def test_different_timeout_buckets_are_isolated():
    fast = get_pooled_client(5.0)
    slow = get_pooled_client(30.0)
    assert fast is not slow


def test_near_equal_timeouts_share_bucket():
    a = get_pooled_client(30.0)
    b = get_pooled_client(30.0001)
    assert a is b


def test_default_limits_bound_concurrency():
    assert DEFAULT_LIMITS.max_connections == 50
    assert DEFAULT_LIMITS.max_keepalive_connections == 20


def test_default_limits_recycle_idle_keepalive_sockets():
    """Idle keep-alive sockets must expire (default 15s) so the client drops
    them before an upstream closes them — avoiding ``RemoteProtocolError`` on
    reuse of a server-closed connection."""
    assert DEFAULT_LIMITS.keepalive_expiry == _DEFAULT_KEEPALIVE_EXPIRY_S == 15.0


def test_pooled_client_applies_limits_and_timeout():
    client = get_pooled_client(12.0)
    assert isinstance(client, httpx.Client)
    assert client.timeout.read == 12.0
    # Limits are applied to the underlying connection pool.
    pool = client._transport._pool  # noqa: SLF001 — assert pooling config
    assert pool._max_connections == DEFAULT_LIMITS.max_connections
    assert pool._max_keepalive_connections == DEFAULT_LIMITS.max_keepalive_connections
    assert pool._keepalive_expiry == DEFAULT_LIMITS.keepalive_expiry


def test_keepalive_expiry_defaults_when_env_unset(monkeypatch):
    monkeypatch.delenv("HTTP_KEEPALIVE_EXPIRY_S", raising=False)
    assert _keepalive_expiry_seconds() == _DEFAULT_KEEPALIVE_EXPIRY_S


def test_keepalive_expiry_honours_valid_env(monkeypatch):
    monkeypatch.setenv("HTTP_KEEPALIVE_EXPIRY_S", "42.5")
    assert _keepalive_expiry_seconds() == 42.5


@pytest.mark.parametrize("bad", ["", "abc", "12s", "nan", "inf", "0", "-5"])
def test_keepalive_expiry_falls_back_on_invalid_env(monkeypatch, bad):
    """Garbage, non-finite, and non-positive values fall back to the default
    rather than producing an unusable pool config."""
    monkeypatch.setenv("HTTP_KEEPALIVE_EXPIRY_S", bad)
    assert _keepalive_expiry_seconds() == _DEFAULT_KEEPALIVE_EXPIRY_S


def test_replaces_externally_closed_client():
    a = get_pooled_client(7.0)
    a.close()
    b = get_pooled_client(7.0)
    assert b is not a
    assert not b.is_closed


def test_close_pool_is_idempotent():
    get_pooled_client(9.0)
    close_pool()
    close_pool()  # second call must not raise
    assert get_pooled_client(9.0) is not None


def test_invalid_timeout_rejected():
    with pytest.raises(AssertionError):
        get_pooled_client(0)
    with pytest.raises(AssertionError):
        get_pooled_client(-5)


def test_non_finite_timeout_rejected():
    """A non-finite timeout passes ``> 0`` but violates the documented
    ``finite`` precondition, so it must be rejected rather than producing an
    ``inf`` bucket key."""
    with pytest.raises(AssertionError):
        get_pooled_client(float("inf"))
    with pytest.raises(AssertionError):
        get_pooled_client(float("nan"))


def test_close_pool_swallows_client_close_errors(monkeypatch):
    """``close_pool`` is best-effort teardown — a failing ``client.close()``
    must not propagate."""
    get_pooled_client(15.0)

    def _boom(self):
        raise RuntimeError("close failed")

    monkeypatch.setattr(httpx.Client, "close", _boom, raising=True)
    close_pool()  # must not raise
    # Pool was cleared despite the close error.
    assert shared_http._clients == {}  # noqa: SLF001 — verify teardown cleared state
