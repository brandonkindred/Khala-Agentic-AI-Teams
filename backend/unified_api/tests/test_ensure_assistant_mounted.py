"""Tests for the unified-API first-request lazy-mount hook.

_ensure_assistant_mounted is the "future first-request mount hook" that
_build_assistant_registry's docstring (see test_team_assistant_mount_startup.py)
promised: it idempotently, thread/async-safely constructs and mounts one
team's assistant sub-app from _ASSISTANT_REGISTRY on that team's first
request, reusing it on every later request.
"""

from __future__ import annotations

import asyncio

import pytest
from starlette.routing import Route

import unified_api.main as main


@pytest.fixture(autouse=True)
def _isolate_mount_state():
    """Snapshot/restore all mount-hook state so these tests don't leak into
    each other or into other test modules that import unified_api.main."""
    registry = dict(main._ASSISTANT_REGISTRY)
    mounted = set(main._MOUNTED_ASSISTANTS)
    locks = dict(main._ASSISTANT_MOUNT_LOCKS)
    routes = list(main.app.routes)
    yield
    main._ASSISTANT_REGISTRY.clear()
    main._ASSISTANT_REGISTRY.update(registry)
    main._MOUNTED_ASSISTANTS.clear()
    main._MOUNTED_ASSISTANTS.update(mounted)
    main._ASSISTANT_MOUNT_LOCKS.clear()
    main._ASSISTANT_MOUNT_LOCKS.update(locks)
    main.app.routes[:] = routes


def _seed_spec(team_key: str = "blogging", mount_path: str = "/api/blogging/assistant") -> main.AssistantMountSpec:
    spec = main.AssistantMountSpec(team_key=team_key, mount_path=mount_path, assistant_config=object())
    main._ASSISTANT_REGISTRY[team_key] = spec
    return spec


@pytest.mark.asyncio
async def test_ensure_assistant_mounted_first_call_mounts(monkeypatch: pytest.MonkeyPatch) -> None:
    spec = _seed_spec()
    calls: list[main.AssistantMountSpec] = []

    def fake_mount(app, spec_):
        calls.append(spec_)
        app.routes.append(Route(spec_.mount_path, endpoint=lambda request: None))

    monkeypatch.setattr(main, "mount_assistant_app", fake_mount)

    result = await main._ensure_assistant_mounted("blogging")

    assert result is True
    assert calls == [spec]
    assert "blogging" in main._MOUNTED_ASSISTANTS


