"""Cross-process / shared-backend integration tests for the three review caches.

Uses a shared FakeRedis so two ``RedisBackend`` instances (stand-ins for two
worker processes) prove cache hits survive across "process" boundaries and that
a Redis outage fails open to a miss rather than raising.

These tests intentionally import private namespace / serialization helpers
from the owning modules (``_chunk_cache_namespace()``, etc.) — those helpers
are the production path (including optional ``KHALA_BUILD_ID`` suffixing).
"""

from __future__ import annotations

from typing import List, Tuple

import pytest

from shared.cache import MemoryBackend, RedisBackend, reset_shared_cache_state
from shared.cache.tests.fake_redis import FakeRedis

# Arbitrary positive capacity so set/single_flight exercise the cache path
# (not eviction). Keep small for readable fixtures.
TEST_MAX_ENTRIES = 8


@pytest.fixture(autouse=True)
def _reset(monkeypatch: pytest.MonkeyPatch):
    # Keep namespaces at static stems unless a test sets a build id explicitly.
    monkeypatch.delenv("KHALA_BUILD_ID", raising=False)
    monkeypatch.delenv("KHALA_CACHE_BUILD_ID", raising=False)
    reset_shared_cache_state()
    yield
    reset_shared_cache_state()


def test_chunk_outcome_cache_hit_across_two_backends():
    """Identical chunk keys hit across two RedisBackend instances (two workers)."""
    from code_review_agent.mapping import (
        _chunk_cache_namespace,
        _chunk_outcome_from_bytes,
        _chunk_outcome_to_bytes,
        _ChunkOutcome,
    )
    from code_review_agent.models import CodeReviewIssue

    client = FakeRedis()
    ns = _chunk_cache_namespace()
    writer = RedisBackend(client, ns)
    reader = RedisBackend(client, ns)

    outcome = _ChunkOutcome(
        issues=[CodeReviewIssue(severity="low", file_path="a.py", description="nits")],
        summaries=["ok"],
        approved_flags=[True],
    )
    key = "chunk-key-1"
    writer.set(key, _chunk_outcome_to_bytes(outcome), max_entries=TEST_MAX_ENTRIES)

    raw = reader.get(key)
    assert raw is not None
    got = _chunk_outcome_from_bytes(raw)
    assert got.summaries == ["ok"]
    assert got.issues[0].description == "nits"
    assert got.approved_flags == [True]


def test_submission_cache_hit_across_two_backends():
    """Submission outputs round-trip across two RedisBackend instances."""
    from code_review_agent.coordinator import _submission_cache_namespace
    from code_review_agent.models import CodeReviewOutput

    client = FakeRedis()
    ns = _submission_cache_namespace()
    writer = RedisBackend(client, ns)
    reader = RedisBackend(client, ns)

    output = CodeReviewOutput(approved=True, issues=[], summary="clean")
    key = "sub-key-1"
    writer.set(key, output.model_dump_json().encode("utf-8"), max_entries=TEST_MAX_ENTRIES)

    raw = reader.get(key)
    assert raw is not None
    got = CodeReviewOutput.model_validate_json(raw)
    assert got.approved is True
    assert got.summary == "clean"


def test_compaction_cache_hit_across_two_backends():
    """Compaction memo bytes are readable from a second RedisBackend."""
    from llm_service.compaction import _compaction_cache_namespace

    client = FakeRedis()
    ns = _compaction_cache_namespace()
    writer = RedisBackend(client, ns)
    reader = RedisBackend(client, ns)

    key = "compact-key-1"
    writer.set(key, b"compacted text", max_entries=TEST_MAX_ENTRIES)
    assert reader.get(key) == b"compacted text"


def test_cache_survives_simulated_process_restart():
    """Clearing the local Memory layer still leaves Redis entries readable."""
    client = FakeRedis()
    redis_cache = RedisBackend(client, "cr:chunk")
    local = MemoryBackend()

    redis_cache.set("k", b"durable", max_entries=TEST_MAX_ENTRIES)
    local.set("k", b"local-only", max_entries=TEST_MAX_ENTRIES)
    local.clear()  # simulate process-local restart

    assert local.get("k") is None
    assert redis_cache.get("k") == b"durable"


def test_redis_down_degrades_to_miss_not_exception():
    """Redis get/set failures degrade to miss/no-op rather than raising."""
    client = FakeRedis()
    cache = RedisBackend(client, "cr:chunk")
    cache.set("k", b"v", max_entries=TEST_MAX_ENTRIES)
    client.fail_ops.add("get")
    assert cache.get("k") is None  # miss, not raise

    client.fail_ops.add("pipeline")
    cache.set("k2", b"v2", max_entries=TEST_MAX_ENTRIES)  # no raise


