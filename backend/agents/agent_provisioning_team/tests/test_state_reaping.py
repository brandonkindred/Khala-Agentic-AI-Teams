"""Bounded reaping of fencing tombstones / force-expired lock records.

Covers ``shared/state_reaping.py`` plus each store's ``reap_stale``/auto-reap,
proving the fix for the unbounded-growth review finding: aged tombstones and
long-expired lock records are reclaimed, while live records and records young
enough to still shadow a resumable stale worker are always preserved.
"""

from __future__ import annotations

from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# state_retention_seconds parsing / clamping
# ---------------------------------------------------------------------------
def test_retention_default_when_unset(monkeypatch) -> None:
    from agent_provisioning_team.shared import state_reaping

    monkeypatch.delenv("AGENT_STATE_RETENTION_S", raising=False)
    assert state_reaping.state_retention_seconds() == 7 * 24 * 3600


def test_retention_garbage_falls_back_to_default(monkeypatch) -> None:
    from agent_provisioning_team.shared import state_reaping

    monkeypatch.setenv("AGENT_STATE_RETENTION_S", "not-a-number")
    assert state_reaping.state_retention_seconds() == 7 * 24 * 3600


def test_retention_below_floor_is_clamped_up(monkeypatch) -> None:
    from agent_provisioning_team.shared import state_reaping

    monkeypatch.setenv("AGENT_STATE_RETENTION_S", "60")  # 1 minute — unsafe
    assert state_reaping.state_retention_seconds() == 24 * 3600  # floored to 1 day


def test_retention_nonpositive_falls_back_to_default(monkeypatch) -> None:
    from agent_provisioning_team.shared import state_reaping

    monkeypatch.setenv("AGENT_STATE_RETENTION_S", "0")
    assert state_reaping.state_retention_seconds() == 7 * 24 * 3600


def test_retention_valid_value_passes_through(monkeypatch) -> None:
    from agent_provisioning_team.shared import state_reaping

    monkeypatch.setenv("AGENT_STATE_RETENTION_S", str(3 * 24 * 3600))
    assert state_reaping.state_retention_seconds() == 3 * 24 * 3600


# ---------------------------------------------------------------------------
# AgentLockStore.reap_stale
# ---------------------------------------------------------------------------
def _lock_store(tmp_path: Path, ttl_seconds: int = 100):
    from agent_provisioning_team.shared.agent_lock import AgentLockStore

    return AgentLockStore(storage_dir=tmp_path / "locks", ttl_seconds=ttl_seconds)


def test_lock_reap_removes_long_expired_record(tmp_path: Path) -> None:
    store = _lock_store(tmp_path)
    store.acquire("agent-old", owner="job-1", now=1000.0)
    store.release("agent-old", owner="job-1", now=1000.0)  # expires_at = 999
    # Reap far in the future, retention 1 day: 999 << now - 86400.
    reaped = store.reap_stale(now=1000.0 + 10 * 86400, retention_s=86400)
    assert reaped == 1
    assert store._read_record("agent-old") is None


def test_lock_reap_keeps_live_lease(tmp_path: Path) -> None:
    store = _lock_store(tmp_path, ttl_seconds=100_000)
    store.acquire("agent-live", owner="job-1", now=1000.0)  # expires_at = 101000
    # Reap while the lease is still live (expires_at in the future) — never
    # eligible regardless of retention.
    reaped = store.reap_stale(now=2000.0, retention_s=86400)
    assert reaped == 0
    assert store._read_record("agent-live") is not None


def test_lock_reap_keeps_recently_released_record(tmp_path: Path) -> None:
    store = _lock_store(tmp_path)
    store.acquire("agent-recent", owner="job-1", now=1000.0)
    store.release("agent-recent", owner="job-1", now=1000.0)  # expires_at = 999
    # Only 10 minutes have passed since expiry; retention is 1 day → keep.
    reaped = store.reap_stale(now=1000.0 + 600, retention_s=86400)
    assert reaped == 0
    rec = store._read_record("agent-recent")
    assert rec is not None and rec["fencing_token"] == 1


def test_lock_reap_never_raises_on_garbage_file(tmp_path: Path) -> None:
    store = _lock_store(tmp_path)
    store.storage_dir.mkdir(parents=True, exist_ok=True)
    (store.storage_dir / "junk.json").write_text("not json {{{", encoding="utf-8")
    assert store.reap_stale(now=10 * 86400, retention_s=86400) == 0


