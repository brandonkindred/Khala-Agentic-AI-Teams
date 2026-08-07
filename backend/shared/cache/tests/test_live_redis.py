"""Optional live-Redis checks for ``shared.cache``.

Skipped unless invoked with ``pytest -m integration`` and ``REDIS_URL`` points
at a reachable Redis (CI wires a ``redis:7-alpine`` service for this).
"""

from __future__ import annotations

import os
import threading
import time
from typing import List, Union

import pytest

from shared.cache.redis_backend import RedisBackend

TEST_MAX_ENTRIES = 64


def _live_redis_or_skip(request: pytest.FixtureRequest | None = None):
    """Return a redis-py client when REDIS_URL is reachable; otherwise skip."""
    url = os.getenv("REDIS_URL", "").strip()
    if not url:
        pytest.skip("REDIS_URL not set (optional live-Redis integration)")
    try:
        import redis
    except ImportError:
        pytest.skip("redis package not installed")
    client = redis.Redis.from_url(url, decode_responses=False, socket_connect_timeout=1.0)
    try:
        from redis.exceptions import ConnectionError as RedisConnectionError
        from redis.exceptions import TimeoutError as RedisTimeoutError
    except ImportError:  # pragma: no cover
        RedisConnectionError = ConnectionError  # type: ignore[misc,assignment]
        RedisTimeoutError = TimeoutError  # type: ignore[misc,assignment]
    try:
        client.ping()
    except (RedisConnectionError, RedisTimeoutError, OSError) as exc:  # pragma: no cover
        pytest.skip(f"Redis at REDIS_URL unreachable: {exc}")
    if request is not None:
        request.addfinalizer(client.close)
    return client


@pytest.mark.integration
def test_live_redis_cross_backend_hit_and_fail_open(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest):
    """Two backends sharing REDIS_URL round-trip a value; outage fails open.

    Preconditions:
        - ``REDIS_URL`` points at a live Redis.
    Postconditions:
        - A value written by one ``RedisBackend`` is readable by another on the
          same client/namespace (cross-worker cache hit).
        - When the shared client raises on ``get``, ``RedisBackend.get`` returns
          ``None`` rather than propagating (fail-open).
    """
    monkeypatch.setenv("REDIS_CACHE_TTL_S", "30")
    client = _live_redis_or_skip(request)
    ns = "shared.cache:live-it"
    writer = RedisBackend(client, ns)
    reader = RedisBackend(client, ns)
    key = "live-integration-key"
    writer.delete(key)
    writer.set(key, b"from-worker-a", max_entries=TEST_MAX_ENTRIES)
    assert reader.get(key) == b"from-worker-a"
    # Live outage: force the shared client to raise. redis-py reconnects after
    # close()/disconnect(), so a hard raise is the reliable fail-open signal.
    orig_get = client.get

    def _boom(*_a, **_k):
        raise OSError("simulated redis outage")

    monkeypatch.setattr(client, "get", _boom)
    assert reader.get(key) is None
    monkeypatch.setattr(client, "get", orig_get)
    writer.delete(key)


@pytest.mark.integration
def test_live_redis_single_flight_dedupes_work(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest):
    """Two RedisBackend instances share one live single-flight compute.

    Preconditions:
        - ``REDIS_URL`` points at a live Redis.
    Postconditions:
        - Concurrent ``single_flight`` calls for the same key on two backends
          execute ``compute`` once and both return the same payload.
    """
    monkeypatch.setenv("REDIS_CACHE_TTL_S", "30")
    monkeypatch.setenv("REDIS_LOCK_TTL_S", "30")
    monkeypatch.setenv("REDIS_WAITER_TIMEOUT_S", "10")
    client = _live_redis_or_skip(request)
    ns = "shared.cache:live-it:single-flight"
    backend_a = RedisBackend(client, ns)
    backend_b = RedisBackend(client, ns)
    key = "single-flight-key"
    backend_a.clear()

    calls = {"n": 0}
    barrier = threading.Barrier(2)
    call_lock = threading.Lock()

    def _slow_compute():
        with call_lock:
            calls["n"] += 1
        time.sleep(0.2)
        return b"expensive-result", True

    results: List[Union[bytes, BaseException]] = []
    lock = threading.Lock()

    def worker(backend: RedisBackend) -> None:
        barrier.wait(timeout=5)
        try:
            out = backend.single_flight(key, _slow_compute, max_entries=TEST_MAX_ENTRIES)
        except BaseException as exc:  # pragma: no cover - surface for assert
            with lock:
                results.append(exc)
            return
        with lock:
            results.append(out)

    threads = [
        threading.Thread(target=worker, args=(backend_a,)),
        threading.Thread(target=worker, args=(backend_b,)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
    assert results == [b"expensive-result", b"expensive-result"]
    assert calls["n"] == 1
    backend_a.clear()


@pytest.mark.integration
def test_live_redis_oom_set_trim_retries(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest):
    """Under ``noeviction``, SET OOM then namespace LRU trim-retry stores the value.

    Preconditions:
        - ``REDIS_URL`` points at a live Redis that allows ``CONFIG SET``.
    Postconditions:
        - Namespace-owned keys fill Redis; ``maxmemory`` is then squeezed below
          ``used_memory`` so the next write hits OOM; ``RedisBackend.set`` trims
          this namespace's LRU and retries successfully. Asserts a durable hit
          (not merely fail-open). Never raises to the caller.
    """
    monkeypatch.setenv("REDIS_CACHE_TTL_S", "120")
    client = _live_redis_or_skip(request)

    def _cfg(name: str) -> str:
        raw = client.config_get(name)
        # redis-py may return str or bytes keys/values when decode_responses=False.
        for key, val in raw.items():
            key_s = key.decode() if isinstance(key, (bytes, bytearray)) else str(key)
            if key_s == name:
                return val.decode() if isinstance(val, (bytes, bytearray)) else str(val)
        return "0"

    prev_max = _cfg("maxmemory")
    prev_policy = _cfg("maxmemory-policy") or "noeviction"

    def _restore() -> None:
        try:
            client.config_set("maxmemory", prev_max)
            client.config_set("maxmemory-policy", prev_policy)
        except Exception:  # pragma: no cover - best-effort restore
            pass

    request.addfinalizer(_restore)

    ns = "shared.cache:live-it:oom"
    cache = RedisBackend(client, ns)
    cache.clear()

    # Fill while unbounded so every write lands on this namespace's LRU ZSET
    # (raw pad keys outside the ZSET cannot be reclaimed by ``_trim``).
    client.config_set("maxmemory-policy", "noeviction")
    client.config_set("maxmemory", "0")
    chunk = b"x" * (128 * 1024)  # 128 KiB
    filled = 0
    for i in range(24):
        cache.set(f"ns-old-{i}", chunk, max_entries=64)
        if cache.get(f"ns-old-{i}") != chunk:
            break
        filled += 1
    if filled < 4:
        pytest.skip(f"could not fill namespace cache (only {filled} keys stored)")

    used = int(client.info("memory")["used_memory"])
    # Squeeze just under current usage so the next SET is denied under noeviction.
    squeezed = max(used - (256 * 1024), 256 * 1024)
    client.config_set("maxmemory", str(squeezed))

    # max_entries=2 forces trim of the older namespace members on OOM retry.
    cache.set("ns-new", b"after-oom-retry", max_entries=2)
    assert cache.get("ns-new") == b"after-oom-retry"

    cache.clear()
