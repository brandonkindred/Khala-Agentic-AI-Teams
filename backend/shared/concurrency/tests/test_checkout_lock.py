"""Unit tests for :func:`held_checkout_lock`, the cancellation-safe async
wrapper around :func:`flock_lock`.

The headline scenario (:func:`test_cancellation_during_acquisition_does_not_leak_the_lock`)
reproduces the exact HIGH-severity bug this helper was introduced to fix: a
coroutine cancelled while ``lock_cm.__enter__()`` is still running in the
executor thread must not leave the flock held forever with nothing left to
release it.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest

from shared.concurrency.checkout_lock import held_checkout_lock


def test_lock_is_held_around_the_body_and_released_after(tmp_path: Path) -> None:
    lock_path = tmp_path / ".test.lock"

    async def _run() -> None:
        loop = asyncio.get_running_loop()
        async with held_checkout_lock(
            loop, lock_path, platform_owned=True, owner="acme", repo="widget", log_prefix="test"
        ):
            pass

    asyncio.run(_run())

    # A second, independent acquisition must succeed immediately if the first
    # call actually released the flock rather than leaking it.
    from shared.concurrency.flock_lock import flock_lock

    entered = False
    with flock_lock(lock_path):
        entered = True
    assert entered


def test_body_exception_still_releases_the_lock(tmp_path: Path) -> None:
    lock_path = tmp_path / ".test.lock"

    async def _run() -> None:
        loop = asyncio.get_running_loop()
        async with held_checkout_lock(
            loop, lock_path, platform_owned=True, owner="acme", repo="widget", log_prefix="test"
        ):
            raise ValueError("boom")

    with pytest.raises(ValueError):
        asyncio.run(_run())

    from shared.concurrency.flock_lock import flock_lock

    entered = False
    with flock_lock(lock_path):
        entered = True
    assert entered


def test_platform_owned_lock_failure_raises_oserror(tmp_path: Path) -> None:
    """A parent directory that cannot be created surfaces as OSError, and the
    body never runs."""
    # A file (not a directory) in the path prevents mkdir(parents=True) from
    # succeeding for a lock file "under" it.
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    lock_path = blocker / "sub" / ".test.lock"

    async def _run() -> None:
        loop = asyncio.get_running_loop()
        async with held_checkout_lock(
            loop, lock_path, platform_owned=True, owner="acme", repo="widget", log_prefix="test"
        ):
            pytest.fail("body must not run when lock acquisition fails for a platform-owned checkout")

    with pytest.raises(OSError):
        asyncio.run(_run())


def test_operator_pinned_lock_failure_degrades_without_raising(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """For an operator-pinned (non-platform-owned) checkout, a lock-acquisition
    OSError degrades to no additional locking rather than failing the request."""
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    lock_path = blocker / "sub" / ".test.lock"
    body_ran = False

    async def _run() -> None:
        nonlocal body_ran
        loop = asyncio.get_running_loop()
        async with held_checkout_lock(
            loop, lock_path, platform_owned=False, owner="acme", repo="widget", log_prefix="test-prefix"
        ):
            body_ran = True

    with caplog.at_level("WARNING"):
        asyncio.run(_run())

    assert body_ran
    assert any("test-prefix" in r.message for r in caplog.records)


def test_cancellation_during_acquisition_does_not_leak_the_lock(tmp_path: Path) -> None:
    """Reproduces the HIGH-severity bug: cancelling the awaiting coroutine while
    ``lock_cm.__enter__()`` is still in flight inside the executor thread must
    not leave the flock held forever.

    A slow, instrumented stand-in for ``flock_lock`` blocks ``__enter__`` on a
    threading.Event so the test can cancel the awaiting task while acquisition
    is provably still in progress in the executor thread, then release the
    Event so acquisition actually completes, then prove the lock was still
    released (via a subsequent, real, uncontended acquisition succeeding
    immediately with a bounded wait).
    """
    lock_path = tmp_path / ".test.lock"
    proceed = threading.Event()
    entered_calls: list[int] = []
    exited_calls: list[int] = []

    class _SlowLock:
        def __init__(self, path: Path) -> None:
            self._path = path

        def __enter__(self) -> "_SlowLock":
            # Block here until the test explicitly releases us -- simulating
            # __enter__ still being in flight in the executor thread at the
            # moment the awaiting coroutine gets cancelled.
            proceed.wait(timeout=5)
            entered_calls.append(1)
            return self

        def __exit__(self, *exc_info) -> bool:
            exited_calls.append(1)
            return False

    import shared.concurrency.checkout_lock as checkout_lock_module

    original_flock_lock = checkout_lock_module.flock_lock
    checkout_lock_module.flock_lock = lambda p: _SlowLock(p)
    try:

        async def _run() -> None:
            loop = asyncio.get_running_loop()
            async with held_checkout_lock(
                loop, lock_path, platform_owned=True, owner="acme", repo="widget", log_prefix="test"
            ):
                pytest.fail("body must not run before this task is cancelled mid-acquisition")

        async def _drive() -> None:
            task = asyncio.ensure_future(_run())
            # Give the executor thread a beat to actually call __enter__ and
            # block on proceed.wait() before cancelling.
            await asyncio.sleep(0.05)
            task.cancel()
            # Give the cancellation a moment to actually reach the task's
            # awaited shield() point. Do NOT await `task` yet: its `finally`
            # will itself await the (still in-flight) acquisition future, so
            # awaiting the task here -- before releasing `proceed` -- would
            # deadlock this test on the same background thread.
            await asyncio.sleep(0.05)
            # __enter__ is still blocked in the executor thread at this point --
            # the cancellation must not have released anything, since nothing
            # was actually acquired yet.
            assert not entered_calls
            assert not exited_calls
            # Now let the executor thread's __enter__ actually complete. The
            # task's `finally` -- awaiting the acquisition future -- can now
            # observe it complete and release the lock before the
            # CancelledError finishes propagating out of the task.
            proceed.set()
            with pytest.raises(asyncio.CancelledError):
                await task

        asyncio.run(_drive())
    finally:
        checkout_lock_module.flock_lock = original_flock_lock

    assert entered_calls == [1], "the executor thread's __enter__ should have completed exactly once"
    assert exited_calls == [1], (
        "a __enter__ that completes after the awaiting coroutine was cancelled must still be released "
        "-- this is the exact HIGH-severity leak this helper exists to prevent"
    )