# ---------------------------------------------------------------------------
# EnvironmentStore.reap_stale
# ---------------------------------------------------------------------------
def _env(tmp_path: Path):
    from agent_provisioning_team.shared.environment_store import EnvironmentStore

    return EnvironmentStore(storage_dir=tmp_path)


def _env_info(agent_id: str):
    from agent_provisioning_team.shared.environment_store import EnvironmentInfo

    return EnvironmentInfo(
        agent_id=agent_id, container_id="c-" + agent_id, container_name="n-" + agent_id
    )


def test_env_reap_removes_old_tombstone_keeps_live(tmp_path: Path) -> None:
    import os

    store = _env(tmp_path)
    store.register(_env_info("live"), fencing_token=1)
    store.register(_env_info("gone"), fencing_token=1)
    assert store.remove("gone", fencing_token=2) is True  # leaves a tombstone
    # Age the tombstone file well past retention via its mtime.
    tomb = store.storage_dir / "gone.json"
    old = 1000.0
    os.utime(tomb, (old, old))
    reaped = store.reap_stale(now=old + 10 * 86400, retention_s=86400)
    assert reaped == 1
    assert not tomb.exists()
    # Live record untouched (and its own recent mtime protects it regardless).
    assert store.exists("live") is True


def test_env_reap_keeps_recent_tombstone(tmp_path: Path) -> None:
    store = _env(tmp_path)
    store.register(_env_info("gone"), fencing_token=1)
    store.remove("gone", fencing_token=2)
    # Tombstone just written (recent mtime) → not eligible.
    reaped = store.reap_stale(retention_s=86400)
    assert reaped == 0
    assert (store.storage_dir / "gone.json").exists()


def test_env_reap_never_removes_valid_record_even_if_old(tmp_path: Path) -> None:
    import os

    store = _env(tmp_path)
    store.register(_env_info("live"), fencing_token=1)
    rec = store.storage_dir / "live.json"
    os.utime(rec, (1000.0, 1000.0))  # make it ancient
    reaped = store.reap_stale(now=1000.0 + 10 * 86400, retention_s=86400)
    assert reaped == 0
    assert store.exists("live") is True


# ---------------------------------------------------------------------------
# CredentialStore.reap_stale
# ---------------------------------------------------------------------------
def _cred(tmp_path: Path):
    from agent_provisioning_team.shared.credential_store import CredentialStore

    return CredentialStore(storage_dir=tmp_path / "creds")


def test_cred_reap_removes_old_tombstone_keeps_live(tmp_path: Path) -> None:
    import os

    store = _cred(tmp_path)
    store.store_credentials("live", "toolA", {"username": "u"}, fencing_token=1)
    store.store_credentials("gone", "toolB", {"username": "v"}, fencing_token=1)
    assert store.delete_credentials("gone", fencing_token=2) is True  # tombstone blob
    tomb = store.storage_dir / "gone.enc"
    os.utime(tomb, (1000.0, 1000.0))
    reaped = store.reap_stale(now=1000.0 + 10 * 86400, retention_s=86400)
    assert reaped == 1
    assert not tomb.exists()
    assert store.get_credentials("live", "toolA") == {"username": "u"}


def test_cred_reap_keeps_recent_tombstone_and_live(tmp_path: Path) -> None:
    store = _cred(tmp_path)
    store.store_credentials("live", "toolA", {"username": "u"}, fencing_token=1)
    store.store_credentials("gone", "toolB", {"username": "v"}, fencing_token=1)
    store.delete_credentials("gone", fencing_token=2)
    reaped = store.reap_stale(retention_s=86400)  # everything just written
    assert reaped == 0
    assert (store.storage_dir / "gone.enc").exists()
    assert store.get_credentials("live", "toolA") == {"username": "u"}


# ---------------------------------------------------------------------------
# ProvisionerStateStore auto-reap on _save
# ---------------------------------------------------------------------------
def _prov(tmp_path: Path):
    from agent_provisioning_team.shared.provisioner_state import ProvisionerStateStore

    return ProvisionerStateStore("docker_provisioner", storage_dir=tmp_path / "state")


def test_provisioner_state_autoreaps_old_tombstone(tmp_path: Path, monkeypatch) -> None:
    import time as _time

    store = _prov(tmp_path)
    store.put("gone", {"container_id": "c1"}, fencing_token=1)
    store.delete("gone", fencing_token=2)  # tombstone with tombstoned_at=now
    # Force the recorded tombstone timestamp far into the past.
    raw = store._load()
    raw["gone"]["tombstoned_at"] = _time.time() - 10 * 86400
    store._save(raw)  # _save reaps as part of the write
    assert "gone" not in store._load()


