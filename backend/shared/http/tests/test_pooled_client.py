"""Tests for the shared pooled HTTP client."""

from __future__ import annotations

import asyncio
import weakref as _weakref

import httpx
import pytest

import shared.http
from shared.http import (
    _DEFAULT_KEEPALIVE_EXPIRY_S,
    _MIN_KEEPALIVE_EXPIRY_S,
    DEFAULT_LIMITS,
    _keepalive_expiry_seconds,
    aclose_async_pool,
    close_async_pool,
    close_pool,
    get_pooled_async_client,
    get_pooled_client,
)


@pytest.fixture(autouse=True)
def _clean_pool():
    close_pool()
    close_async_pool()
    yield
    close_pool()
    close_async_pool()


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
    """``DEFAULT_LIMITS`` must enable idle-socket recycling (a positive expiry) so
    the client drops a socket before an upstream closes it — avoiding
    ``RemoteProtocolError`` on reuse of a server-closed connection.

    Only the *behaviour* (recycling is enabled) is asserted here. ``DEFAULT_LIMITS``
    is built once at import from ``_keepalive_expiry_seconds()``, so pinning the exact
    value would couple this to import-time env state; the default value and env
    parsing are covered directly by the ``_keepalive_expiry_seconds`` tests below."""
    assert DEFAULT_LIMITS.keepalive_expiry is not None
    assert DEFAULT_LIMITS.keepalive_expiry > 0


def test_pooled_client_applies_limits_and_timeout():
    client = get_pooled_client(12.0)
    assert isinstance(client, httpx.Client)
    assert client.timeout.read == 12.0
    # The public surface we can always assert: the limits object the pool is
    # built from carries the configured caps. ``keepalive_expiry`` is only
    # checked for being a positive expiry (recycling enabled) — its exact value
    # depends on import-time ``HTTP_KEEPALIVE_EXPIRY_S`` and is covered by the
    # ``_keepalive_expiry_seconds`` tests, so pinning it here would be flaky.
    assert DEFAULT_LIMITS.max_connections == 50
    assert DEFAULT_LIMITS.max_keepalive_connections == 20
    assert DEFAULT_LIMITS.keepalive_expiry > 0
    # Stronger check: confirm DEFAULT_LIMITS is actually wired into the live
    # pool. httpx exposes no public accessor for this, so it requires reaching
    # into httpcore internals (``_transport._pool``). Guarded so a future httpx
    # that renames these private attributes degrades to the public assertions
    # above rather than hard-failing on an internals change.
    pool = getattr(getattr(client, "_transport", None), "_pool", None)  # noqa: SLF001
    if pool is not None and hasattr(pool, "_max_connections"):
        assert pool._max_connections == DEFAULT_LIMITS.max_connections  # noqa: SLF001
        assert pool._max_keepalive_connections == DEFAULT_LIMITS.max_keepalive_connections  # noqa: SLF001
        assert pool._keepalive_expiry == DEFAULT_LIMITS.keepalive_expiry  # noqa: SLF001
    else:  # pragma: no cover - only taken if a future httpx renames pool internals
        pytest.skip("httpx pool internals unavailable; public limits asserted above")


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


def test_keepalive_expiry_clamps_below_floor(monkeypatch):
    """A positive value below the 1.0s floor is clamped up (not discarded) — an
    extremely short expiry would recycle sockets almost immediately, defeating
    the pool."""
    monkeypatch.setenv("HTTP_KEEPALIVE_EXPIRY_S", "1e-10")
    assert _keepalive_expiry_seconds() == _MIN_KEEPALIVE_EXPIRY_S == 1.0


def test_keepalive_expiry_at_floor_is_kept(monkeypatch):
    monkeypatch.setenv("HTTP_KEEPALIVE_EXPIRY_S", "1.0")
    assert _keepalive_expiry_seconds() == _MIN_KEEPALIVE_EXPIRY_S


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
    assert shared.http._clients == {}  # noqa: SLF001 — verify teardown cleared state


