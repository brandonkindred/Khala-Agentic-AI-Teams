"""Unit tests for shared/agent_lock.py's AgentLockStore.

Covers acquire/release ownership semantics, idempotent re-acquire for the
current owner, TTL-based staleness reclaim, and a concurrency test proving
exactly one of N racing acquirers wins — the core guarantee issue #1489's
serialization fix depends on.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest


def _store(tmp_path: Path, ttl_seconds: int = 7200):
    from agent_provisioning_team.shared.agent_lock import AgentLockStore

    return AgentLockStore(storage_dir=tmp_path / "locks", ttl_seconds=ttl_seconds)


def test_acquire_succeeds_when_unowned(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.acquire("agent-1", owner="job-1")
    assert store.get_owner("agent-1") == "job-1"


def test_acquire_raises_when_busy(tmp_path: Path) -> None:
    from agent_provisioning_team.shared.agent_lock import AgentLockBusyError

    store = _store(tmp_path)
    store.acquire("agent-1", owner="job-1")

    with pytest.raises(AgentLockBusyError) as exc_info:
        store.acquire("agent-1", owner="job-2")

    assert exc_info.value.agent_id == "agent-1"
    assert exc_info.value.holder == "job-1"
    # A failed acquire must not disturb the existing owner's record.
    assert store.get_owner("agent-1") == "job-1"


def test_acquire_is_idempotent_for_same_owner(tmp_path: Path) -> None:
    """A retried acquire_agent_lock_activity for the *same* job renews rather
    than raising, so Temporal activity retries never self-deadlock."""
    store = _store(tmp_path)
    store.acquire("agent-1", owner="job-1", now=1000.0)
    first_expiry = store._read_record("agent-1")["expires_at"]

    store.acquire("agent-1", owner="job-1", now=2000.0)
    second_expiry = store._read_record("agent-1")["expires_at"]

    assert store.get_owner("agent-1", now=2000.0) == "job-1"
    assert second_expiry > first_expiry


def test_acquire_succeeds_after_ttl_expiry(tmp_path: Path) -> None:
    """A stale record (owner never released — e.g. a hard-terminated
    workflow) self-heals: a new owner can claim it once the lease expires."""
    store = _store(tmp_path, ttl_seconds=100)
    store.acquire("agent-1", owner="job-1", now=1000.0)

    # Still within the lease: a different owner is rejected.
    from agent_provisioning_team.shared.agent_lock import AgentLockBusyError

    with pytest.raises(AgentLockBusyError):
        store.acquire("agent-1", owner="job-2", now=1050.0)

    # Past the lease: a different owner may now claim it.
    store.acquire("agent-1", owner="job-2", now=1200.0)
    assert store.get_owner("agent-1", now=1200.0) == "job-2"


def test_release_clears_only_for_current_owner(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.acquire("agent-1", owner="job-1")

    store.release("agent-1", owner="job-1")

    assert store.get_owner("agent-1") is None


def test_release_is_noop_for_non_owner(tmp_path: Path) -> None:
    """release() is best-effort cleanup: it must never raise or clear a
    record it doesn't own (mirrors cleanup_setup/compensate's idiom)."""
    store = _store(tmp_path)
    store.acquire("agent-1", owner="job-1")

    store.release("agent-1", owner="job-2")  # must not raise

    assert store.get_owner("agent-1") == "job-1"


def test_release_is_noop_when_absent(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.release("agent-1", owner="job-1")  # must not raise
    assert store.get_owner("agent-1") is None


def test_get_owner_returns_none_when_expired(tmp_path: Path) -> None:
    store = _store(tmp_path, ttl_seconds=100)
    store.acquire("agent-1", owner="job-1", now=1000.0)

    assert store.get_owner("agent-1", now=1050.0) == "job-1"
    assert store.get_owner("agent-1", now=1200.0) is None


def test_acquire_rejects_unsafe_agent_id(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(ValueError):
        store.acquire("../../etc/passwd", owner="job-1")


def test_locks_for_different_agents_are_independent(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.acquire("agent-1", owner="job-1")
    store.acquire("agent-2", owner="job-2")  # must not raise

    assert store.get_owner("agent-1") == "job-1"
    assert store.get_owner("agent-2") == "job-2"


def test_default_locks_dir_uses_agent_cache_env(tmp_path: Path, monkeypatch) -> None:
    from agent_provisioning_team.shared.agent_lock import AgentLockStore, default_locks_dir

    monkeypatch.setenv("AGENT_CACHE", str(tmp_path))
    assert default_locks_dir() == tmp_path / "agent_provisioning" / "locks"

    store = AgentLockStore()  # storage_dir omitted -> falls back to default_locks_dir()
    assert store.storage_dir == tmp_path / "agent_provisioning" / "locks"


def test_acquire_fails_closed_on_corrupted_record(tmp_path: Path) -> None:
    """P2 regression: a torn/corrupted record file must NOT be treated as
    absent — that would let a second caller silently steal a lock a live
    owner still holds. acquire()/release()/get_owner() all fail closed
    (raise) rather than guessing, unlike ProvisionerStateStore._load's
    tolerance (that store isn't a mutual-exclusion primitive)."""
    from agent_provisioning_team.shared.agent_lock import AgentLockError

    store = _store(tmp_path)
    record_path = store._record_path("agent-1")
    record_path.write_text("not valid json {{{", encoding="utf-8")

    with pytest.raises(AgentLockError):
        store.get_owner("agent-1")
    with pytest.raises(AgentLockError):
        store.acquire("agent-1", owner="job-1")
    with pytest.raises(AgentLockError):
        store.release("agent-1", owner="job-1")
    # None of the failed calls above touched the corrupt file.
    assert record_path.read_text(encoding="utf-8") == "not valid json {{{"


def test_write_record_cleans_up_tmp_file_on_replace_failure(tmp_path: Path, monkeypatch) -> None:
    """A failure during publish (e.g. os.replace) propagates and leaves no
    stray tempfile behind."""
    import os

    store = _store(tmp_path)

    def _boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", _boom)

    with pytest.raises(OSError, match="disk full"):
        store.acquire("agent-1", owner="job-1")

    leftover = list(store.storage_dir.glob(".agent-1.*.json"))
    assert leftover == []
    assert store.get_owner("agent-1") is None


def test_delete_record_is_noop_when_already_gone(tmp_path: Path) -> None:
    """_delete_record tolerates a record removed out-of-band between the
    ownership check and the unlink (a narrow, otherwise-harmless TOCTOU)."""
    store = _store(tmp_path)
    store._delete_record("never-acquired")  # must not raise


def test_acquire_returns_token_starting_at_one(tmp_path: Path) -> None:
    store = _store(tmp_path)
    token = store.acquire("agent-1", owner="job-1")
    assert token == 1


def test_live_renewal_does_not_bump_token(tmp_path: Path) -> None:
    """A same-owner re-acquire before expiry is a pure lease renewal: the
    fencing token must stay stable, or concurrent activities fanned out
    with the pre-renewal token would be wrongly self-rejected as stale."""
    store = _store(tmp_path, ttl_seconds=1000)
    first = store.acquire("agent-1", owner="job-1", now=1000.0)
    second = store.acquire("agent-1", owner="job-1", now=1500.0)
    assert second == first


def test_reclaim_after_expiry_by_different_owner_bumps_token(tmp_path: Path) -> None:
    store = _store(tmp_path, ttl_seconds=100)
    first = store.acquire("agent-1", owner="job-1", now=1000.0)
    second = store.acquire("agent-1", owner="job-2", now=1200.0)
    assert second == first + 1


def test_reclaim_after_expiry_by_same_owner_also_bumps_token(tmp_path: Path) -> None:
    """A resuming caller cannot tell 'nobody touched this while I was gone'
    from 'someone acquired and released it while I was gone' -- both read
    as 'expired'. A same-owner reclaim after expiry must therefore mint a
    NEW token, not silently reuse the old one, so that any of its own
    activities still carrying the pre-expiry token are correctly treated
    as stale by the resource stores."""
    store = _store(tmp_path, ttl_seconds=100)
    first = store.acquire("agent-1", owner="job-1", now=1000.0)
    second = store.acquire("agent-1", owner="job-1", now=1200.0)
    assert second == first + 1


def test_concurrent_acquire_winner_gets_next_token(tmp_path: Path) -> None:
    store = _store(tmp_path)
    baseline = store.acquire("agent-1", owner="job-1", now=1000.0)
    store.release("agent-1", owner="job-1", now=1000.0)

    winner_token = store.acquire("agent-1", owner="job-2", now=1001.0)
    assert winner_token == baseline + 1


def test_release_preserves_token(tmp_path: Path) -> None:
    store = _store(tmp_path)
    token = store.acquire("agent-1", owner="job-1", now=1000.0)

    store.release("agent-1", owner="job-1", now=1000.0)

    record = store._read_record("agent-1")
    assert record is not None
    assert record["token"] == token
    assert record["expires_at"] < 1000.0


def test_release_with_stale_fencing_token_is_noop(tmp_path: Path) -> None:
    store = _store(tmp_path, ttl_seconds=100)
    store.acquire("agent-1", owner="job-1", now=1000.0)
    store.release("agent-1", owner="job-1", now=1000.0)
    # job-1 reclaims after expiry, minting a new (higher) token.
    current_token = store.acquire("agent-1", owner="job-1", now=1200.0)

    # A stale caller presenting the pre-reclaim token must not release.
    store.release("agent-1", owner="job-1", now=1200.0, fencing_token=current_token - 1)

    assert store.get_owner("agent-1", now=1200.0) == "job-1"
    record = store._read_record("agent-1")
    assert record["token"] == current_token


def test_release_with_fencing_token_none_behaves_as_before(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.acquire("agent-1", owner="job-1")
    store.release("agent-1", owner="job-1", fencing_token=None)
    assert store.get_owner("agent-1") is None


def test_concurrent_acquire_exactly_one_wins(tmp_path: Path) -> None:
    """N threads racing acquire() on the same agent_id: exactly one succeeds,
    the rest observe AgentLockBusyError. This is the guarantee that makes
    two concurrent AgentProvisioningWorkflow runs for one agent_id safe. Also
    asserts the sole winner mints token 1 -- proving the mint-under-flock is
    itself race-free, not just the owner assignment."""
    from agent_provisioning_team.shared.agent_lock import AgentLockBusyError, AgentLockStore

    sdir = tmp_path / "locks"
    n = 12
    winners: list[str] = []
    losers: list[str] = []
    errors: list[Exception] = []
    tokens: list[int] = []
    lock = threading.Lock()
    barrier = threading.Barrier(n)

    def _try_acquire(i: int) -> None:
        owner = f"job-{i}"
        try:
            barrier.wait()  # release all threads into acquire() at once
            store = AgentLockStore(storage_dir=sdir)
            try:
                token = store.acquire("agent-1", owner=owner)
                with lock:
                    winners.append(owner)
                    tokens.append(token)
            except AgentLockBusyError:
                with lock:
                    losers.append(owner)
        except Exception as exc:  # pragma: no cover - only on regression
            errors.append(exc)

    threads = [threading.Thread(target=_try_acquire, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"concurrent acquire raised: {errors}"
    assert len(winners) == 1, f"expected exactly one winner, got {winners}"
    assert len(losers) == n - 1
    assert AgentLockStore(storage_dir=sdir).get_owner("agent-1") == winners[0]
    assert tokens == [1]