def test_provisioner_state_keeps_recent_tombstone(tmp_path: Path) -> None:
    store = _prov(tmp_path)
    store.put("gone", {"container_id": "c1"}, fencing_token=1)
    store.delete("gone", fencing_token=2)  # just-written tombstone
    # A later unrelated write triggers _save; the recent tombstone survives so
    # its high-water mark still rejects a stale caller.
    store.put("other", {"container_id": "c2"}, fencing_token=1)
    assert "gone" in store._load()
    from agent_provisioning_team.shared.fencing import StaleFencingTokenError

    with pytest.raises(StaleFencingTokenError):
        store.check_fencing_token("gone", 1)


def test_provisioner_state_keeps_live_row_even_without_timestamp(tmp_path: Path) -> None:
    store = _prov(tmp_path)
    store.put("live", {"container_id": "c1"}, fencing_token=1)
    # Live rows carry no tombstoned_at and must never be reaped.
    store.put("live", {"container_id": "c1b"}, fencing_token=1)
    assert store.get("live") == {"container_id": "c1b"}


# ---------------------------------------------------------------------------
# Coordinator: throttle + best-effort
# ---------------------------------------------------------------------------
def test_coordinator_throttles(monkeypatch) -> None:
    from agent_provisioning_team.shared import state_reaping

    calls = {"n": 0}

    class _FakeStore:
        def __init__(self, *a, **k):
            pass

        def reap_stale(self, **k):
            calls["n"] += 1
            return 0

    monkeypatch.setattr(state_reaping, "_last_reap_at", None)
    monkeypatch.setattr("agent_provisioning_team.shared.agent_lock.AgentLockStore", _FakeStore)
    monkeypatch.setattr(
        "agent_provisioning_team.shared.environment_store.EnvironmentStore", _FakeStore
    )
    monkeypatch.setattr(
        "agent_provisioning_team.shared.credential_store.CredentialStore", _FakeStore
    )

    state_reaping.reap_stale_agent_state(now=1000.0)
    assert calls["n"] == 3  # all three stores swept
    # A second call inside the throttle interval is a no-op.
    state_reaping.reap_stale_agent_state(now=1000.0 + 5)
    assert calls["n"] == 3
    # Past the interval it runs again.
    state_reaping.reap_stale_agent_state(now=1000.0 + state_reaping._REAP_MIN_INTERVAL_S + 1)
    assert calls["n"] == 6


def test_coordinator_survives_one_store_raising(monkeypatch) -> None:
    from agent_provisioning_team.shared import state_reaping

    seen = []

    class _BoomStore:
        def __init__(self, *a, **k):
            pass

        def reap_stale(self, **k):
            raise RuntimeError("io")

    class _OkStore:
        def __init__(self, *a, **k):
            pass

        def reap_stale(self, **k):
            seen.append(type(self).__name__)
            return 0

    monkeypatch.setattr(state_reaping, "_last_reap_at", None)
    monkeypatch.setattr("agent_provisioning_team.shared.agent_lock.AgentLockStore", _BoomStore)
    monkeypatch.setattr(
        "agent_provisioning_team.shared.environment_store.EnvironmentStore", _OkStore
    )
    monkeypatch.setattr("agent_provisioning_team.shared.credential_store.CredentialStore", _OkStore)

    # Must not raise even though the lock store blows up.
    state_reaping.reap_stale_agent_state(now=2000.0, force=True)
    assert seen == ["_OkStore", "_OkStore"]  # env + cred still swept


def test_coordinator_survives_env_and_cred_raising(monkeypatch) -> None:
    from agent_provisioning_team.shared import state_reaping

    class _Boom:
        def __init__(self, *a, **k):
            pass

        def reap_stale(self, **k):
            raise RuntimeError("io")

    class _Ok:
        def __init__(self, *a, **k):
            pass

        def reap_stale(self, **k):
            return 0

    monkeypatch.setattr(state_reaping, "_last_reap_at", None)
    monkeypatch.setattr("agent_provisioning_team.shared.agent_lock.AgentLockStore", _Ok)
    monkeypatch.setattr(
        "agent_provisioning_team.shared.environment_store.EnvironmentStore", _Boom
    )
    monkeypatch.setattr("agent_provisioning_team.shared.credential_store.CredentialStore", _Boom)

    # Both the env and credential reapers raising must be swallowed independently.
    state_reaping.reap_stale_agent_state(now=3000.0, force=True)