def test_async_requires_running_event_loop():
    with pytest.raises(AssertionError, match="running event loop"):
        get_pooled_async_client(30.0)


def test_async_same_timeout_returns_same_instance():
    async def _run():
        a = get_pooled_async_client(30.0)
        b = get_pooled_async_client(30.0)
        assert a is b
        assert isinstance(a, httpx.AsyncClient)

    asyncio.run(_run())


def test_async_close_terminates_and_replaces_on_next_get():
    async def _grab_and_aclose():
        a = get_pooled_async_client(30.0)
        await aclose_async_pool()
        assert a.is_closed
        return a

    a = asyncio.run(_grab_and_aclose())

    async def _second():
        b = get_pooled_async_client(30.0)
        assert b is not a
        assert not b.is_closed

    asyncio.run(_second())


def test_async_different_timeout_buckets_are_isolated():
    async def _run():
        fast = get_pooled_async_client(5.0)
        slow = get_pooled_async_client(30.0)
        assert fast is not slow

    asyncio.run(_run())


def test_async_near_equal_timeouts_share_bucket():
    async def _run():
        a = get_pooled_async_client(30.0)
        b = get_pooled_async_client(30.0001)
        assert a is b

    asyncio.run(_run())


def test_async_replaces_externally_closed_client():
    async def _run():
        a = get_pooled_async_client(7.0)
        await a.aclose()
        b = get_pooled_async_client(7.0)
        assert b is not a
        assert not b.is_closed

    asyncio.run(_run())


def test_async_close_pool_is_idempotent():
    async def _grab_and_aclose():
        client = get_pooled_async_client(9.0)
        await aclose_async_pool()
        return client

    asyncio.run(_grab_and_aclose())
    close_async_pool()
    close_async_pool()

    async def _grab():
        return get_pooled_async_client(9.0)

    assert asyncio.run(_grab()) is not None


def test_async_invalid_timeout_rejected():
    async def _run():
        with pytest.raises(AssertionError):
            get_pooled_async_client(0)
        with pytest.raises(AssertionError):
            get_pooled_async_client(-5)

    asyncio.run(_run())


def test_async_non_finite_timeout_rejected():
    async def _run():
        with pytest.raises(AssertionError):
            get_pooled_async_client(float("inf"))
        with pytest.raises(AssertionError):
            get_pooled_async_client(float("nan"))

    asyncio.run(_run())


def test_async_close_pool_swallows_client_close_errors(monkeypatch):
    async def _boom(_client: httpx.AsyncClient) -> None:
        raise RuntimeError("close failed")

    monkeypatch.setattr(shared.http, "_aclose_quietly", _boom)

    async def _body():
        client = get_pooled_async_client(15.0)
        await aclose_async_pool()  # must not raise
        # Client could not be closed; remains tracked (not silently orphaned).
        assert any(
            entry.client is client
            for entry in shared.http._async_clients.values()  # noqa: SLF001
        )
        assert not client.is_closed

    asyncio.run(_body())


@pytest.mark.asyncio
async def test_async_aclose_pool_under_running_loop_awaits_without_asyncio_run():
    """Awaited teardown must close on the owning loop, never via asyncio.run."""
    client = get_pooled_async_client(15.0)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            shared.http.asyncio,
            "run",
            lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("must not asyncio.run")),
        )
        await aclose_async_pool()
    assert client.is_closed
    assert client not in [e.client for e in shared.http._async_clients.values()]  # noqa: SLF001


def test_async_close_pool_closes_even_when_caller_has_running_loop():
    """Sync close under a live owning loop schedules aclose (no foreign asyncio.run)."""

    async def _body():
        client = get_pooled_async_client(15.0)
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                shared.http.asyncio,
                "run",
                lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("must not asyncio.run")),
            )
            close_async_pool()
        # Give the scheduled aclose task a turn on this loop.
        await asyncio.sleep(0)
        assert client.is_closed
        assert client not in [
            e.client
            for e in shared.http._async_clients.values()  # noqa: SLF001
        ]

    asyncio.run(_body())