def test_single_flight_shared_across_two_redis_backends():
    """Two backends sharing FakeRedis run compute only once under contention."""
    import threading
    import time

    client = FakeRedis()
    a = RedisBackend(client, "cr:chunk")
    b = RedisBackend(client, "cr:chunk")
    calls = {"n": 0}
    barrier = threading.Barrier(2)
    results: List[bytes] = []

    def compute() -> Tuple[bytes, bool]:
        calls["n"] += 1
        time.sleep(0.05)
        return b"one-shot", True

    def worker(cache: RedisBackend) -> None:
        barrier.wait()
        results.append(cache.single_flight("sf-key", compute, max_entries=TEST_MAX_ENTRIES))

    threads = [
        threading.Thread(target=worker, args=(a,)),
        threading.Thread(target=worker, args=(b,)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
        assert not t.is_alive(), "single-flight worker thread hung"
    assert calls["n"] == 1
    assert results == [b"one-shot", b"one-shot"]


def test_namespaces_are_isolated_on_shared_client():
    """Identical raw keys in different namespaces do not collide."""
    client = FakeRedis()
    chunk = RedisBackend(client, "cr:chunk:v1")
    sub = RedisBackend(client, "cr:sub:v1")
    chunk.set("same-key", b"chunk-val", max_entries=TEST_MAX_ENTRIES)
    sub.set("same-key", b"sub-val", max_entries=TEST_MAX_ENTRIES)
    assert chunk.get("same-key") == b"chunk-val"
    assert sub.get("same-key") == b"sub-val"


def test_redis_value_ttl_expires(monkeypatch: pytest.MonkeyPatch):
    """Cached values disappear after REDIS_CACHE_TTL_S (FakeRedis clock)."""
    monkeypatch.setenv("REDIS_CACHE_TTL_S", "1")
    client = FakeRedis()
    clock = {"t": 1000.0}
    client._now = lambda: clock["t"]
    cache = RedisBackend(client, "ns")
    cache.set("k", b"v", max_entries=TEST_MAX_ENTRIES)
    assert cache.get("k") == b"v"
    clock["t"] += 2.0  # past TTL
    assert cache.get("k") is None


def test_single_flight_lock_ttl_allows_new_leader(monkeypatch: pytest.MonkeyPatch):
    """An expired foreign lock lets a new leader compute instead of hanging."""
    monkeypatch.setenv("REDIS_LOCK_TTL_S", "1")
    monkeypatch.setenv("REDIS_WAITER_TIMEOUT_S", "0.01")
    client = FakeRedis()
    clock = {"t": 1000.0}
    client._now = lambda: clock["t"]
    cache = RedisBackend(client, "ns")
    # Foreign lock that will expire.
    assert client.set(cache._lock_key("k"), b"1", nx=True, ex=1)
    clock["t"] += 2.0
    calls = {"n": 0}

    def compute() -> Tuple[bytes, bool]:
        calls["n"] += 1
        return b"new-leader", True

    assert cache.single_flight("k", compute, max_entries=TEST_MAX_ENTRIES) == b"new-leader"
    assert calls["n"] == 1


def _live_redis_or_skip():
    """Return a redis-py client when REDIS_URL is reachable; otherwise skip."""
    import os

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
    except ImportError:  # pragma: no cover - redis missing already skipped above
        RedisConnectionError = ConnectionError  # type: ignore[misc,assignment]
        RedisTimeoutError = TimeoutError  # type: ignore[misc,assignment]
    try:
        client.ping()
    except (RedisConnectionError, RedisTimeoutError, OSError) as exc:  # pragma: no cover
        pytest.skip(f"Redis at REDIS_URL unreachable: {exc}")
    return client


@pytest.mark.integration
def test_live_redis_chunk_hit_and_outage_fail_open(monkeypatch: pytest.MonkeyPatch):
    """Optional compose check: real Redis hit + fail-open when connection drops.

    Preconditions:
        - ``REDIS_URL`` points at a live Redis (e.g. compose ``redis://localhost:6379/0``).
    Postconditions:
        - Two ``RedisBackend`` instances sharing that client round-trip a value.
        - When the shared client raises on ``get``, ``RedisBackend.get`` returns
          ``None`` (miss) rather than propagating.
    """
    monkeypatch.setenv("REDIS_CACHE_TTL_S", "30")
    client = _live_redis_or_skip()
    ns = "cr:chunk:v1:live-it"
    writer = RedisBackend(client, ns)
    reader = RedisBackend(client, ns)
    key = "live-integration-key"
    writer.delete(key)
    writer.set(key, b"from-worker-a", max_entries=TEST_MAX_ENTRIES)
    assert reader.get(key) == b"from-worker-a"
    # Live outage: force the shared client to raise. redis-py reconnects after
    # close()/disconnect(), so a hard raise is the reliable fail-open signal.
    def _boom(*_a, **_k):
        raise OSError("simulated redis outage")

    monkeypatch.setattr(client, "get", _boom)
    assert reader.get(key) is None  # fail-open miss, not exception