@pytest.mark.asyncio
async def test_ensure_assistant_mounted_idempotent_second_call(monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_spec()
    calls: list[str] = []

    def fake_mount(app, spec_):
        calls.append(spec_.team_key)
        app.routes.append(Route(spec_.mount_path, endpoint=lambda request: None))

    monkeypatch.setattr(main, "mount_assistant_app", fake_mount)

    first = await main._ensure_assistant_mounted("blogging")
    second = await main._ensure_assistant_mounted("blogging")

    assert first is True
    assert second is True
    assert calls == ["blogging"]


@pytest.mark.asyncio
async def test_ensure_assistant_mounted_unregistered_team_returns_false(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(main, "mount_assistant_app", lambda app, spec_: calls.append(spec_.team_key))

    result = await main._ensure_assistant_mounted("no_such_team")

    assert result is False
    assert calls == []
    assert "no_such_team" not in main._MOUNTED_ASSISTANTS


@pytest.mark.asyncio
async def test_ensure_assistant_mounted_concurrent_calls_mount_once(monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_spec()
    calls: list[str] = []

    async def fake_ensure_wrapped():
        return await main._ensure_assistant_mounted("blogging")

    def fake_mount(app, spec_):
        calls.append(spec_.team_key)
        app.routes.append(Route(spec_.mount_path, endpoint=lambda request: None))

    monkeypatch.setattr(main, "mount_assistant_app", fake_mount)

    results = await asyncio.gather(*[fake_ensure_wrapped() for _ in range(10)])

    assert results == [True] * 10
    assert calls == ["blogging"]


@pytest.mark.asyncio
async def test_ensure_assistant_mounted_inner_double_check_when_lock_contended(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deterministically exercise the double-check *inside* the lock: a
    second caller that queues behind an in-flight mount must see the
    already-mounted result once the lock is released, without calling
    mount_assistant_app itself. (Plain asyncio.gather doesn't reliably force
    this interleaving because the real critical section has no `await`
    inside it, so the fast outer check alone usually wins the race — this
    test manually holds the lock to make the inner branch deterministic.)
    """
    _seed_spec()
    lock = asyncio.Lock()
    main._ASSISTANT_MOUNT_LOCKS["blogging"] = lock
    calls: list[str] = []
    monkeypatch.setattr(main, "mount_assistant_app", lambda app, spec_: calls.append(spec_.team_key))

    release_holder = asyncio.Event()

    async def holder() -> None:
        async with lock:
            # Held with team_key NOT YET in _MOUNTED_ASSISTANTS, so the waiter's
            # outer fast-path check (before it even reaches the lock) misses too
            # — only the double-check *inside* the lock, after re-acquiring, can
            # observe the mount that happens right before release below.
            await release_holder.wait()
            main._MOUNTED_ASSISTANTS.add("blogging")  # simulates the winner of the race having just mounted

    holder_task = asyncio.create_task(holder())
    await asyncio.sleep(0)  # let holder acquire the lock before the waiter tries

    waiter_task = asyncio.create_task(main._ensure_assistant_mounted("blogging"))
    await asyncio.sleep(0)  # let the waiter pass its outer check and block on the contended lock
    release_holder.set()

    result = await waiter_task
    await holder_task

    assert result is True
    assert calls == []  # mount_assistant_app never called — inner double-check short-circuited


@pytest.mark.asyncio
async def test_ensure_assistant_mounted_failure_is_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_spec()
    attempts = {"count": 0}

    def flaky_mount(app, spec_):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError("boom")
        app.routes.append(Route(spec_.mount_path, endpoint=lambda request: None))

    monkeypatch.setattr(main, "mount_assistant_app", flaky_mount)

    first = await main._ensure_assistant_mounted("blogging")

    assert first is False
    assert "blogging" not in main._MOUNTED_ASSISTANTS

    second = await main._ensure_assistant_mounted("blogging")

    assert second is True
    assert "blogging" in main._MOUNTED_ASSISTANTS
    assert attempts["count"] == 2


@pytest.mark.asyncio
async def test_ensure_assistant_mounted_reorders_route_before_catchall(monkeypatch: pytest.MonkeyPatch) -> None:
    spec = _seed_spec()
    catchall = Route("/api/blogging/{path:path}", endpoint=lambda request: None)
    main.app.routes.insert(0, catchall)

    def fake_mount(app, spec_):
        app.routes.append(Route(spec_.mount_path, endpoint=lambda request: None))

    monkeypatch.setattr(main, "mount_assistant_app", fake_mount)

    await main._ensure_assistant_mounted("blogging")

    mounted_index = next(i for i, r in enumerate(main.app.routes) if getattr(r, "path", None) == spec.mount_path)
    catchall_index = main.app.routes.index(catchall)
    assert mounted_index < catchall_index


def test_match_unmounted_assistant_prefix_boundary() -> None:
    _seed_spec()

    assert main._match_unmounted_assistant_prefix("/api/blogging/assistant") == "blogging"
    assert main._match_unmounted_assistant_prefix("/api/blogging/assistant/health") == "blogging"
    assert main._match_unmounted_assistant_prefix("/api/blogging/assistant-x/foo") is None
    assert main._match_unmounted_assistant_prefix("/api/other") is None


def test_match_unmounted_assistant_prefix_skips_already_mounted() -> None:
    _seed_spec()
    main._MOUNTED_ASSISTANTS.add("blogging")

    assert main._match_unmounted_assistant_prefix("/api/blogging/assistant/health") is None