def test_async_aclose_pool_keeps_stopped_owner_loop_client_without_hanging():
    """A client owned by a stopped-but-not-closed loop must not hang teardown and
    must not be dropped while still open — it stays tracked so the sync path
    (which has no running loop) can close it on that loop."""
    owner_loop = asyncio.new_event_loop()
    try:
        client = owner_loop.run_until_complete(_grab_async_client(15.0))
        # owner_loop is now stopped but NOT closed; its client is still pooled.
        assert not client.is_closed

        async def _teardown_from_other_loop():
            # Must return promptly rather than queueing on the inactive loop.
            await asyncio.wait_for(aclose_async_pool(), timeout=5.0)

        asyncio.run(_teardown_from_other_loop())
        # Still open, so still tracked — dropping it here would leak the socket.
        assert not client.is_closed
        assert any(
            entry.client is client
            for entry in shared.http._async_clients.values()  # noqa: SLF001
        )

        # The sync path has no running loop, so it can drive the owner loop.
        close_async_pool()
        assert client.is_closed
        assert shared.http._async_clients == {}  # noqa: SLF001
    finally:
        owner_loop.close()


async def _grab_async_client(timeout: float) -> httpx.AsyncClient:
    return get_pooled_async_client(timeout)


def test_async_clients_isolated_across_sequential_event_loops():
    """Sequential asyncio.run calls must get distinct clients, and the prior
    (dead-loop) entry must be dropped on the next access without a foreign-loop
    aclose — never reused even if httpx still reports it open."""

    async def _grab() -> httpx.AsyncClient:
        return get_pooled_async_client(30.0)

    first = asyncio.run(_grab())
    second = asyncio.run(_grab())
    assert first is not second
    assert first not in [e.client for e in shared.http._async_clients.values()]  # noqa: SLF001


def test_async_pool_does_not_accumulate_across_many_asyncio_runs():
    """Repeated asyncio.run must not leak one pool entry per invocation."""

    async def _grab() -> httpx.AsyncClient:
        return get_pooled_async_client(30.0)

    for _ in range(8):
        asyncio.run(_grab())

    async def _count_after_purge() -> int:
        get_pooled_async_client(30.0)
        return len([k for k in shared.http._async_clients if k[0] == 30.0])  # noqa: SLF001

    assert asyncio.run(_count_after_purge()) == 1


def test_async_same_loop_reuses_client():
    async def _twice() -> tuple[httpx.AsyncClient, httpx.AsyncClient]:
        return get_pooled_async_client(30.0), get_pooled_async_client(30.0)

    a, b = asyncio.run(_twice())
    assert a is b


def test_async_pooled_client_applies_limits_and_timeout():
    async def _run():
        client = get_pooled_async_client(12.0)
        assert isinstance(client, httpx.AsyncClient)
        assert client.timeout.read == 12.0
        pool = getattr(getattr(client, "_transport", None), "_pool", None)  # noqa: SLF001
        if pool is not None and hasattr(pool, "_max_connections"):
            assert pool._max_connections == DEFAULT_LIMITS.max_connections  # noqa: SLF001
            assert pool._max_keepalive_connections == DEFAULT_LIMITS.max_keepalive_connections  # noqa: SLF001
            assert pool._keepalive_expiry == DEFAULT_LIMITS.keepalive_expiry  # noqa: SLF001
        else:  # pragma: no cover
            pytest.skip("httpx pool internals unavailable; public timeout asserted above")

    asyncio.run(_run())


