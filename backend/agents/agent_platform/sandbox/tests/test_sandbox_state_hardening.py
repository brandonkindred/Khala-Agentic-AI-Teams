"""Tests for the sandbox state layer.

Covers the thread-safety hardening — once acquire/teardown/reap run on the
Temporal worker thread (while status/note_activity/list_active/metrics stay on
the API loop), ``_state`` and its JSON persistence are touched from two threads,
so these tests exercise the ``state.save`` snapshot + unique-temp-file fix and
the ``Lifecycle`` ``threading.RLock`` — plus the general state helpers: load/save
round-trips, malformed-entry handling, and env-driven path/timeout resolution.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest

from agent_platform.sandbox import lifecycle as lifecycle_mod
from agent_platform.sandbox import state as state_mod
from agent_platform.sandbox.lifecycle import Lifecycle


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


@pytest.mark.asyncio
async def test_persist_asserts_lock_not_already_held(tmp_path: Path) -> None:
    """Enforces the documented precondition: `_persist()` must not be called
    while the caller already holds `_state_lock`. An RLock's reentrancy is
    thread-identity-based, not coroutine-based, so a nested call would let an
    unrelated coroutine resumed on the same OS thread silently reenter the
    lock mid-`await` instead of raising — this assertion is the only thing
    that would catch a future violation."""
    lc = Lifecycle(state_file=tmp_path / "state.json")
    with lc._state_lock:
        with pytest.raises(AssertionError):
            await lc._persist()


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


@pytest.mark.asyncio
async def test_persist_skips_write_when_superseded_before_write_runs(
    tmp_path: Path, monkeypatch
) -> None:
    """``_persist()``'s sequence gate: if a newer call has already snapshotted
    by the time an older call's threaded write runs, the older write must be
    skipped rather than risking a stale overwrite of the newer one.

    Deterministic (no real thread race): ``asyncio.to_thread`` is replaced
    with a fake that captures the write thunk instead of running it, so the
    two thunks can be invoked in a controlled order after both ``_persist()``
    calls have snapshotted and bumped ``_persist_seq``.
    """
    lc = Lifecycle(state_file=tmp_path / "state.json")
    lc._state.update(_seed(2))

    save_calls: list[dict] = []

    def fake_save(path, state) -> None:
        save_calls.append(dict(state))

    monkeypatch.setattr(state_mod, "save", fake_save)

    captured_thunks = []

    async def fake_to_thread(func, *args, **kwargs):
        captured_thunks.append(func)

    monkeypatch.setattr(lifecycle_mod.asyncio, "to_thread", fake_to_thread)

    await lc._persist()  # seq -> 1, thunk captured but not run
    await lc._persist()  # seq -> 2, thunk captured but not run
    assert len(captured_thunks) == 2

    # Run the stale (seq=1) write after the fresh (seq=2) one has already
    # been requested: it must be a no-op.
    captured_thunks[0]()
    assert save_calls == []

    captured_thunks[1]()
    assert len(save_calls) == 1


@pytest.mark.asyncio
async def test_persist_snapshot_immune_to_field_mutation_after_snapshot(
    tmp_path: Path, monkeypatch
) -> None:
    """P2 regression (code review): _persist()'s model_copy() must happen
    INSIDE the with self._state_lock: block that takes the snapshot, not
    later inside state_mod.save() on the unlocked background thread. If the
    copy happened later, a field write from an in-flight acquire()/teardown()
    for the same agent_id (which also takes _state_lock, but only briefly,
    per field-group) could land between the snapshot and the eventual copy,
    producing a torn (partially-old, partially-new) checkpoint.

    Verified by mutating the live SandboxState object AFTER _persist()'s
    snapshot line has run but BEFORE the deferred write executes: the write
    must still reflect the pre-mutation values.
    """
    lc = Lifecycle(state_file=tmp_path / "state.json")
    st = state_mod.new_state(agent_id="a1", team="blogging", container_name="sbx-a1")
    lc._state["a1"] = st

    save_calls: list[dict] = []

    def fake_save(path, state) -> None:
        save_calls.append({agent_id: s.model_dump() for agent_id, s in state.items()})

    monkeypatch.setattr(state_mod, "save", fake_save)

    captured_thunks = []

    async def fake_to_thread(func, *args, **kwargs):
        captured_thunks.append(func)

    monkeypatch.setattr(lifecycle_mod.asyncio, "to_thread", fake_to_thread)

    await lc._persist()  # snapshots st while status=WARMING, container_id=None

    # Simulate an in-flight acquire() continuing to mutate the SAME live
    # object after _persist()'s snapshot was taken but before its deferred
    # write has run.
    st.container_id = "abc123"
    st.status = state_mod.SandboxStatus.WARM

    captured_thunks[0]()  # run the deferred write now

    written = save_calls[0]["a1"]
    assert written["status"] == "warming"
    assert written["container_id"] is None


def test_save_survives_concurrent_resize_during_iteration(tmp_path: Path, monkeypatch) -> None:
    """P3 regression (code review, and a genuine regression from an earlier
    round): save()'s per-item model_copy() comprehension must iterate a
    pre-materialized list(state.items()), never state.items() directly — an
    earlier round accidentally dropped the list() wrapper while adding
    model_copy(), reintroducing 'dictionary changed size during iteration' if
    another thread inserts/pops into the same live dict mid-iteration.

    Simulates exactly that: a concurrent insert happens during the first
    per-item model_copy() call.
    """
    path = tmp_path / "state.json"
    live_state = _seed(3)

    real_model_copy = state_mod.SandboxState.model_copy
    calls = {"n": 0}

    def mutating_model_copy(self, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            live_state["late-comer"] = state_mod.new_state(
                agent_id="late-comer", team="t", container_name="late-comer"
            )
        return real_model_copy(self, *args, **kwargs)

    monkeypatch.setattr(state_mod.SandboxState, "model_copy", mutating_model_copy)

    # Must not raise RuntimeError: dictionary changed size during iteration.
    state_mod.save(path, live_state)

    loaded = state_mod.load(path)
    assert {"agent-0", "agent-1", "agent-2"} <= set(loaded)


# -------------------------------------------------------------------------
# Additional sandbox state coverage: load/save round-trips, malformed-entry
# handling, and env-driven path/timeout resolution.
# -------------------------------------------------------------------------


def test_state_load_missing_file_returns_empty(tmp_path: Path) -> None:
    from agent_platform.sandbox import state as state_mod

    out = state_mod.load(tmp_path / "ghost.json")
    assert out == {}


def test_state_load_corrupt_returns_empty(tmp_path: Path) -> None:
    from agent_platform.sandbox import state as state_mod

    bad = tmp_path / "bad.json"
    bad.write_text("not json", encoding="utf-8")
    assert state_mod.load(bad) == {}


def test_state_load_drops_malformed_entries(tmp_path: Path) -> None:
    import json

    from agent_platform.sandbox import state as state_mod

    f = tmp_path / "s.json"
    f.write_text(
        json.dumps({"a1": {"missing": "fields"}}),  # invalid SandboxState shape
        encoding="utf-8",
    )
    out = state_mod.load(f)
    assert out == {}


def test_state_save_roundtrip(tmp_path: Path) -> None:
    from agent_platform.sandbox.state import (
        SandboxStatus,
        load,
        new_state,
        save,
    )

    state = {
        "a1": new_state(agent_id="a1", team="t", container_name="khala-sbx-a1"),
    }
    state["a1"].status = SandboxStatus.COLD
    f = tmp_path / "s.json"
    save(f, state)
    loaded = load(f)
    assert "a1" in loaded
    assert loaded["a1"].status == SandboxStatus.COLD


def test_boot_timeout_seconds(monkeypatch) -> None:
    from agent_platform.sandbox.state import boot_timeout_seconds

    monkeypatch.delenv("AGENT_PROVISIONING_SANDBOX_BOOT_TIMEOUT_S", raising=False)
    assert boot_timeout_seconds() == 90
    monkeypatch.setenv("AGENT_PROVISIONING_SANDBOX_BOOT_TIMEOUT_S", "30")
    assert boot_timeout_seconds() == 30


def test_sandbox_stack_template_path_override(monkeypatch, tmp_path: Path) -> None:
    from agent_platform.sandbox import state as state_mod

    template = tmp_path / "custom.yml"
    template.write_text("services: {}", encoding="utf-8")
    monkeypatch.setenv("AGENT_PROVISIONING_SANDBOX_STACK_TEMPLATE", str(template))
    assert state_mod.sandbox_stack_template_path() == template
    assert state_mod.sandbox_stack_assets_dir() == template.parent


def test_sandbox_stack_template_path_default(monkeypatch) -> None:
    from agent_platform.sandbox import state as state_mod

    monkeypatch.delenv("AGENT_PROVISIONING_SANDBOX_STACK_TEMPLATE", raising=False)
    p = state_mod.sandbox_stack_template_path()
    # Should resolve to the in-tree path; existence not required for the
    # function itself.
    assert "agent_sandbox_image" in str(p)


def test_state_file_path_with_override(monkeypatch) -> None:
    from agent_platform.sandbox import state as state_mod

    monkeypatch.setenv("AGENT_PROVISIONING_SANDBOX_STATE_FILE", "/tmp/x.json")
    assert str(state_mod.state_file_path()) == "/tmp/x.json"


# -------------------------------------------------------------------------
# General state-layer helpers: image / idle-threshold / state-file resolution.
# -------------------------------------------------------------------------


def test_sandbox_image_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_platform.sandbox import state as state_mod

    monkeypatch.setenv("AGENT_PROVISIONING_SANDBOX_IMAGE", "my/custom:tag")
    assert state_mod.sandbox_image() == "my/custom:tag"
    monkeypatch.delenv("AGENT_PROVISIONING_SANDBOX_IMAGE")
    assert state_mod.sandbox_image() == "khala-agent-sandbox:latest"


def test_idle_threshold_reads_per_agent_env(monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_platform.sandbox import state as state_mod

    monkeypatch.setenv("AGENT_PROVISIONING_SANDBOX_IDLE_MINUTES", "2")
    assert state_mod.idle_teardown_seconds() == 120
    monkeypatch.delenv("AGENT_PROVISIONING_SANDBOX_IDLE_MINUTES")
    assert state_mod.idle_teardown_seconds() == 300  # 5-minute default


def test_state_file_path_uses_agent_cache(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from agent_platform.sandbox import state as state_mod

    monkeypatch.delenv("AGENT_PROVISIONING_SANDBOX_STATE_FILE", raising=False)
    monkeypatch.setenv("AGENT_CACHE", str(tmp_path))
    path = state_mod.state_file_path()
    assert path == tmp_path / "agent_provisioning" / "sandboxes" / "state.json"
