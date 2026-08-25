"""Unit tests for the cross-process pip-install lock.

Covers ``pip_install_lock_path`` (pure derivation) and ``pip_install_lock``
(mutual exclusion under concurrency, and graceful degradation when the lock
file can't be opened/flocked).
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

from software_engineering_team import pip_install_lock as pil

# ``shared.concurrency``'s own __init__.py does ``from shared.concurrency.
# flock_lock import flock_lock`` — since the imported name matches the
# submodule's own name, this rebinds the ``flock_lock`` *attribute* on the
# ``shared.concurrency`` package to the function, shadowing the submodule.
# Any attribute-chain resolver (plain ``import x.y as name``, or the dotted
# string form monkeypatch/mock accept) walks that same, possibly-shadowed
# attribute rather than going straight to ``sys.modules`` — so which one it
# resolves to can depend on import order elsewhere in the test session.
# ``sys.modules`` is the one lookup immune to this.
_flock_lock_module = sys.modules["shared.concurrency.flock_lock"]


def test_pip_install_lock_path_is_pure_and_stable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("AGENT_CACHE", str(tmp_path))
    first = pil.pip_install_lock_path()
    second = pil.pip_install_lock_path()
    assert first == second
    assert first == tmp_path / "coding_team" / ".pip_install.lock"
    # Pure: no filesystem access, so nothing was created on disk.
    assert not first.exists()


def test_pip_install_lock_path_not_repo_scoped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The lock guards the shared interpreter, not a per-repo/worktree resource."""
    monkeypatch.setenv("AGENT_CACHE", str(tmp_path))
    assert pil.pip_install_lock_path() == pil.pip_install_lock_path()


def test_pip_install_lock_excludes_concurrent_holders(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
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


def test_pip_install_lock_yields_when_open_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A lock-file open failure degrades to running the block unguarded, not raising.

    The ``open()`` call lives in the shared ``flock_lock`` primitive
    (``shared.concurrency.flock_lock``), not in this module directly, so the
    patch target is there.
    """
    monkeypatch.setenv("AGENT_CACHE", str(tmp_path))

    def _boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(_flock_lock_module, "open", _boom, raising=False)

    ran = False
    with pil.pip_install_lock():
        ran = True
    assert ran


def test_pip_install_lock_yields_when_flock_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A flock failure also degrades to running the block unguarded, not raising.

    The ``fcntl.flock()`` call lives in the shared ``flock_lock`` primitive
    (``shared.concurrency.flock_lock``), not in this module directly, so the
    patch target is there.
    """
    monkeypatch.setenv("AGENT_CACHE", str(tmp_path))

    def _boom(*args, **kwargs):
        raise OSError("lock unavailable")

    monkeypatch.setattr(_flock_lock_module.fcntl, "flock", _boom)

    ran = False
    with pil.pip_install_lock():
        ran = True
    assert ran


def test_pip_install_lock_reraises_body_oserror_without_double_yield(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An OSError from the wrapped block itself must propagate, not be swallowed.

    Regression guard: a naive ``try: with flock_lock(...): yield / except
    OSError`` implementation would also catch an OSError raised by the
    caller's own code inside the ``with`` block (mistaking it for a lock-
    acquisition failure) and then call ``yield`` a second time, which is
    invalid for a generator-based context manager.
    """
    monkeypatch.setenv("AGENT_CACHE", str(tmp_path))

    with pytest.raises(OSError, match="caller failure"):
        with pil.pip_install_lock():
            raise OSError("caller failure")