def test_async_finalizers_do_not_accumulate_across_create_close_cycles():
    """Repeated get/aclose cycles on one loop must not stack weakref finalizers
    for the same pool key."""
    import weakref as _weakref

    async def _cycles():
        before = len(_weakref.finalize._registry)
        for _ in range(50):
            get_pooled_async_client(30.0)
            await aclose_async_pool()
        # At most one live finalizer per pooled entry (the pool is empty here).
        assert len(_weakref.finalize._registry) - before <= 1

    asyncio.run(_cycles())


def test_async_aclose_pool_closes_client_repooled_during_teardown():
    """A client created by a concurrent caller while aclose_async_pool awaits
    must not be left open and untracked."""
    repooled: list[httpx.AsyncClient] = []
    original = shared.http._aclose_quietly  # noqa: SLF001

    async def _aclose_and_repool(client):
        await original(client)
        # Simulate a racing get_pooled_async_client during the teardown await.
        if not repooled:
            repooled.append(get_pooled_async_client(30.0))

    async def _run():
        get_pooled_async_client(30.0)
        shared.http._aclose_quietly = _aclose_and_repool  # noqa: SLF001
        try:
            await aclose_async_pool()
        finally:
            shared.http._aclose_quietly = original  # noqa: SLF001
        assert repooled, "test did not exercise the repool race"
        assert repooled[0].is_closed
        assert shared.http._async_clients == {}  # noqa: SLF001

    asyncio.run(_run())


def test_async_aclose_pool_bounded_when_owner_loop_wedged(monkeypatch):
    """A foreign owning loop that never runs the submitted close must not hang
    teardown; the wait is bounded and the entry stays tracked."""
    import concurrent.futures

    owner_loop = asyncio.new_event_loop()
    try:
        client = owner_loop.run_until_complete(_grab_async_client(30.0))

        # Pretend the owner loop is running but wedged: the submitted close
        # future never completes.
        monkeypatch.setattr(owner_loop, "is_running", lambda: True)
        never = concurrent.futures.Future()
        monkeypatch.setattr(asyncio, "run_coroutine_threadsafe", lambda coro, loop: (coro.close(), never)[1])
        monkeypatch.setattr(shared.http, "_CROSS_LOOP_CLOSE_TIMEOUT_S", 0.05)

        async def _teardown():
            await asyncio.wait_for(aclose_async_pool(), timeout=5.0)

        asyncio.run(_teardown())
        assert not client.is_closed
        assert any(
            entry.client is client
            for entry in shared.http._async_clients.values()  # noqa: SLF001
        )
    finally:
        # Undo the is_running/run_coroutine_threadsafe patches before driving
        # the real loop for cleanup.
        monkeypatch.undo()
        shared.http._async_clients.clear()  # noqa: SLF001
        owner_loop.run_until_complete(client.aclose())
        owner_loop.close()


# --- Async pool internals: race, restore, and best-effort teardown branches ---


def _entry_for(client, loop):
    """Build a pool entry mirroring what ``get_pooled_async_client`` stores."""
    return shared.http._AsyncPoolEntry(client=client, loop_ref=_weakref.ref(loop))  # noqa: SLF001


def test_take_async_pool_entry_loses_race_to_a_replacement():
    """A caller must not close an entry another caller already replaced."""
    loop = asyncio.new_event_loop()
    try:
        key = (30.0, id(loop))
        mine = _entry_for(httpx.AsyncClient(), loop)
        theirs = _entry_for(httpx.AsyncClient(), loop)
        shared.http._async_clients[key] = theirs  # noqa: SLF001
        assert shared.http._take_async_pool_entry(key, mine) is False  # noqa: SLF001
        assert shared.http._async_clients[key] is theirs  # noqa: SLF001
    finally:
        shared.http._async_clients.clear()  # noqa: SLF001
        loop.close()


