"""Unit tests for the shared :class:`KeyedLockManager` per-key lock registry.

Covers the two acceptance-criteria scenarios directly — same-key writers
serialize to a deterministic, non-corrupted result; disjoint-key writers run
genuinely concurrently, not serialized — plus the multi-key batch
deadlock-avoidance strategy, reentrancy detection, and release-on-exception.

Every test that exercises real contention forces the interleaving with
``threading.Event``/``threading.Barrier`` and bounded ``.wait()``/``.join()``
calls rather than sleeps, and spawns daemon threads, so a regression that
reintroduces a deadlock fails the specific assertion instead of hanging the
whole test process.
"""

from __future__ import annotations

import threading
import time

import pytest

from shared.concurrency.keyed_lock_manager import KeyedLockManager


def _run_daemon(target, *, args=(), kwargs=None) -> threading.Thread:
    """Start ``target`` on a daemon thread so a hung worker can't block CI."""
    thread = threading.Thread(target=target, args=args, kwargs=kwargs or {}, daemon=True)
    thread.start()
    return thread


def test_disjoint_keys_run_concurrently() -> None:
    """Two lock() calls on disjoint keys never block each other."""
    locks: KeyedLockManager[str] = KeyedLockManager()
    a_entered = threading.Event()
    b_entered = threading.Event()
    errors: list[Exception] = []

    def worker(key: str, own_event: threading.Event, other_event: threading.Event) -> None:
        try:
            with locks.lock([key]):
                own_event.set()
                # If disjoint keys were wrongly serialized, the other worker could
                # never enter while this one is still inside its critical section,
                # and this wait would time out.
                assert other_event.wait(timeout=5), f"the other worker never entered while {key!r} was held"
        except Exception as exc:  # noqa: BLE001 - collected and re-raised on the main thread
            errors.append(exc)

    t_a = _run_daemon(worker, args=("a", a_entered, b_entered))
    t_b = _run_daemon(worker, args=("b", b_entered, a_entered))
    t_a.join(timeout=5)
    t_b.join(timeout=5)

    assert not t_a.is_alive() and not t_b.is_alive(), "disjoint-key workers did not both finish"
    assert not errors, errors


def test_same_key_serializes_and_no_torn_write() -> None:
    """Two 'microtasks' writing the same path serialize; the result is not torn or dropped.

    Mirrors ``_execute_coding_phase``'s write-then-``all_files.update()`` shape
    directly: each writer holds the lock across both the disk write and the
    accumulator update, so the two can never be observed out of sync.
    """
    locks: KeyedLockManager[str] = KeyedLockManager()
    disk: dict[str, str] = {}
    all_files: dict[str, str] = {}
    active = 0
    active_lock = threading.Lock()
    observed_concurrent = False
    microtask_1_acquired = threading.Event()
    microtask_1_may_finish = threading.Event()
    errors: list[Exception] = []

    def microtask_write(content: str, *, acquired: threading.Event = None, wait_for: threading.Event = None) -> None:
        nonlocal active, observed_concurrent
        try:
            with locks.lock(["shared.py"]):
                with active_lock:
                    active += 1
                    if active > 1:
                        observed_concurrent = True
                if acquired is not None:
                    acquired.set()
                if wait_for is not None:
                    assert wait_for.wait(timeout=5), "gate was never opened"
                disk["shared.py"] = content
                all_files["shared.py"] = content
                with active_lock:
                    active -= 1
        except Exception as exc:  # noqa: BLE001 - collected and re-raised on the main thread
            errors.append(exc)

    t1 = _run_daemon(
        microtask_write,
        args=("from-microtask-1",),
        kwargs={"acquired": microtask_1_acquired, "wait_for": microtask_1_may_finish},
    )
    assert microtask_1_acquired.wait(timeout=5), "microtask 1 never acquired the lock"

    t2 = _run_daemon(microtask_write, args=("from-microtask-2",))
    time.sleep(0.1)  # give microtask 2 a chance to attempt (and block behind) the same key
    microtask_1_may_finish.set()  # let microtask 1 finish its write + update and release

    t1.join(timeout=5)
    t2.join(timeout=5)

    assert not t1.is_alive() and not t2.is_alive()
    assert not errors, errors
    assert not observed_concurrent, "both microtasks were inside the critical section at once"
    # Whichever microtask actually acquired second is authoritative in BOTH
    # places — disk and all_files never diverge (no split-brain, no torn write).
    assert disk["shared.py"] == all_files["shared.py"] == "from-microtask-2"


