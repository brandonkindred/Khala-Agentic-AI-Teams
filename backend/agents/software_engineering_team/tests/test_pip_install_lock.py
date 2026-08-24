"""Unit tests for the cross-process pip-install lock.

Covers ``pip_install_lock_path`` (pure derivation) and ``pip_install_lock``
(mutual exclusion under concurrency, and graceful degradation when the lock
file can't be opened/flocked).
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from software_engineering_team import pip_install_lock as pil


def test_pip_install_lock_path_is_pure_and_stable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("AGENT_CACHE", str(tmp_path))
    first = pil.pip_install_lock_path()
    second = pil.pip_install_lock_path()
    assert first == second
    assert first == tmp_path / "coding_team" / ".pip_install.lock"
    # Pure: no filesystem access, so nothing was created on disk.
    assert not first.exists()


def test_pip_install_lock_path_not_repo_scoped(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """The lock guards the shared interpreter, not a per-repo/worktree resource."""
    monkeypatch.setenv("AGENT_CACHE", str(tmp_path))
    assert pil.pip_install_lock_path() == pil.pip_install_lock_path()


def test_pip_install_lock_excludes_concurrent_holders(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """Two threads racing for the lock never observe overlapping critical sections."""
    monkeypatch.setenv("AGENT_CACHE", str(tmp_path))
    active = 0
    max_active = 0
    guard = threading.Lock()
    barrier = threading.Barrier(2)

    def worker():
        barrier.wait()
        nonlocal active, max_active
        with pil.pip_install_lock():
            with guard:
                active += 1
                max_active = max(max_active, active)
            # Hold the lock briefly so a racing thread has a chance to (wrongly)
            # enter concurrently if mutual exclusion were broken.
            threading.Event().wait(0.05)
            with guard:
                active -= 1

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert max_active == 1


def test_pip_install_lock_yields_when_open_fails(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """A lock-file open failure degrades to running the block unguarded, not raising."""
    monkeypatch.setenv("AGENT_CACHE", str(tmp_path))

    def _boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(pil, "open", _boom, raising=False)

    ran = False
    with pil.pip_install_lock():
        ran = True
    assert ran


def test_pip_install_lock_yields_when_flock_fails(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """A flock failure also degrades to running the block unguarded, not raising."""
    monkeypatch.setenv("AGENT_CACHE", str(tmp_path))

    def _boom(*args, **kwargs):
        raise OSError("lock unavailable")

    monkeypatch.setattr(pil.fcntl, "flock", _boom)

    ran = False
    with pil.pip_install_lock():
        ran = True
    assert ran