def test_restore_async_pool_entry_yields_to_a_newer_entry():
    """A failed teardown must not clobber a client pooled while it was closing."""
    loop = asyncio.new_event_loop()
    try:
        key = (30.0, id(loop))
        newer = _entry_for(httpx.AsyncClient(), loop)
        shared.http._async_clients[key] = newer  # noqa: SLF001
        shared.http._restore_async_pool_entry(key, _entry_for(httpx.AsyncClient(), loop))  # noqa: SLF001
        assert shared.http._async_clients[key] is newer  # noqa: SLF001
    finally:
        shared.http._async_clients.clear()  # noqa: SLF001
        loop.close()


def test_restore_async_pool_entry_reregisters_finalizer():
    """A restored entry gets a fresh finalizer so loop collection still evicts it."""
    loop = asyncio.new_event_loop()
    try:
        key = (30.0, id(loop))
        entry = _entry_for(httpx.AsyncClient(), loop)
        shared.http._restore_async_pool_entry(key, entry)  # noqa: SLF001
        assert shared.http._async_clients[key] is entry  # noqa: SLF001
        assert entry.finalizer is not None and entry.finalizer.alive
    finally:
        shared.http._async_clients.clear()  # noqa: SLF001
        loop.close()


def test_close_client_on_owning_loop_closes_via_other_running_loop():
    """A client owned by another *running* loop is closed on that loop."""
    import threading

    owner_loop = asyncio.new_event_loop()
    ready = threading.Event()

    def _drive():
        asyncio.set_event_loop(owner_loop)
        owner_loop.call_soon(ready.set)
        owner_loop.run_forever()

    thread = threading.Thread(target=_drive, daemon=True)
    thread.start()
    try:
        ready.wait(timeout=5.0)
        client = asyncio.run_coroutine_threadsafe(_make_client(), owner_loop).result(timeout=5.0)
        entry = _entry_for(client, owner_loop)
        assert shared.http._close_client_on_owning_loop(entry) is True  # noqa: SLF001
        assert client.is_closed
    finally:
        owner_loop.call_soon_threadsafe(owner_loop.stop)
        thread.join(timeout=5.0)
        owner_loop.close()


async def _make_client() -> httpx.AsyncClient:
    return httpx.AsyncClient()


def test_close_client_on_owning_loop_swallows_submission_failure(monkeypatch):
    """A failure submitting the close must be logged, not raised."""
    owner_loop = asyncio.new_event_loop()
    try:
        client = owner_loop.run_until_complete(_make_client())
        monkeypatch.setattr(owner_loop, "is_running", lambda: True)
        monkeypatch.setattr(
            asyncio,
            "run_coroutine_threadsafe",
            lambda coro, loop: (coro.close(), _raise(RuntimeError("submit failed")))[1],
        )
        assert shared.http._close_client_on_owning_loop(_entry_for(client, owner_loop)) is False  # noqa: SLF001
        assert not client.is_closed
    finally:
        monkeypatch.undo()
        owner_loop.run_until_complete(client.aclose())
        owner_loop.close()


def _raise(exc):
    raise exc


def test_close_client_on_stopped_loop_swallows_run_failure(monkeypatch):
    """A stopped-loop close that blows up is reported as 'not closed', not raised."""
    owner_loop = asyncio.new_event_loop()
    try:
        client = owner_loop.run_until_complete(_make_client())
        monkeypatch.setattr(owner_loop, "run_until_complete", lambda coro: _raise(RuntimeError("boom")))
        assert shared.http._close_client_on_stopped_loop(client, owner_loop) is False  # noqa: SLF001
    finally:
        monkeypatch.undo()
        owner_loop.run_until_complete(client.aclose())
        owner_loop.close()


def test_aclose_quietly_swallows_client_error():
    """``_aclose_quietly`` is best-effort: a failing aclose must not propagate."""

    class _Boom:
        is_closed = False

        async def aclose(self):
            raise RuntimeError("aclose failed")

    asyncio.run(shared.http._aclose_quietly(_Boom()))  # noqa: SLF001 — must not raise