def test_batch_acquisition_avoids_deadlock_with_reversed_key_order() -> None:
    """Two callers locking ["a","b"] vs ["b","a"] concurrently, repeatedly, never deadlock."""
    locks: KeyedLockManager[str] = KeyedLockManager()
    errors: list[Exception] = []
    iterations = 200

    def loop(keys: list[str]) -> None:
        try:
            for _ in range(iterations):
                with locks.lock(keys):
                    pass
        except Exception as exc:  # noqa: BLE001 - collected and re-raised on the main thread
            errors.append(exc)

    t_ab = _run_daemon(loop, args=(["a", "b"],))
    t_ba = _run_daemon(loop, args=(["b", "a"],))
    t_ab.join(timeout=10)
    t_ba.join(timeout=10)

    assert not t_ab.is_alive() and not t_ba.is_alive(), "reversed-order batch acquisition deadlocked"
    assert not errors, errors


def test_empty_key_batch_is_a_noop() -> None:
    """lock([]) acquires nothing and still runs the with-block's body."""
    locks: KeyedLockManager[str] = KeyedLockManager()
    entered = False
    with locks.lock([]):
        entered = True
    assert entered
    assert locks._order == {}  # nothing was registered by an empty batch


def test_duplicate_keys_in_one_batch_do_not_self_deadlock() -> None:
    """A batch with a repeated key acquires that key's lock exactly once."""
    locks: KeyedLockManager[str] = KeyedLockManager()
    result: list[str] = []

    def run() -> None:
        with locks.lock(["a", "a", "a"]):
            result.append("ok")

    t = _run_daemon(run)
    t.join(timeout=5)

    assert not t.is_alive(), "duplicate keys in one batch caused a self-deadlock"
    assert result == ["ok"]


def test_reentrant_lock_on_same_thread_raises_instead_of_hanging() -> None:
    """A same-thread nested lock() on a held key raises RuntimeError promptly."""
    locks: KeyedLockManager[str] = KeyedLockManager()
    errors: list[Exception] = []

    def run() -> None:
        try:
            with locks.lock(["a"]):
                with pytest.raises(RuntimeError):
                    with locks.lock(["a"]):
                        pass  # pragma: no cover - unreachable if lock() raises as required
        except Exception as exc:  # noqa: BLE001 - collected and re-raised on the main thread
            errors.append(exc)

    t = _run_daemon(run)
    t.join(timeout=5)

    assert not t.is_alive(), "reentrant lock() call hung instead of raising"
    assert not errors, errors


def test_locks_release_on_exception_in_with_block() -> None:
    """A held key is released even when the with-block's body raises."""
    locks: KeyedLockManager[str] = KeyedLockManager()

    with pytest.raises(ValueError):
        with locks.lock(["a"]):
            raise ValueError("boom")

    acquired_again = threading.Event()
    errors: list[Exception] = []

    def run() -> None:
        try:
            with locks.lock(["a"]):
                acquired_again.set()
        except Exception as exc:  # noqa: BLE001 - collected and re-raised on the main thread
            errors.append(exc)

    t = _run_daemon(run)
    assert acquired_again.wait(timeout=5), "'a' was not released after an exception in the with block"
    t.join(timeout=5)
    assert not errors, errors


def test_concurrent_first_use_of_new_key_is_still_mutually_exclusive() -> None:
    """Many threads racing to lock() a brand-new key for the first time still serialize."""
    locks: KeyedLockManager[str] = KeyedLockManager()
    thread_count = 20
    active = 0
    active_lock = threading.Lock()
    observed_concurrent = False
    start_gate = threading.Barrier(thread_count)
    errors: list[Exception] = []

    def worker() -> None:
        nonlocal active, observed_concurrent
        try:
            start_gate.wait(timeout=5)
            with locks.lock(["brand-new-key"]):
                with active_lock:
                    active += 1
                    if active > 1:
                        observed_concurrent = True
                time.sleep(0.005)
                with active_lock:
                    active -= 1
        except Exception as exc:  # noqa: BLE001 - collected and re-raised on the main thread
            errors.append(exc)

    threads = [_run_daemon(worker) for _ in range(thread_count)]
    for t in threads:
        t.join(timeout=10)

    assert all(not t.is_alive() for t in threads), "not every racing worker finished"
    assert not errors, errors
    assert not observed_concurrent, "two threads held the same brand-new key's lock at once"


def test_same_key_lock_is_reused_across_sequential_calls() -> None:
    """Sequential (non-nested) lock() calls for one key reuse the same registered lock."""
    locks: KeyedLockManager[str] = KeyedLockManager()

    with locks.lock(["a"]):
        pass  # first call fully exits before the second begins

    assert len(locks._order) == 1

    acquired = threading.Event()
    errors: list[Exception] = []

    def run() -> None:
        try:
            with locks.lock(["a"]):
                acquired.set()
        except Exception as exc:  # noqa: BLE001 - collected and re-raised on the main thread
            errors.append(exc)

    t = _run_daemon(run)
    assert acquired.wait(timeout=5), "second sequential lock() call for the same key never acquired"
    t.join(timeout=5)
    assert not errors, errors
    # Still exactly one registered key — re-registration did not corrupt the order map.
    assert len(locks._order) == 1
