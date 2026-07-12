"""Tests for the sandbox state-layer thread-safety hardening.

Once acquire/teardown/reap run on the Temporal worker thread (while status/
note_activity/list_active/metrics stay on the API loop), ``_state`` and its JSON
persistence are touched from two threads. These tests cover the ``state.save``
snapshot + unique-temp-file fix and the ``Lifecycle`` ``threading.RLock``.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

from agent_provisioning_team.sandbox import state as state_mod
from agent_provisioning_team.sandbox.lifecycle import Lifecycle


def _seed(n: int) -> dict:
    return {
        f"agent-{i}": state_mod.new_state(
            agent_id=f"agent-{i}", team="blogging", container_name=f"sbx-{i}"
        )
        for i in range(n)
    }


def test_save_roundtrips_and_leaves_no_tmp(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    seeded = _seed(5)
    state_mod.save(path, seeded)

    loaded = state_mod.load(path)
    assert set(loaded) == set(seeded)
    # No temp files left behind.
    assert list(tmp_path.glob("*.tmp")) == []


def test_save_uses_unique_tmp_per_call(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "state.json"
    seen: list[str] = []

    real_replace = state_mod.os.replace

    def spy_replace(src, dst):
        seen.append(str(src))
        return real_replace(src, dst)

    monkeypatch.setattr(state_mod.os, "replace", spy_replace)

    state_mod.save(path, _seed(2))
    state_mod.save(path, _seed(2))

    # Two calls → two distinct temp paths (pid + random suffix), both under `path`.
    assert len(seen) == 2
    assert seen[0] != seen[1]
    assert str(os_pid := state_mod.os.getpid()) in seen[0]
    assert str(os_pid) in seen[1]


def test_lifecycle_has_state_lock(tmp_path: Path) -> None:
    lc = Lifecycle(state_file=tmp_path / "state.json")
    # Usable as a re-entrant context manager (RLock).
    with lc._state_lock:
        with lc._state_lock:
            pass


def test_persist_and_mutation_are_thread_safe(tmp_path: Path) -> None:
    """A writer thread persisting while a mutator thread inserts/pops must not
    raise ``RuntimeError: dictionary changed size during iteration`` or corrupt
    the file. Regression guard for the two-thread state access the Temporal
    migration introduces.

    ``_persist()`` is ``async`` (it offloads the actual disk write via
    ``asyncio.to_thread``), so the writer thread needs its own running event
    loop to drive it — mirroring how it's really invoked (from within an async
    ``Lifecycle`` method on either the API loop or the Temporal worker loop).
    """
    lc = Lifecycle(state_file=tmp_path / "state.json")
    lc._state.update(_seed(40))

    errors: list[BaseException] = []
    stop = threading.Event()

    def writer() -> None:
        async def _run() -> None:
            for _ in range(400):
                await lc._persist()

        try:
            asyncio.run(_run())
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            stop.set()

    def mutator() -> None:
        try:
            i = 0
            while not stop.is_set():
                key = f"transient-{i % 25}"
                with lc._state_lock:
                    lc._state[key] = state_mod.new_state(agent_id=key, team="t", container_name=key)
                    lc._state.pop(key, None)
                i += 1
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=writer), threading.Thread(target=mutator)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert errors == []
    # File is still valid JSON that loads back to the seeded agents.
    loaded = state_mod.load(tmp_path / "state.json")
    assert all(f"agent-{i}" in loaded for i in range(40))


def test_boot_ms_samples_append_and_read_are_thread_safe(tmp_path: Path) -> None:
    """A writer thread appending to ``_boot_ms_samples`` (what ``acquire()``
    does after a successful cold start) concurrently with a reader thread doing
    ``list(self._boot_ms_samples)`` (what ``metrics()`` does) must not raise
    ``RuntimeError: deque mutated during iteration``. Both sides go through
    ``_state_lock`` now, closing the gap where these ran unlocked."""
    lc = Lifecycle(state_file=tmp_path / "state.json")

    errors: list[BaseException] = []
    stop = threading.Event()

    def appender() -> None:
        try:
            for i in range(5000):
                with lc._state_lock:
                    lc._boot_ms_samples.append(i)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            stop.set()

    def reader() -> None:
        try:
            while not stop.is_set():
                with lc._state_lock:
                    list(lc._boot_ms_samples)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=appender), threading.Thread(target=reader)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert errors == []
