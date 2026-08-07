"""Unit tests for ``shared.cache`` — Memory + FakeRedis, no live Redis required."""

from __future__ import annotations

import threading
import time
from typing import List, Tuple

import pytest

from shared.cache import (
    MemoryBackend,
    RedisBackend,
    close_shared_cache,
    get_shared_cache,
    override_shared_cache_backend,
    reset_shared_cache_state,
)
from shared.cache import config as cache_config
from shared.cache import factory as factory_mod
from shared.cache.tests.fake_redis import FakeRedis


@pytest.fixture(autouse=True)
def _reset_factory(monkeypatch):
    """Isolate each test from factory singletons and ambient Redis env.

    CI (and some local shells) may export ``REDIS_URL``; unit tests must not
    accidentally construct a live ``RedisBackend``.
    """
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.delenv("REDIS_HOST", raising=False)
    reset_shared_cache_state()
    yield
    reset_shared_cache_state()


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def test_is_redis_configured_false_when_unset(monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.delenv("REDIS_HOST", raising=False)
    assert cache_config.is_redis_configured() is False


def test_redis_url_from_host(monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.setenv("REDIS_HOST", "redis")
    monkeypatch.setenv("REDIS_PORT", "6380")
    monkeypatch.setenv("REDIS_DB", "2")
    monkeypatch.setenv("REDIS_PASSWORD", "s3cret")
    assert cache_config.redis_url() == "redis://:s3cret@redis:6380/2"


def test_redis_url_brackets_ipv6_host(monkeypatch):
    """Bare IPv6 REDIS_HOST values must be bracketed for redis-py URL parsing."""
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.setenv("REDIS_HOST", "::1")
    monkeypatch.delenv("REDIS_PASSWORD", raising=False)
    assert cache_config.redis_url() == "redis://[::1]:6379/0"
    monkeypatch.setenv("REDIS_HOST", "[::1]")
    assert cache_config.redis_url() == "redis://[::1]:6379/0"


def test_redis_url_does_not_bracket_host_port(monkeypatch):
    """Colon-bearing host:port strings are rejected (use REDIS_PORT / REDIS_URL)."""
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.setenv("REDIS_HOST", "127.0.0.1:6379")
    monkeypatch.delenv("REDIS_PASSWORD", raising=False)
    with pytest.raises(ValueError, match="must not include a port"):
        cache_config.redis_url()


def test_redis_url_rejects_bracketed_ipv6_with_port(monkeypatch):
    """Bracketed IPv6 hosts must not embed a port either."""
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.setenv("REDIS_HOST", "[::1]:6379")
    monkeypatch.delenv("REDIS_PASSWORD", raising=False)
    with pytest.raises(ValueError, match="must not include a port"):
        cache_config.redis_url()


def test_redis_url_rejects_unclosed_bracket(monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.setenv("REDIS_HOST", "[::1")
    with pytest.raises(ValueError, match="unclosed bracket"):
        cache_config.redis_url()


def test_redis_url_rejects_invalid_bracketed_host(monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.setenv("REDIS_HOST", "[not-an-ip]")
    with pytest.raises(ValueError, match="not a valid IPv6 address"):
        cache_config.redis_url()


def test_redis_url_rejects_zone_id_in_host(monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.setenv("REDIS_HOST", "[fe80::1%eth0]")
    with pytest.raises(ValueError, match="zone ID"):
        cache_config.redis_url()
    monkeypatch.setenv("REDIS_HOST", "fe80::1%eth0")
    with pytest.raises(ValueError, match="zone ID"):
        cache_config.redis_url()


def test_redis_url_explicit_wins(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://explicit:6379/1")
    monkeypatch.setenv("REDIS_HOST", "ignored")
    assert cache_config.redis_url() == "redis://explicit:6379/1"


def test_key_prefix_default_and_blank_fallback(monkeypatch):
    monkeypatch.delenv("REDIS_KEY_PREFIX", raising=False)
    assert cache_config.key_prefix() == "khala"
    monkeypatch.setenv("REDIS_KEY_PREFIX", "   ")
    assert cache_config.key_prefix() == "khala"
    monkeypatch.setenv("REDIS_KEY_PREFIX", "myapp")
    assert cache_config.key_prefix() == "myapp"


def test_key_prefix_rejects_colon(monkeypatch):
    monkeypatch.setenv("REDIS_KEY_PREFIX", "my:app")
    with pytest.raises(ValueError, match="must not contain ':'"):
        cache_config.key_prefix()


def test_default_waiter_timeout_is_independent_of_lock_ttl(monkeypatch):
    """Unset waiter timeout uses a fixed default, not the lock TTL."""
    monkeypatch.delenv("REDIS_WAITER_TIMEOUT_S", raising=False)
    monkeypatch.setenv("REDIS_LOCK_TTL_S", "120")
    assert cache_config.waiter_timeout_s() == 300.0
    monkeypatch.setenv("REDIS_WAITER_TIMEOUT_S", "45")
    assert cache_config.waiter_timeout_s() == 45.0
    monkeypatch.setenv("REDIS_WAITER_TIMEOUT_S", "not-a-float")
    assert cache_config.waiter_timeout_s() == 300.0


def test_default_cache_ttl_is_one_hour(monkeypatch):
    monkeypatch.delenv("REDIS_CACHE_TTL_S", raising=False)
    assert cache_config.cache_ttl_s() == 3600


def test_result_ttl_defaults_to_lock_ttl(monkeypatch):
    """Unset REDIS_RESULT_TTL_S tracks the effective lock TTL."""
    monkeypatch.delenv("REDIS_RESULT_TTL_S", raising=False)
    monkeypatch.setenv("REDIS_LOCK_TTL_S", "90")
    assert cache_config.result_ttl_s() == 90
    monkeypatch.setenv("REDIS_RESULT_TTL_S", "15")
    assert cache_config.result_ttl_s() == 15


def test_redis_socket_connect_timeout_env(monkeypatch):
    monkeypatch.setenv("REDIS_SOCKET_CONNECT_TIMEOUT_S", "2.5")
    assert cache_config.redis_socket_connect_timeout_s() == 2.5


def test_redis_socket_timeout_env(monkeypatch):
    """Command socket_timeout is independently configurable (fail-open on hung I/O)."""
    monkeypatch.delenv("REDIS_SOCKET_TIMEOUT_S", raising=False)
    assert cache_config.redis_socket_timeout_s() == 1.0
    monkeypatch.setenv("REDIS_SOCKET_TIMEOUT_S", "3.5")
    assert cache_config.redis_socket_timeout_s() == 3.5


def test_build_redis_client_passes_socket_timeouts(monkeypatch):
    """Client construction must set both connect and command socket timeouts.

    Without ``socket_timeout``, a Redis that accepts TCP but stalls on GET/SET
    blocks the calling thread forever and breaks fail-open.
    """
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("REDIS_SOCKET_CONNECT_TIMEOUT_S", "2.0")
    monkeypatch.setenv("REDIS_SOCKET_TIMEOUT_S", "4.0")
    monkeypatch.setenv("REDIS_MAX_CONNECTIONS", "17")
    captured: dict = {}

    class _FakeRedisMod:
        class Redis:
            @staticmethod
            def from_url(url, **kwargs):
                captured.update(kwargs)
                return FakeRedis()

    import sys

    monkeypatch.setitem(sys.modules, "redis", _FakeRedisMod)
    client = factory_mod._build_redis_client()
    assert client is not None
    assert captured["socket_connect_timeout"] == 2.0
    assert captured["socket_timeout"] == 4.0
    assert captured["max_connections"] == 17


def test_redis_fail_open_on_timeout_error():
    """Hung command I/O (TimeoutError) must miss/fail-open, never raise."""
    client = FakeRedis()
    cache = RedisBackend(client, "ns")
    cache.set("k", b"v", max_entries=8)

    def _timeout_get(_key: str):
        raise TimeoutError("redis command timed out")

    client.get = _timeout_get  # type: ignore[method-assign]
    assert cache.get("k") is None


# ---------------------------------------------------------------------------
# MemoryBackend
# ---------------------------------------------------------------------------


def test_memory_get_set_and_lru_eviction():
    cache = MemoryBackend()
    cache.set("a", b"1", max_entries=2)
    cache.set("b", b"2", max_entries=2)
    assert cache.get("a") == b"1"  # mark 'a' as recently used
    cache.set("c", b"3", max_entries=2)
    assert cache.get("b") is None  # 'b' was least recently used
    assert cache.get("a") == b"1"
    assert cache.get("c") == b"3"


def test_memory_max_entries_zero_is_noop():
    cache = MemoryBackend()
    cache.set("a", b"1", max_entries=0)
    assert cache.get("a") is None


def test_memory_single_flight_dedup():
    cache = MemoryBackend()
    calls = {"n": 0}
    barrier = threading.Barrier(2)
    results: List[bytes] = []

    def compute() -> Tuple[bytes, bool]:
        calls["n"] += 1
        time.sleep(0.05)
        return b"payload", True

    def worker() -> None:
        barrier.wait()
        results.append(cache.single_flight("k", compute, max_entries=8))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert calls["n"] == 1
    assert results == [b"payload", b"payload"]
    assert cache.get("k") == b"payload"


def test_memory_single_flight_non_cacheable_not_stored():
    cache = MemoryBackend()
    out = cache.single_flight("k", lambda: (b"tmp", False), max_entries=8)
    assert out == b"tmp"
    assert cache.get("k") is None


def test_memory_single_flight_exception_propagates_to_waiter():
    cache = MemoryBackend()
    started = threading.Event()
    errors: List[BaseException] = []

    def compute() -> Tuple[bytes, bool]:
        started.set()
        time.sleep(0.05)
        raise RuntimeError("boom")

    def leader() -> None:
        try:
            cache.single_flight("k", compute, max_entries=8)
        except RuntimeError as exc:
            errors.append(exc)

    def waiter() -> None:
        started.wait(timeout=1)
        time.sleep(0.01)
        try:
            cache.single_flight("k", lambda: (b"nope", True), max_entries=8)
        except RuntimeError as exc:
            errors.append(exc)

    threads = [threading.Thread(target=leader), threading.Thread(target=waiter)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(errors) == 2
    assert all(str(e) == "boom" for e in errors)


def test_memory_clear():
    cache = MemoryBackend()
    cache.set("a", b"1", max_entries=8)
    cache.clear()
    assert cache.get("a") is None


# ---------------------------------------------------------------------------
# RedisBackend (FakeRedis)
# ---------------------------------------------------------------------------


def test_redis_get_set_round_trip():
    client = FakeRedis()
    a = RedisBackend(client, "cr:chunk")
    b = RedisBackend(client, "cr:chunk")
    a.set("k", b"v", max_entries=8)
    assert b.get("k") == b"v"


def test_redis_trim_evicts_oldest():
    client = FakeRedis()
    cache = RedisBackend(client, "ns")
    cache.set("a", b"1", max_entries=2)
    time.sleep(0.01)
    cache.set("b", b"2", max_entries=2)
    time.sleep(0.01)
    cache.set("c", b"3", max_entries=2)
    assert cache.get("a") is None
    assert cache.get("b") == b"2"
    assert cache.get("c") == b"3"


def test_redis_trim_drops_ttl_ghosts_before_evicting_live():
    """TTL-expired ZSET members must not displace colder live keys.

    Value keys expire via EX while the LRU ZSET keeps the member and its score.
    A high-score ghost must be purged on trim; otherwise oldest live keys are
    evicted first and effective capacity shrinks below ``max_entries``.
    """
    client = FakeRedis()
    cache = RedisBackend(client, "ns")
    cache.set("cold", b"1", max_entries=8)
    time.sleep(0.01)
    cache.set("hot", b"2", max_entries=8)
    # Simulate value TTL expiry without touching the LRU ZSET (get() would zrem).
    hot_value_key = cache._value_key("hot")
    client._kv.pop(hot_value_key, None)
    client._expiry.pop(hot_value_key, None)
    assert "hot" in client._zsets[cache._lru_key()]

    cache.set("new", b"3", max_entries=2)

    assert cache.get("cold") == b"1"
    assert cache.get("new") == b"3"
    assert cache.get("hot") is None
    assert "hot" not in client._zsets.get(cache._lru_key(), {})


def test_redis_trim_purges_hot_ghost_beyond_oldest_overflow_window():
    """Ghosts with high scores (not among the oldest overflow) must still be purged."""
    client = FakeRedis()
    cache = RedisBackend(client, "ns")
    cache.set("a", b"1", max_entries=8)
    time.sleep(0.01)
    cache.set("b", b"2", max_entries=8)
    time.sleep(0.01)
    cache.set("ghost", b"g", max_entries=8)
    ghost_vk = cache._value_key("ghost")
    client._kv.pop(ghost_vk, None)
    client._expiry.pop(ghost_vk, None)

    cache.set("c", b"3", max_entries=3)

    assert cache.get("a") == b"1"
    assert cache.get("b") == b"2"
    assert cache.get("c") == b"3"
    assert "ghost" not in client._zsets.get(cache._lru_key(), {})


def test_redis_set_retries_once_after_write_failure():
    """OOM/transient SET failure trims then retries once before fail-open."""
    client = FakeRedis()
    cache = RedisBackend(client, "ns")
    cache.set("keep", b"1", max_entries=8)
    attempts = {"n": 0}
    real_pipeline = client.pipeline

    def flaky_pipeline():
        pipe = real_pipeline()
        real_execute = pipe.execute

        def execute():
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise ConnectionError("OOM command not allowed when used memory")
            return real_execute()

        pipe.execute = execute  # type: ignore[method-assign]
        return pipe

    client.pipeline = flaky_pipeline  # type: ignore[method-assign]
    cache.set("retry", b"2", max_entries=8)
    assert attempts["n"] >= 2
    assert cache.get("retry") == b"2"


def test_redis_backend_rejects_non_positive_ttls():
    client = FakeRedis()
    with pytest.raises(ValueError, match="cache_ttl_s"):
        RedisBackend(client, "ns", cache_ttl_s=-1)
    with pytest.raises(ValueError, match="cache_ttl_s"):
        RedisBackend(client, "ns", cache_ttl_s=0)
    with pytest.raises(ValueError, match="waiter_timeout_s"):
        RedisBackend(client, "ns", waiter_timeout_s=-0.5)


def test_redis_backend_rejects_empty_logical_key():
    client = FakeRedis()
    cache = RedisBackend(client, "ns")
    with pytest.raises(ValueError, match="non-empty"):
        cache.get("")


def test_redis_trim_noop_under_capacity():
    client = FakeRedis()
    cache = RedisBackend(client, "ns")
    cache.set("a", b"1", max_entries=8)
    cache._trim(8)
    assert cache.get("a") == b"1"


def test_redis_trim_batches_large_overflow(monkeypatch):
    """Overflow larger than _TRIM_BATCH is drained across multiple rounds."""
    import shared.cache.redis_backend as rb

    monkeypatch.setattr(rb, "_TRIM_BATCH", 2)
    client = FakeRedis()
    cache = RedisBackend(client, "ns")
    for i in range(7):
        cache.set(f"k{i}", str(i).encode(), max_entries=100)
        time.sleep(0.005)
    cache._trim(3)
    surviving = [cache.get(f"k{i}") for i in range(7)]
    assert sum(1 for v in surviving if v is not None) == 3


def test_redis_trim_fail_open():
    client = FakeRedis()
    cache = RedisBackend(client, "ns")
    cache.set("a", b"1", max_entries=8)
    client.fail_ops.add("zcard")
    cache._trim(1)  # must not raise
    client.fail_ops.clear()
    assert cache.get("a") == b"1"


def test_redis_fail_open_on_get():
    client = FakeRedis()
    cache = RedisBackend(client, "ns")
    cache.set("k", b"v", max_entries=8)
    client.fail_ops.add("get")
    assert cache.get("k") is None


def test_redis_fail_open_on_set():
    client = FakeRedis()
    cache = RedisBackend(client, "ns")
    client.fail_ops.add("pipeline")
    cache.set("k", b"v", max_entries=8)  # must not raise
    client.fail_ops.clear()
    assert cache.get("k") is None


def test_redis_single_flight_across_backends():
    client = FakeRedis()
    a = RedisBackend(client, "ns")
    b = RedisBackend(client, "ns")
    calls = {"n": 0}
    barrier = threading.Barrier(2)
    results: List[bytes] = []

    def compute() -> Tuple[bytes, bool]:
        calls["n"] += 1
        time.sleep(0.08)
        return b"shared", True

    def worker(cache: RedisBackend) -> None:
        barrier.wait()
        results.append(cache.single_flight("k", compute, max_entries=8))

    threads = [
        threading.Thread(target=worker, args=(a,)),
        threading.Thread(target=worker, args=(b,)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert calls["n"] == 1
    assert results == [b"shared", b"shared"]


def test_redis_clear_namespace():
    client = FakeRedis()
    cache = RedisBackend(client, "ns")
    cache.set("k", b"v", max_entries=8)
    cache.clear()
    assert cache.get("k") is None


def test_redis_clear_handles_bytes_keys_from_scan():
    """decode_responses=False clients yield bytes from scan_iter; clear must decode."""
    client = FakeRedis()
    client.scan_yield_bytes = True
    cache = RedisBackend(client, "ns")
    cache.set("k", b"v", max_entries=8)
    cache.clear()
    assert cache.get("k") is None
    assert client._kv == {}


def test_redis_single_flight_disabled_passthrough():
    client = FakeRedis()
    cache = RedisBackend(client, "ns")
    calls = {"n": 0}

    def compute() -> Tuple[bytes, bool]:
        calls["n"] += 1
        return b"x", True

    assert cache.single_flight("k", compute, max_entries=0) == b"x"
    assert calls["n"] == 1
    assert cache.get("k") is None


def test_redis_single_flight_lock_failure_computes_locally():
    client = FakeRedis()
    cache = RedisBackend(client, "ns")
    client.fail_ops.add("set")
    out = cache.single_flight("k", lambda: (b"local", True), max_entries=8)
    assert out == b"local"


def test_redis_single_flight_waiter_recomputes_when_lock_dropped_without_result(
    monkeypatch: pytest.MonkeyPatch,
):
    client = FakeRedis()
    cache = RedisBackend(client, "ns")
    lock_held = threading.Event()
    release_lock = threading.Event()
    waiter_polling = threading.Event()
    calls = {"n": 0}
    results: list[bytes] = []

    def hold_lock() -> None:
        assert client.set(cache._lock_key("k"), b"1", nx=True, ex=30)
        lock_held.set()
        release_lock.wait(timeout=2)
        client.delete(cache._lock_key("k"))

    def compute() -> Tuple[bytes, bool]:
        calls["n"] += 1
        return b"recomputed", True

    orig_wait = cache._wait_for_result

    def wait_and_signal(key: str, result_key: str):
        waiter_polling.set()
        return orig_wait(key, result_key)

    monkeypatch.setattr(cache, "_wait_for_result", wait_and_signal)

    holder = threading.Thread(target=hold_lock)
    holder.start()
    assert lock_held.wait(timeout=2)

    def run_waiter() -> None:
        results.append(cache.single_flight("k", compute, max_entries=8))

    waiter = threading.Thread(target=run_waiter)
    waiter.start()
    assert waiter_polling.wait(timeout=2)
    release_lock.set()
    holder.join(timeout=2)
    waiter.join(timeout=2)

    assert results == [b"recomputed"]
    assert calls["n"] == 1


def test_redis_single_flight_waiter_reraises_leader_exception(
    monkeypatch: pytest.MonkeyPatch,
):
    client = FakeRedis()
    leader = RedisBackend(client, "ns")
    waiter = RedisBackend(client, "ns")
    started = threading.Event()
    release = threading.Event()
    waiter_polling = threading.Event()

    def compute() -> Tuple[bytes, bool]:
        started.set()
        release.wait(timeout=2)
        raise ValueError("leader boom")

    leader_errors: list[BaseException] = []
    waiter_errors: list[BaseException] = []

    orig_wait = waiter._wait_for_result

    def wait_and_signal(key: str, result_key: str):
        waiter_polling.set()
        return orig_wait(key, result_key)

    monkeypatch.setattr(waiter, "_wait_for_result", wait_and_signal)

    def run_leader() -> None:
        try:
            leader.single_flight("k", compute, max_entries=8)
        except BaseException as exc:  # noqa: BLE001 - capture for assertion
            leader_errors.append(exc)

    def run_waiter() -> None:
        try:
            waiter.single_flight("k", lambda: (b"should-not-run", True), max_entries=8)
        except BaseException as exc:  # noqa: BLE001 - capture for assertion
            waiter_errors.append(exc)

    t_leader = threading.Thread(target=run_leader)
    t_leader.start()
    assert started.wait(timeout=2)
    t_waiter = threading.Thread(target=run_waiter)
    t_waiter.start()
    assert waiter_polling.wait(timeout=2)
    release.set()
    t_leader.join(timeout=2)
    t_waiter.join(timeout=2)
    assert len(leader_errors) == 1 and isinstance(leader_errors[0], ValueError)
    assert len(waiter_errors) == 1 and isinstance(waiter_errors[0], ValueError)
    assert "leader boom" in str(waiter_errors[0])


def test_memory_delete_removes_entry():
    cache = MemoryBackend()
    cache.set("k", b"v", max_entries=8)
    cache.delete("k")
    assert cache.get("k") is None


def test_redis_delete_removes_entry():
    client = FakeRedis()
    cache = RedisBackend(client, "ns")
    cache.set("k", b"v", max_entries=8)
    cache.delete("k")
    assert cache.get("k") is None


def test_redis_set_max_entries_zero_noop():
    client = FakeRedis()
    cache = RedisBackend(client, "ns")
    cache.set("k", b"v", max_entries=0)
    assert cache.get("k") is None


def test_redis_get_decodes_str_values():
    client = FakeRedis()
    cache = RedisBackend(client, "ns")
    # Bypass set() to inject a str (some redis clients decode responses).
    client.set_raw(cache._value_key("k"), "hello")
    assert cache.get("k") == b"hello"


def test_redis_clear_fail_open():
    client = FakeRedis()
    cache = RedisBackend(client, "ns")
    cache.set("k", b"v", max_entries=8)
    client.fail_ops.add("scan_iter")
    cache.clear()  # must not raise


def test_redis_url_without_password(monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.setenv("REDIS_HOST", "localhost")
    monkeypatch.delenv("REDIS_PASSWORD", raising=False)
    assert cache_config.redis_url() == "redis://localhost:6379/0"


def test_redis_url_with_password_is_urlencoded(monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.setenv("REDIS_HOST", "localhost")
    monkeypatch.setenv("REDIS_PASSWORD", "p@ss/word#1")
    assert cache_config.redis_url() == "redis://:p%40ss%2Fword%231@localhost:6379/0"


def test_factory_builds_redis_backend(monkeypatch):
    client = FakeRedis()
    monkeypatch.setenv("REDIS_URL", "redis://fake:6379/0")
    monkeypatch.setattr(factory_mod, "_build_redis_client", lambda: client)
    cache = get_shared_cache("cr:chunk")
    assert isinstance(cache, RedisBackend)
    cache.set("k", b"v", max_entries=4)
    assert cache.get("k") == b"v"


def test_close_shared_cache_closes_client(monkeypatch):
    """close_shared_cache invokes client.close() and resets factory state."""
    client = FakeRedis()
    monkeypatch.setenv("REDIS_URL", "redis://fake:6379/0")
    monkeypatch.setattr(factory_mod, "_build_redis_client", lambda: client)
    get_shared_cache("ns")
    close_shared_cache()
    assert client.close_calls == 1
    # After close, a new cache is built fresh.
    monkeypatch.delenv("REDIS_URL", raising=False)
    assert isinstance(get_shared_cache("ns"), MemoryBackend)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def test_factory_defaults_to_memory(monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.delenv("REDIS_HOST", raising=False)
    cache = get_shared_cache("cr:chunk")
    assert isinstance(cache, MemoryBackend)
    assert get_shared_cache("cr:chunk") is cache


def test_factory_override(monkeypatch):
    """Override wins; clearing it rebuilds the default (memory when Redis unset)."""
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.delenv("REDIS_HOST", raising=False)
    mem = MemoryBackend()
    override_shared_cache_backend(mem)
    assert get_shared_cache("anything") is mem
    override_shared_cache_backend(None)
    assert isinstance(get_shared_cache("cr:chunk"), MemoryBackend)


def test_factory_empty_namespace_raises():
    with pytest.raises(ValueError):
        get_shared_cache("")


def test_factory_redis_unreachable_falls_back(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/0")
    monkeypatch.setattr(factory_mod, "_build_redis_client", lambda: None)
    cache = get_shared_cache("cr:chunk")
    assert isinstance(cache, MemoryBackend)


def test_factory_invalid_key_prefix_falls_back_to_memory(monkeypatch):
    """Bad REDIS_KEY_PREFIX must not abort reviews on first get_shared_cache.

    ``key_prefix()`` / ``RedisBackend`` raise ``ValueError`` for ``:`` in the
    prefix; the factory catches construction errors and fails open to memory
    the same way a missing Redis client does.
    """
    client = FakeRedis()
    monkeypatch.setenv("REDIS_URL", "redis://fake:6379/0")
    monkeypatch.setenv("REDIS_KEY_PREFIX", "bad:prefix")
    monkeypatch.setattr(factory_mod, "_build_redis_client", lambda: client)
    cache = get_shared_cache("cr:chunk")
    assert isinstance(cache, MemoryBackend)
    cache.set("k", b"v", max_entries=4)
    assert cache.get("k") == b"v"


def test_redis_single_flight_hit_short_circuits_compute():
    """A durable hit skips compute entirely in single_flight."""
    client = FakeRedis()
    cache = RedisBackend(client, "ns")
    cache.set("k", b"cached", max_entries=8)
    calls = {"n": 0}

    def compute() -> Tuple[bytes, bool]:
        calls["n"] = 1
        return b"x", True

    out = cache.single_flight("k", compute, max_entries=8)
    assert out == b"cached"
    assert calls["n"] == 0


def test_redis_leader_compute_exception_releases_lock():
    """Leader exceptions release the NX lock so later callers are not stuck."""
    client = FakeRedis()
    cache = RedisBackend(client, "ns")

    def boom() -> Tuple[bytes, bool]:
        raise RuntimeError("leader blew up")

    with pytest.raises(RuntimeError, match="leader blew up"):
        cache.single_flight("k", boom, max_entries=8)
    assert client.exists(cache._lock_key("k")) == 0


def test_redis_lock_release_is_token_owned():
    """A leader whose lock expired must not delete a newer leader's token."""
    client = FakeRedis()
    cache = RedisBackend(client, "ns")
    lock_key = cache._lock_key("k")
    client.set(lock_key, b"newer-leader-token", ex=30)
    cache._release_lock(lock_key, b"stale-leader-token")
    assert client.get(lock_key) == b"newer-leader-token"
    cache._release_lock(lock_key, b"newer-leader-token")
    assert client.get(lock_key) is None


def test_redis_waiter_returns_durable_despite_sibling_get_failure():
    """Durable hit must not be discarded when a later result/lock get would fail."""
    client = FakeRedis()
    cache = RedisBackend(client, "ns")
    cache.set("k", b"durable", max_entries=8)

    real_get = client.get
    calls = {"n": 0}

    def flaky_get(key: str):
        calls["n"] += 1
        # After the durable value read, every sibling Redis get fails.
        if calls["n"] > 1:
            raise ConnectionError("sibling get down")
        return real_get(key)

    client.get = flaky_get  # type: ignore[method-assign]
    out = cache._wait_for_result("k", cache._result_key("k"))
    assert out == b"durable"


def test_redis_waiter_reads_result_key():
    """Waiters return the published __sf_result when the durable value is absent."""
    client = FakeRedis()
    cache = RedisBackend(client, "ns")
    # Hold the lock so we take the waiter path, with a result already published.
    from shared.cache.redis_backend import _RESULT_PREFIX

    client.set(cache._lock_key("k"), b"1", nx=True, ex=30)
    client.set(cache._result_key("k"), _RESULT_PREFIX + b"from-leader", ex=30)
    out = cache.single_flight("k", lambda: (b"should-not-run", True), max_entries=8)
    assert out == b"from-leader"


def test_redis_waiter_reads_str_result_and_error_prefix():
    """Waiters decode str results and fail-open past corrupt error markers."""
    client = FakeRedis()
    cache = RedisBackend(client, "ns")
    client.set(cache._lock_key("k"), b"1", nx=True, ex=30)
    client.set_raw(cache._result_key("k"), "str-result")
    assert cache.single_flight("k", lambda: (b"x", True), max_entries=8) == b"str-result"

    client2 = FakeRedis()
    cache2 = RedisBackend(client2, "ns")
    client2.set(cache2._lock_key("k"), b"1", nx=True, ex=30)
    client2.set(cache2._result_key("k"), b"\x00ERR\x00boom", ex=30)
    # Error marker → waiter recomputes
    assert cache2.single_flight("k", lambda: (b"recomputed", True), max_entries=8) == b"recomputed"


def test_redis_non_cacheable_single_flight_not_stored():
    """cacheable=False publishes to waiters but does not durable-store the value."""
    client = FakeRedis()
    cache = RedisBackend(client, "ns")
    out = cache.single_flight("k", lambda: (b"tmp", False), max_entries=8)
    assert out == b"tmp"
    assert cache.get("k") is None


def test_build_redis_client_import_error(monkeypatch):
    """Missing redis package yields None so the factory falls back to memory."""
    import sys

    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    # sys.modules sentinel None → ModuleNotFoundError without patching builtins.
    monkeypatch.delitem(sys.modules, "redis", raising=False)
    monkeypatch.setitem(sys.modules, "redis", None)
    assert factory_mod._build_redis_client() is None


def test_build_redis_client_success(monkeypatch):
    """A constructible redis-py client is returned when REDIS_URL is set."""
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")

    class _FakeRedisMod:
        class Redis:
            @staticmethod
            def from_url(url, **kwargs):
                return FakeRedis()

    import sys

    monkeypatch.setitem(sys.modules, "redis", _FakeRedisMod)
    client = factory_mod._build_redis_client()
    assert client is not None


def test_build_redis_client_no_url(monkeypatch):
    """No REDIS_URL/HOST means no client is constructed."""
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.delenv("REDIS_HOST", raising=False)
    assert factory_mod._build_redis_client() is None


def test_memory_single_flight_disabled():
    """max_entries=0 bypasses single-flight and caching on MemoryBackend."""
    cache = MemoryBackend()
    out = cache.single_flight("k", lambda: (b"x", True), max_entries=0)
    assert out == b"x"
    assert cache.get("k") is None


def test_build_redis_client_from_url_raises(monkeypatch):
    """from_url failures are swallowed and reported as no client."""
    monkeypatch.setenv("REDIS_URL", "redis://:supersecret@localhost:6379/0")

    class _FakeRedisMod:
        class Redis:
            @staticmethod
            def from_url(url, **kwargs):
                raise RuntimeError("bad url")

    import logging
    import sys

    monkeypatch.setitem(sys.modules, "redis", _FakeRedisMod)
    records: list[str] = []

    class _Handler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record.getMessage())

    handler = _Handler()
    log = logging.getLogger("shared.cache.factory")
    log.addHandler(handler)
    try:
        assert factory_mod._build_redis_client() is None
    finally:
        log.removeHandler(handler)
    assert records
    assert "supersecret" not in records[0]
    assert "***" in records[0]


def test_redact_redis_url_password_and_user_only():
    """Password URLs redact; user-only auth URLs are left unchanged."""
    assert "***" in factory_mod._redact_redis_url("redis://alice:s3cret@host:6379/0")
    assert "s3cret" not in factory_mod._redact_redis_url("redis://alice:s3cret@host:6379/0")
    assert factory_mod._redact_redis_url("redis://alice@host:6379/0") == "redis://alice@host:6379/0"
    assert factory_mod._redact_redis_url("redis://host:6379/0") == "redis://host:6379/0"


def test_redact_redis_url_preserves_ipv6_brackets():
    """Rebuilt netloc keeps RFC 3986 brackets around IPv6 hosts."""
    assert factory_mod._redact_redis_url("redis://alice:s3cret@[::1]:6379/0") == ("redis://alice:***@[::1]:6379/0")


def test_redis_rejects_colon_in_logical_key():
    """Logical keys must not contain ':' (namespace ownership invariant)."""
    cache = RedisBackend(FakeRedis(), "ns")
    with pytest.raises(ValueError, match=":"):
        cache.set("bad:key", b"v", max_entries=8)


@pytest.mark.parametrize("reserved", ["__lru", "__sf_lock", "__sf_result", "__sf_custom"])
def test_redis_rejects_reserved_logical_keys(reserved: str):
    """``__lru`` and ``__sf_*`` collide with coordination / LRU metadata keys."""
    cache = RedisBackend(FakeRedis(), "ns")
    with pytest.raises(ValueError, match="reserved"):
        cache.set(reserved, b"v", max_entries=8)
    with pytest.raises(ValueError, match="reserved"):
        cache.get(reserved)


def test_redis_get_rejects_non_string_key():
    """Logical keys must be strings."""
    cache = RedisBackend(FakeRedis(), "ns")
    with pytest.raises(TypeError):
        cache.get(123)  # type: ignore[arg-type]


def test_redis_set_accepts_bytearray_and_memoryview():
    cache = RedisBackend(FakeRedis(), "ns")
    cache.set("a", bytearray(b"ba"), max_entries=8)
    assert cache.get("a") == b"ba"
    cache.set("b", memoryview(b"mv"), max_entries=8)
    assert cache.get("b") == b"mv"


def test_belongs_to_namespace_rejects_nested_and_accepts_suffixes():
    cache = RedisBackend(FakeRedis(), "foo")
    assert cache._belongs_to_namespace("khala:foo:__lru") is True
    assert cache._belongs_to_namespace("khala:foo:k:__sf_lock") is True
    assert cache._belongs_to_namespace("khala:foo:k:__sf_result") is True
    assert cache._belongs_to_namespace("khala:foo:plain") is True
    assert cache._belongs_to_namespace("khala:foo:nested:key") is False
    assert cache._belongs_to_namespace("khala:other:plain") is False


def test_reset_shared_cache_state_closes_client(monkeypatch):
    """reset_shared_cache_state closes any open Redis client best-effort."""
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    closed = {"n": 0}

    class _Client(FakeRedis):
        def close(self) -> None:
            closed["n"] += 1

    client = _Client()
    monkeypatch.setattr(factory_mod, "_build_redis_client", lambda: client)
    assert isinstance(get_shared_cache("ns"), RedisBackend)
    reset_shared_cache_state()
    assert closed["n"] == 1


def test_redis_waiter_rechecks_durable_after_lock_gone(monkeypatch):
    """Waiters re-read the durable value after seeing the lock drop mid-poll."""
    import shared.cache.redis_backend as rb_mod

    client = FakeRedis()
    cache = RedisBackend(client, "ns")
    # Hold the lock so single_flight takes the waiter path, then publish the
    # durable value and drop the lock once the waiter has entered its poll loop.
    client.set(cache._lock_key("k"), b"1", nx=True, ex=30)
    waiter_ready = threading.Event()
    published = threading.Event()

    def unlock_and_publish() -> None:
        assert waiter_ready.wait(timeout=2)
        client.set(cache._value_key("k"), b"late-write", ex=30)
        client.delete(cache._lock_key("k"))
        published.set()

    def fake_sleep(_seconds: float) -> None:
        waiter_ready.set()
        assert published.wait(timeout=2)

    monkeypatch.setattr(rb_mod.time, "sleep", fake_sleep)
    t = threading.Thread(target=unlock_and_publish)
    t.start()
    out = cache.single_flight("k", lambda: (b"should-not-run", True), max_entries=8)
    t.join(timeout=2)
    assert out == b"late-write"


def test_close_shared_cache_safe_when_unused():
    """close_shared_cache is a no-op when Redis was never opened."""
    close_shared_cache()


def test_fake_redis_exists_sees_zset_keys():
    """FakeRedis.exists returns true for sorted-set keys, matching Redis."""
    client = FakeRedis()
    client.zadd("z", {"m": 1.0})
    assert client.exists("z") == 1


def test_fake_redis_delete_counts_key_once():
    """Deleting a key present in both kv and zset stores counts as one."""
    client = FakeRedis()
    client.set("both", b"v")
    client.zadd("both", {"m": 1.0})
    assert client.delete("both") == 1
    assert client.exists("both") == 0


def test_fake_redis_zset_ops_honor_fail_ops():
    """zcard/zrange/zrem raise when listed in FakeRedis.fail_ops."""
    client = FakeRedis()
    client.zadd("z", {"m": 1.0})
    client.fail_ops.add("zcard")
    with pytest.raises(ConnectionError):
        client.zcard("z")
    client.fail_ops = {"zrange"}
    with pytest.raises(ConnectionError):
        client.zrange("z", 0, -1)
    client.fail_ops = {"zrem"}
    with pytest.raises(ConnectionError):
        client.zrem("z", "m")


def test_raise_published_error_uses_message_when_args_empty():
    """Empty error-marker args fall back to the published message string."""
    from shared.cache.redis_backend import RedisBackend

    marker = b'\x00ERR\x00{"module":"builtins","name":"RuntimeError","args":[],"message":"msg-only"}'
    with pytest.raises(RuntimeError, match="msg-only"):
        RedisBackend._raise_published_error(marker)


def test_raise_published_error_typeerror_falls_back(monkeypatch):
    """Constructor TypeError falls back to _LeaderComputeError with message."""
    import types

    from shared.cache import redis_backend as rb
    from shared.cache.redis_backend import RedisBackend, _LeaderComputeError

    class Strict(Exception):
        def __init__(self) -> None:
            super().__init__("x")

    fake_mod = types.SimpleNamespace(Strict=Strict)
    monkeypatch.setattr(rb.importlib, "import_module", lambda _name: fake_mod)
    marker = b'\x00ERR\x00{"module":"shared.x","name":"Strict","args":["a"],"message":"fallback-msg"}'
    with pytest.raises(_LeaderComputeError, match="fallback-msg"):
        RedisBackend._raise_published_error(marker)


def test_clear_does_not_touch_nested_namespace():
    """clear() on ``foo`` must not delete keys belonging to ``foo:bar``."""
    client = FakeRedis()
    parent = RedisBackend(client, "foo")
    child = RedisBackend(client, "foo:bar")
    parent.set("k", b"parent", max_entries=8)
    child.set("k", b"child", max_entries=8)
    parent.clear()
    assert parent.get("k") is None
    assert child.get("k") == b"child"


def test_fake_redis_zadd_counts_only_new_members():
    """zadd returns the number of new members, ignoring score updates."""
    client = FakeRedis()
    assert client.zadd("z", {"a": 1.0, "b": 2.0}) == 2
    assert client.zadd("z", {"a": 3.0, "c": 4.0}) == 1


def test_raise_published_error_allows_team_modules(monkeypatch):
    """Allow-list reconstructs exceptions from code_review_agent / llm_service."""
    import json
    import sys
    import types

    from shared.cache import redis_backend as rb
    from shared.cache.redis_backend import _ERROR_PREFIX, RedisBackend, _LeaderComputeError

    assert any(p.startswith("code_review_agent") for p in rb._ALLOWED_EXC_MODULE_PREFIXES)
    assert any(p.startswith("llm_service") for p in rb._ALLOWED_EXC_MODULE_PREFIXES)
    assert "agents." not in rb._ALLOWED_EXC_MODULE_PREFIXES

    mod = types.ModuleType("code_review_agent.testerr")

    class Boom(Exception):
        pass

    mod.Boom = Boom
    monkeypatch.setitem(sys.modules, "code_review_agent.testerr", mod)
    marker = _ERROR_PREFIX + json.dumps(
        {"module": "code_review_agent.testerr", "name": "Boom", "args": ["x"], "message": "x"}
    ).encode("utf-8")
    with pytest.raises(Boom, match="x"):
        RedisBackend._raise_published_error(marker)

    blocked = _ERROR_PREFIX + json.dumps(
        {"module": "evil.module", "name": "Boom", "args": ["x"], "message": "x"}
    ).encode("utf-8")
    with pytest.raises(_LeaderComputeError):
        RedisBackend._raise_published_error(blocked)


def test_raise_published_error_rejects_poisoned_base_exceptions():
    """Poisoned BaseException markers must not terminate the worker."""
    import json

    from shared.cache.redis_backend import _ERROR_PREFIX, RedisBackend, _LeaderComputeError

    for name in ("SystemExit", "KeyboardInterrupt", "GeneratorExit"):
        marker = _ERROR_PREFIX + json.dumps({"module": "builtins", "name": name, "args": [0], "message": name}).encode(
            "utf-8"
        )
        with pytest.raises(_LeaderComputeError):
            RedisBackend._raise_published_error(marker)


def test_fake_redis_expires_keys():
    """FakeRedis honors ``ex`` TTLs via a controllable ``_now`` clock."""
    fake = FakeRedis()
    fake._now = lambda: 1000.0
    fake.set("k", b"v", ex=10)
    assert fake.get("k") == b"v"
    fake._now = lambda: 1011.0
    assert fake.get("k") is None
    assert "k" not in fake._kv


def test_fake_redis_scan_iter_skips_expired():
    """scan_iter purges expired keys before yielding."""
    fake = FakeRedis()
    fake._now = lambda: 1000.0
    fake.set("a", b"1", ex=5)
    fake.set("b", b"2")
    fake._now = lambda: 1007.0
    assert list(fake.scan_iter(match="*")) == ["b"]


def test_result_envelope_avoids_error_prefix_collision():
    """A payload beginning with error magic is not treated as an error marker."""
    from shared.cache.redis_backend import _RESULT_PREFIX

    client = FakeRedis()
    cache = RedisBackend(client, "ns")
    payload = b"\x00ERR\x00not-an-error-marker"
    client.set(cache._lock_key("k"), b"1", nx=True, ex=30)
    client.set(cache._result_key("k"), _RESULT_PREFIX + payload, ex=30)
    assert cache.single_flight("k", lambda: (b"x", True), max_entries=8) == payload


def test_redis_backend_rejects_empty_namespace():
    """Empty namespace raises ValueError (not an assert that -O could strip)."""
    with pytest.raises(ValueError, match="namespace"):
        RedisBackend(FakeRedis(), "")


def test_redis_set_rejects_non_bytes():
    """set() rejects non-bytes values before talking to Redis."""
    cache = RedisBackend(FakeRedis(), "ns")
    with pytest.raises(TypeError, match="bytes"):
        cache.set("k", "not-bytes", max_entries=8)  # type: ignore[arg-type]