def test_aclose_quietly_is_a_noop_for_a_closed_client():
    class _Closed:
        is_closed = True

        async def aclose(self):  # pragma: no cover - must not be reached
            raise AssertionError("must not aclose an already-closed client")

    asyncio.run(shared.http._aclose_quietly(_Closed()))  # noqa: SLF001


def test_dispose_async_pool_entry_swallows_unexpected_error(monkeypatch):
    """Dispose is best-effort; an unexpected error falls back to is_closed."""
    loop = asyncio.new_event_loop()
    try:
        client = loop.run_until_complete(_make_client())
        monkeypatch.setattr(
            shared.http,
            "_close_client_on_owning_loop",
            lambda entry: _raise(RuntimeError("dispose blew up")),
        )
        assert shared.http._dispose_async_pool_entry(_entry_for(client, loop)) is False  # noqa: SLF001
    finally:
        monkeypatch.undo()
        loop.run_until_complete(client.aclose())
        loop.close()


def test_evict_async_pool_key_is_a_noop_for_a_missing_key():
    shared.http._evict_async_pool_key((30.0, 12345))  # noqa: SLF001 — must not raise


def test_evict_async_pool_key_restores_an_unclosable_entry(monkeypatch):
    """An entry that cannot be closed yet stays pooled rather than being lost."""
    loop = asyncio.new_event_loop()
    try:
        client = loop.run_until_complete(_make_client())
        key = (30.0, id(loop))
        entry = _entry_for(client, loop)
        shared.http._async_clients[key] = entry  # noqa: SLF001
        monkeypatch.setattr(shared.http, "_dispose_async_pool_entry", lambda e: False)
        shared.http._evict_async_pool_key(key)  # noqa: SLF001
        assert shared.http._async_clients[key] is entry  # noqa: SLF001
    finally:
        monkeypatch.undo()
        shared.http._async_clients.clear()  # noqa: SLF001
        loop.run_until_complete(client.aclose())
        loop.close()


def test_purge_stale_async_clients_restores_an_unclosable_entry(monkeypatch):
    """A dead-loop entry that dispose refuses to drop stays tracked."""
    loop = asyncio.new_event_loop()
    client = loop.run_until_complete(_make_client())
    key = (30.0, id(loop))
    entry = _entry_for(client, loop)
    loop.run_until_complete(client.aclose())
    loop.close()
    shared.http._async_clients[key] = entry  # noqa: SLF001
    try:
        monkeypatch.setattr(shared.http, "_dispose_async_pool_entry", lambda e: False)
        shared.http._purge_stale_async_clients()  # noqa: SLF001
        assert shared.http._async_clients[key] is entry  # noqa: SLF001
    finally:
        monkeypatch.undo()
        shared.http._async_clients.clear()  # noqa: SLF001


def test_close_async_pool_skips_an_entry_replaced_concurrently():
    """close_async_pool must not close a client a racing caller re-pooled."""
    loop = asyncio.new_event_loop()
    try:
        client = loop.run_until_complete(_make_client())
        key = (30.0, id(loop))
        stale = _entry_for(client, loop)
        # Snapshot sees `stale`, but the pool already holds a different entry.
        with_other = _entry_for(httpx.AsyncClient(), loop)
        shared.http._async_clients[key] = stale  # noqa: SLF001
        snapshot_taken = []

        real_take = shared.http._take_async_pool_entry  # noqa: SLF001

        def _swap_then_take(k, e):
            if not snapshot_taken:
                snapshot_taken.append(True)
                shared.http._async_clients[k] = with_other  # noqa: SLF001
            return real_take(k, e)

        shared.http._take_async_pool_entry = _swap_then_take  # noqa: SLF001
        try:
            close_async_pool()
        finally:
            shared.http._take_async_pool_entry = real_take  # noqa: SLF001
        assert not client.is_closed
    finally:
        shared.http._async_clients.clear()  # noqa: SLF001
        loop.run_until_complete(client.aclose())
        loop.close()
