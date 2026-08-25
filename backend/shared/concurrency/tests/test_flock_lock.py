"""Unit tests for the shared cross-process :func:`flock_lock` primitive.

Covers mutual exclusion under concurrent threads (the same mechanism
serializes concurrent *processes* too — a thread-level test exercises the
real ``fcntl.flock`` call without the overhead of spawning subprocesses, the
same trade-off ``pip_install_lock``'s own test makes), that acquisition
failures (``open``/``flock`` raising ``OSError``) propagate rather than being
swallowed (callers decide how to handle that — see the module docstring),
and that the lock is always released and the file descriptor always closed,
including when the wrapped block raises.
"""

from __future__ import annotations

import multiprocessing
import sys
import threading
import time
from pathlib import Path

import pytest

from shared.concurrency.flock_lock import flock_lock


def _process_worker(lock_path_str: str, barrier, active, max_active, guard) -> None:
    """Top-level (picklable) worker body for the cross-process test below."""
    lock_path = Path(lock_path_str)
    barrier.wait()
    with flock_lock(lock_path):
        with guard:
            active.value += 1
            max_active.value = max(max_active.value, active.value)
        # Hold the lock briefly so a racing process has a chance to (wrongly)
        # enter concurrently if mutual exclusion were broken.
        time.sleep(0.05)
        with guard:
            active.value -= 1


def test_flock_lock_excludes_concurrent_holders_across_processes(tmp_path: Path) -> None:
    """The actual production scenario: two separate *processes*, not just threads.

    Same-stack backend workers race as separate OS processes under Temporal
    mode, so this is the scenario ``flock_lock`` exists for; the thread-level
    test above exercises the same underlying ``fcntl.flock`` call more
    cheaply, but doesn't prove the lock is visible across process boundaries.
    Uses the "fork" start method (this codebase is Linux-only, same
    assumption ``fcntl`` itself already makes) so the worker doesn't need to
    re-import this test module under "spawn".
    """
    lock_path = tmp_path / ".test.lock"
    ctx = multiprocessing.get_context("fork")
    barrier = ctx.Barrier(2)
    active = ctx.Value("i", 0)
    max_active = ctx.Value("i", 0)
    guard = ctx.Lock()
    procs = [
        ctx.Process(target=_process_worker, args=(str(lock_path), barrier, active, max_active, guard)) for _ in range(2)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=10)
        assert p.exitcode == 0

    assert max_active.value == 1


def test_flock_lock_excludes_concurrent_holders(tmp_path: Path) -> None:
    lock_path = tmp_path / ".test.lock"
    active = 0
    max_active = 0
    guard = threading.Lock()
    barrier = threading.Barrier(2)

    def worker() -> None:
        barrier.wait()
        nonlocal active, max_active
        with flock_lock(lock_path):
            with guard:
                active += 1
                max_active = max(max_active, active)
            threading.Event().wait(0.05)
            with guard:
                active -= 1

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert max_active == 1


def test_flock_lock_releases_and_closes_after_body_raises(tmp_path: Path) -> None:
    """The lock is released (a second acquisition succeeds) even after a body exception."""
    lock_path = tmp_path / ".test.lock"

    with pytest.raises(ValueError):
        with flock_lock(lock_path):
            raise ValueError("boom")

    # A second, independent acquisition must succeed immediately if the first
    # one actually released the lock rather than leaking it.
    entered = False
    with flock_lock(lock_path):
        entered = True
    assert entered


def test_flock_lock_open_failure_raises_oserror(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """An open() failure propagates as OSError rather than being swallowed."""
    missing_dir_path = tmp_path / "no-such-dir" / ".test.lock"

    with pytest.raises(OSError):
        with flock_lock(missing_dir_path):
            pytest.fail("body must not run when acquisition fails")


def test_flock_lock_flock_failure_raises_oserror(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A flock() failure propagates as OSError rather than being swallowed.

    Looked up via ``sys.modules`` rather than ``import shared.concurrency.
    flock_lock as fl`` or a dotted string path: ``shared.concurrency``'s own
    ``__init__.py`` does ``from shared.concurrency.flock_lock import
    flock_lock``, which — since the imported name matches the submodule's
    own name — rebinds the ``flock_lock`` *attribute* on the
    ``shared.concurrency`` package to the function, shadowing the submodule.
    Both plain ``import x.y as name`` and monkeypatch's/mock's dotted-string
    attribute-chain resolvers walk that same (possibly-shadowed) attribute
    rather than going straight to ``sys.modules``, so either can silently
    resolve to the function instead of the module depending on import order
    elsewhere in the test session. ``sys.modules`` is the one lookup immune
    to this: it is always the actual module object.
    """

    def _boom(*args, **kwargs):
        raise OSError("lock unavailable")

    monkeypatch.setattr(sys.modules["shared.concurrency.flock_lock"].fcntl, "flock", _boom)
    lock_path = tmp_path / ".test.lock"

    with pytest.raises(OSError):
        with flock_lock(lock_path):
            pytest.fail("body must not run when acquisition fails")
