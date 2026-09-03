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
import fcntl
import logging
import threading
from pathlib import Path

import pytest

import shared.concurrency.checkout_lock as checkout_lock_module
from shared.concurrency.checkout_lock import held_checkout_lock


class _SlowLock:
    """Instrumented ``flock_lock`` stand-in whose ``__enter__`` blocks on an Event.

    Shared by both cancellation tests: each installs ONE instance as the
    ``flock_lock`` factory's return value, so ``entered_calls``/``exited_calls``
    record that instance's own lifecycle rather than a module-level list two
    copies of this class would have to close over.

    Preconditions:
        - ``proceed`` is the test's own Event; ``__enter__`` blocks until the
          test sets it (bounded by a 5s timeout so a hung test still fails
          rather than wedging the suite), simulating acquisition still in
          flight in the executor thread.
    Postconditions:
        - ``entered_calls``/``exited_calls`` gain one entry per completed
          ``__enter__``/``__exit__``. ``__exit__`` returns False, so it never
          suppresses an exception propagating through the ``with`` body.
    """

    def __init__(self, proceed: threading.Event) -> None:
        self._proceed = proceed
        self.entered_calls: list[int] = []
        self.exited_calls: list[int] = []

    def __enter__(self) -> "_SlowLock":
        self._proceed.wait(timeout=5)
        self.entered_calls.append(1)
        return self

    def __exit__(self, *exc_info) -> bool:
        self.exited_calls.append(1)
        return False


def _install_slow_lock(monkeypatch: pytest.MonkeyPatch, proceed: threading.Event) -> _SlowLock:
    """Patch ``checkout_lock``'s ``flock_lock`` to hand back one shared _SlowLock.

    Postconditions:
        - Returns the instance every ``flock_lock(...)`` call in the module under
          test will return, so a test can assert on its call records. The patch
          is undone by ``monkeypatch``'s own teardown.
    """
    lock = _SlowLock(proceed)
    monkeypatch.setattr(checkout_lock_module, "flock_lock", lambda _path: lock)
    return lock


def _assert_flock_is_held(lock_path: Path) -> None:
    """Assert some OTHER open file description already holds ``lock_path``'s flock.

    ``fcntl.flock`` locks are per OPEN FILE DESCRIPTION, not per process or per
    thread, so a fresh ``open()`` of the same path from this very test yields a
    new description whose non-blocking ``LOCK_EX`` genuinely conflicts with a
    lock held elsewhere in this process. That makes "is the lock held right
    now?" directly observable in-process, rather than only inferable after the
    fact from a later acquisition succeeding.

    Preconditions:
        - ``lock_path`` exists (``flock_lock`` creates it on acquisition).
    Postconditions:
        - Returns None when the non-blocking acquisition is refused with
          ``BlockingIOError`` (i.e. the lock IS held). Fails the test otherwise.
          The probe descriptor is always closed, and no lock is left behind.
    """
    probe = open(lock_path, "w", encoding="utf-8")
    try:
        with pytest.raises(BlockingIOError):
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        probe.close()


def _assert_flock_is_free(lock_path: Path) -> None:
    """Assert NO open file description currently holds ``lock_path``'s flock.

    The non-blocking mirror of :func:`_assert_flock_is_held`, and the reason it
    exists: proving release with a BLOCKING ``with flock_lock(lock_path)`` turns
    a leaked lock into a HUNG test rather than a failing one, wedging the whole
    suite on the very bug these tests exist to catch — the opposite of the
    bounded-wait intent :class:`_SlowLock` documents for itself.

    Preconditions:
        - ``lock_path`` exists (``flock_lock`` creates it on acquisition).
    Postconditions:
        - Returns None when a non-blocking ``LOCK_EX`` is GRANTED (i.e. the lock
          is free), failing the test with a clear message when it is refused
          instead of blocking. The probe's own lock is released and its
          descriptor closed, so this leaves the file exactly as it found it.
    """
    probe = open(lock_path, "w", encoding="utf-8")
    try:
        try:
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:  # pragma: no cover - only on the regression
            pytest.fail(f"flock on {lock_path} is still held; it was leaked, not released")
        fcntl.flock(probe, fcntl.LOCK_UN)
    finally:
        probe.close()


def test_lock_is_held_around_the_body_and_released_after(tmp_path: Path) -> None:
    lock_path = tmp_path / ".test.lock"

    async def _run() -> None:
        loop = asyncio.get_running_loop()
        async with held_checkout_lock(
            loop, lock_path, platform_owned=True, owner="acme", repo="widget", log_prefix="test"
        ):
            # The "held around the body" half of this test's name: a no-op
            # implementation that never actually acquires would sail past the
            # post-run assertion below, so prove the flock is genuinely held
            # WHILE the body runs, from an independent open file description.
            _assert_flock_is_held(lock_path)

    asyncio.run(_run())

    # Probed non-blockingly: a leaked lock must FAIL this test, not hang it.
    _assert_flock_is_free(lock_path)


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

    _assert_flock_is_free(lock_path)


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
    # Pin the LEVEL, not merely presence: caplog.at_level("WARNING") admits
    # ERROR and CRITICAL too, so a bare presence check would also pass if this
    # degraded-but-successful path were logged as an alert-worthy error.
    assert any(
        r.levelno == logging.WARNING and "test-prefix" in r.message for r in caplog.records
    ), "expected a WARNING mentioning the log prefix for the degraded acquisition"


def test_cancellation_during_acquisition_does_not_leak_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reproduces the HIGH-severity bug: cancelling the awaiting coroutine while
    ``lock_cm.__enter__()`` is still in flight inside the executor thread must
    not leave the flock held forever.

    ``_SlowLock`` (the module-level stand-in for ``flock_lock``) blocks
    ``__enter__`` on a threading.Event so the test can cancel the awaiting task
    while acquisition is provably still in progress in the executor thread,
    then release the Event so acquisition actually completes. Release is proven
    by asserting ``exited_calls == [1]`` on the stand-in itself (its
    ``__exit__`` was actually invoked) -- not via a subsequent real
    acquisition.
    """
    lock_path = tmp_path / ".test.lock"
    proceed = threading.Event()
    lock = _install_slow_lock(monkeypatch, proceed)

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
        assert not lock.entered_calls
        assert not lock.exited_calls
        # Now let the executor thread's __enter__ actually complete. The
        # task's `finally` -- awaiting the acquisition future -- can now
        # observe it complete and release the lock before the
        # CancelledError finishes propagating out of the task.
        proceed.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(_drive())

    assert lock.entered_calls == [1], "the executor thread's __enter__ should have completed exactly once"
    assert lock.exited_calls == [1], (
        "a __enter__ that completes after the awaiting coroutine was cancelled must still be released "
        "-- this is the exact HIGH-severity leak this helper exists to prevent"
    )


def test_second_cancellation_during_cleanup_await_does_not_leak_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reproduces the deeper variant of the same bug: a SECOND cancellation
    delivered while the ``finally`` block is itself awaiting the (shielded)
    acquisition future must not abandon that await either.

    Without shielding that second await too, this second cancellation would
    be swallowed by the surrounding ``contextlib.suppress(asyncio.CancelledError)``,
    leaving ``lock_acquired`` False and the task unwinding -- while the
    executor thread goes on to complete ``__enter__`` moments later with no
    code left anywhere to call ``__exit__``.
    """
    lock_path = tmp_path / ".test.lock"
    proceed = threading.Event()
    lock = _install_slow_lock(monkeypatch, proceed)

    async def _run() -> None:
        loop = asyncio.get_running_loop()
        async with held_checkout_lock(
            loop, lock_path, platform_owned=True, owner="acme", repo="widget", log_prefix="test"
        ):
            pytest.fail("body must not run before this task is cancelled mid-acquisition")

    async def _drive() -> None:
        task = asyncio.ensure_future(_run())
        # Let the executor thread actually start __enter__ and block.
        await asyncio.sleep(0.05)
        # First cancellation: reaches the initial (shielded) acquisition
        # await, unwinds into the `finally` block, which starts its own
        # (also shielded) await of the still in-flight acquisition future.
        task.cancel()
        await asyncio.sleep(0.05)
        assert not lock.entered_calls
        assert not lock.exited_calls
        # Second cancellation: delivered while the task is suspended at
        # the `finally` block's cleanup await. Before this fix, this
        # would be caught by `contextlib.suppress(asyncio.CancelledError)`
        # and the task would unwind with `lock_acquired` still False.
        task.cancel()
        await asyncio.sleep(0.05)
        # __enter__ is still blocked -- neither cancellation should have
        # released anything, since nothing was actually acquired yet.
        assert not lock.entered_calls
        assert not lock.exited_calls
        # Now let the executor thread's __enter__ actually complete.
        proceed.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(_drive())

    assert lock.entered_calls == [1], "the executor thread's __enter__ should have completed exactly once"
    assert lock.exited_calls == [1], (
        "a __enter__ that completes after a SECOND cancellation during cleanup must still be released"
    )
