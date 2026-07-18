"""Unit tests for the encrypted credential store (`shared/credential_store.py`).

Covers key resolution (env var, key file, legacy dirs, blank/partial keys),
credential round-trips, corruption tolerance, key rotation, and concurrent
first-time initialisation."""

from __future__ import annotations

from pathlib import Path

import pytest


def test_credential_store_defaults_under_agent_cache(tmp_path: Path, monkeypatch) -> None:
    """Encrypted credentials land on the durable AGENT_CACHE volume path."""
    from agent_provisioning_team.shared.credential_store import (
        CredentialStore,
        default_credentials_dir,
    )

    monkeypatch.setenv("AGENT_CACHE", str(tmp_path / "agents"))
    expected = tmp_path / "agents" / "agent_provisioning" / "credentials"
    assert default_credentials_dir() == expected
    store = CredentialStore()
    assert store.storage_dir == expected
    assert expected.is_dir()


def test_credential_store_with_keyfile_env(tmp_path: Path, monkeypatch) -> None:
    """PA_CREDENTIAL_KEY_FILE pointing at an existing file is read first."""
    from cryptography.fernet import Fernet

    from agent_provisioning_team.shared.credential_store import CredentialStore

    key_file = tmp_path / "key.bin"
    k = Fernet.generate_key()
    key_file.write_bytes(k)

    monkeypatch.setenv("PA_CREDENTIAL_KEY_FILE", str(key_file))
    monkeypatch.delenv("PROVISION_CREDENTIAL_KEY", raising=False)

    store = CredentialStore(storage_dir=tmp_path / "store")
    assert store.fernet is not None
    # Roundtrip a value to prove the key works.
    store.store_credentials("a1", "pg", {"password": "p"})
    assert store.get_credentials("a1", "pg") == {"password": "p"}


def test_credential_store_missing_keyfile_falls_through(tmp_path: Path, monkeypatch) -> None:
    from agent_provisioning_team.shared.credential_store import CredentialStore

    monkeypatch.setenv("PA_CREDENTIAL_KEY_FILE", str(tmp_path / "ghost"))
    monkeypatch.delenv("PROVISION_CREDENTIAL_KEY", raising=False)
    monkeypatch.delenv("PROVISION_REQUIRE_KEY", raising=False)
    # No key file → falls through to auto-generated dev key.
    store = CredentialStore(storage_dir=tmp_path / "store")
    assert store.fernet is not None


def test_credential_store_generate_key_static() -> None:
    from agent_provisioning_team.shared.credential_store import CredentialStore

    k = CredentialStore.generate_key()
    assert isinstance(k, str) and len(k) > 40


def test_credential_store_username_sanitization() -> None:
    from agent_provisioning_team.shared.credential_store import CredentialStore

    u = CredentialStore.generate_username("agent-1!@#", "pg/sql")
    # Only alnum + _ in the output
    for c in u:
        assert c.isalnum() or c == "_"


def test_credential_store_get_credentials_corrupt(tmp_path: Path) -> None:
    from cryptography.fernet import Fernet

    from agent_provisioning_team.shared.credential_store import CredentialStore

    k1 = Fernet.generate_key().decode()
    store = CredentialStore(storage_dir=tmp_path, encryption_key=k1)
    # Hand-write a corrupt file
    path = store._agent_file("agent-x")
    path.write_bytes(b"garbage")
    assert store.get_credentials("agent-x") is None


def test_credential_store_rotate_key_invalid_raises(tmp_path: Path) -> None:
    from agent_provisioning_team.shared.credential_store import (
        CredentialStore,
        CredentialStoreConfigError,
    )

    store = CredentialStore(storage_dir=tmp_path)
    with pytest.raises(CredentialStoreConfigError):
        store.rotate_key("not-a-valid-fernet-key")


def test_credential_store_delete_missing_returns_false(tmp_path: Path) -> None:
    from agent_provisioning_team.shared.credential_store import CredentialStore

    store = CredentialStore(storage_dir=tmp_path)
    assert store.delete_credentials("nobody") is False


def test_credential_store_delete_tool_credentials_removes_one_tool_only(tmp_path: Path) -> None:
    """Deleting one tool's entry must leave the agent's other tools intact."""
    from agent_provisioning_team.shared.credential_store import CredentialStore

    store = CredentialStore(storage_dir=tmp_path)
    store.store_credentials("a1", "pg", {"password": "p"})
    store.store_credentials("a1", "redis", {"password": "r"})

    assert store.delete_tool_credentials("a1", "pg") is True

    assert store.get_credentials("a1", "pg") is None
    assert store.get_credentials("a1", "redis") == {"password": "r"}


def test_credential_store_delete_tool_credentials_missing_agent_returns_false(
    tmp_path: Path,
) -> None:
    from agent_provisioning_team.shared.credential_store import CredentialStore

    store = CredentialStore(storage_dir=tmp_path)
    assert store.delete_tool_credentials("nobody", "pg") is False


def test_credential_store_delete_tool_credentials_missing_tool_returns_false(
    tmp_path: Path,
) -> None:
    """The agent exists but never had this tool — nothing to remove."""
    from agent_provisioning_team.shared.credential_store import CredentialStore

    store = CredentialStore(storage_dir=tmp_path)
    store.store_credentials("a1", "pg", {"password": "p"})

    assert store.delete_tool_credentials("a1", "redis") is False
    assert store.get_credentials("a1", "pg") == {"password": "p"}


def test_credential_store_delete_tool_credentials_removes_legacy_copy_when_emptied(
    tmp_path: Path, monkeypatch
) -> None:
    """Purging a legacy record's only tool must also remove that now-empty legacy file.

    Otherwise the purged secret would still sit in the legacy copy and could
    resurface if the primary file is later removed or becomes unreadable,
    since reads fall back to legacy candidates.
    """
    from agent_provisioning_team.shared.credential_store import CredentialStore

    monkeypatch.chdir(tmp_path)
    key = CredentialStore.generate_key()
    primary = tmp_path / "primary"
    writer = CredentialStore(storage_dir=primary, encryption_key=key)
    writer.store_credentials("legacy-a1", "pg", {"password": "secret"})

    legacy_dir = tmp_path / ".agent_cache" / "provisioning_credentials"
    legacy_dir.mkdir(parents=True)
    legacy_file = legacy_dir / "legacy-a1.enc"
    legacy_file.write_bytes((primary / "legacy-a1.enc").read_bytes())
    (primary / "legacy-a1.enc").unlink()  # primary now empty; only legacy remains

    reader = CredentialStore(storage_dir=tmp_path / "empty-primary", encryption_key=key)
    assert reader.delete_tool_credentials("legacy-a1", "pg") is True

    assert not legacy_file.exists()
    assert reader.get_credentials("legacy-a1", "pg") is None


def test_credential_store_delete_tool_credentials_purges_stale_copy_alongside_primary(
    tmp_path: Path, monkeypatch
) -> None:
    """A legacy copy surviving ALONGSIDE an already-migrated primary must also be purged.

    A legacy file can independently hold a stale copy of a tool's secret
    even while a (newer) primary file already exists — e.g. left over from
    before the primary store cut over. _read_agent_credentials only returns
    the primary in that case, so relying on that single read source alone
    would leave the legacy copy behind, resurfacing the "deleted" secret if
    the primary is later lost. The legacy file's OTHER tool ("redis") must
    survive — this purges only the targeted tool from each candidate.
    """
    from agent_provisioning_team.shared.credential_store import CredentialStore

    monkeypatch.chdir(tmp_path)
    key = CredentialStore.generate_key()
    primary = tmp_path / "primary"
    store = CredentialStore(storage_dir=primary, encryption_key=key)
    store.store_credentials("a1", "pg", {"password": "current"})

    legacy_dir = tmp_path / ".agent_cache" / "provisioning_credentials"
    legacy_dir.mkdir(parents=True)
    legacy_file = legacy_dir / "a1.enc"
    legacy_store = CredentialStore(storage_dir=legacy_dir, encryption_key=key)
    legacy_store.store_credentials("a1", "pg", {"password": "stale"})
    legacy_store.store_credentials("a1", "redis", {"password": "r"})
    assert legacy_file.exists()

    assert store.delete_tool_credentials("a1", "pg") is True

    # Primary: pg gone.
    assert store.get_credentials("a1", "pg") is None
    # Legacy: pg purged too, but its unrelated "redis" entry survives.
    legacy_reader = CredentialStore(storage_dir=legacy_dir, encryption_key=key)
    assert legacy_reader.get_credentials("a1", "pg") is None
    assert legacy_reader.get_credentials("a1", "redis") == {"password": "r"}


def test_credential_store_list_agents(tmp_path: Path) -> None:
    from agent_provisioning_team.shared.credential_store import CredentialStore

    store = CredentialStore(storage_dir=tmp_path)
    store.store_credentials("a1", "pg", {"password": "p"})
    store.store_credentials("a2", "redis", {"password": "p"})
    out = sorted(store.list_agents())
    assert out == ["a1", "a2"]


def test_credential_store_reads_legacy_path(tmp_path: Path, monkeypatch) -> None:
    """Pre-cutover ``.agent_cache/provisioning_credentials`` files remain readable."""
    from agent_provisioning_team.shared.credential_store import CredentialStore

    monkeypatch.chdir(tmp_path)
    key = CredentialStore.generate_key()
    primary = tmp_path / "primary"
    writer = CredentialStore(storage_dir=primary, encryption_key=key)
    writer.store_credentials("legacy-a1", "pg", {"password": "secret"})

    legacy_dir = tmp_path / ".agent_cache" / "provisioning_credentials"
    legacy_dir.mkdir(parents=True)
    legacy_file = legacy_dir / "legacy-a1.enc"
    legacy_file.write_bytes((primary / "legacy-a1.enc").read_bytes())

    reader = CredentialStore(storage_dir=tmp_path / "empty-primary", encryption_key=key)
    assert reader.get_credentials("legacy-a1", "pg") == {"password": "secret"}
    assert "legacy-a1" in reader.list_agents()
    assert reader.delete_credentials("legacy-a1") is True
    assert not legacy_file.exists()


def test_credential_store_reads_legacy_with_legacy_generated_key(
    tmp_path: Path, monkeypatch
) -> None:
    """Upgrade: primary mints a new dir but legacy .enc still decrypts via legacy key."""
    from agent_provisioning_team.shared.credential_store import CredentialStore

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PROVISION_CREDENTIAL_KEY", raising=False)
    monkeypatch.delenv("PA_CREDENTIAL_KEY_FILE", raising=False)
    monkeypatch.delenv("PROVISION_REQUIRE_KEY", raising=False)

    legacy_dir = tmp_path / ".agent_cache" / "provisioning_credentials"
    legacy_writer = CredentialStore(storage_dir=legacy_dir)
    legacy_writer.store_credentials("legacy-a1", "pg", {"password": "secret"})
    assert (legacy_dir / ".encryption_key").is_file()

    # New primary path with no explicit key — must still open the legacy file.
    primary = tmp_path / "agents" / "agent_provisioning" / "credentials"
    reader = CredentialStore(storage_dir=primary)
    assert reader.get_credentials("legacy-a1", "pg") == {"password": "secret"}
    assert (primary / ".encryption_key").is_file()


def test_credential_store_decrypts_legacy_when_primary_key_diverged(
    tmp_path: Path, monkeypatch
) -> None:
    """Primary already minted a fresh key; legacy files still decrypt via trailing key."""
    from agent_provisioning_team.shared.credential_store import CredentialStore

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PROVISION_CREDENTIAL_KEY", raising=False)
    monkeypatch.delenv("PA_CREDENTIAL_KEY_FILE", raising=False)
    monkeypatch.delenv("PROVISION_REQUIRE_KEY", raising=False)

    primary = tmp_path / "primary"
    # Mint a primary key before any legacy dir exists (simulates a prior boot
    # that generated a divergent key under the new path).
    CredentialStore(storage_dir=primary)
    primary_key = (primary / ".encryption_key").read_bytes()

    legacy_dir = tmp_path / ".agent_cache" / "provisioning_credentials"
    CredentialStore(storage_dir=legacy_dir).store_credentials(
        "legacy-a1", "pg", {"password": "secret"}
    )
    assert primary_key != (legacy_dir / ".encryption_key").read_bytes()

    reader = CredentialStore(storage_dir=primary)
    assert reader.get_credentials("legacy-a1", "pg") == {"password": "secret"}


def test_credential_store_store_credentials_handles_corrupt_existing(tmp_path: Path) -> None:
    """If an existing encrypted file is corrupt, store overwrites it."""
    from agent_provisioning_team.shared.credential_store import CredentialStore

    store = CredentialStore(storage_dir=tmp_path)
    path = store._agent_file("a1")
    path.write_bytes(b"garbage")

    store.store_credentials("a1", "pg", {"password": "p"})
    assert store.get_credentials("a1", "pg") == {"password": "p"}


def test_credential_store_get_credentials_returns_all_when_no_tool(tmp_path: Path) -> None:
    from agent_provisioning_team.shared.credential_store import CredentialStore

    store = CredentialStore(storage_dir=tmp_path)
    store.store_credentials("a1", "pg", {"password": "p1"})
    store.store_credentials("a1", "redis", {"password": "p2"})
    out = store.get_credentials("a1")
    assert set(out.keys()) == {"pg", "redis"}


def test_credential_store_load_key_with_blank_env(tmp_path: Path, monkeypatch) -> None:
    """Empty PROVISION_CREDENTIAL_KEY is treated as unset."""
    from agent_provisioning_team.shared.credential_store import CredentialStore

    monkeypatch.setenv("PROVISION_CREDENTIAL_KEY", "")
    monkeypatch.delenv("PA_CREDENTIAL_KEY_FILE", raising=False)
    monkeypatch.delenv("PROVISION_REQUIRE_KEY", raising=False)
    store = CredentialStore(storage_dir=tmp_path)
    assert store.fernet is not None


@pytest.mark.parametrize("partial", [b"", b"   ", b"not-a-valid-fernet-key"])
def test_credential_store_tolerates_partial_key_file(tmp_path: Path, monkeypatch, partial) -> None:
    """A present-but-invalid key file (the concurrent-write race window) is
    replaced rather than crashing the store with an invalid Fernet key."""
    from agent_provisioning_team.shared.credential_store import CredentialStore

    monkeypatch.delenv("PROVISION_CREDENTIAL_KEY", raising=False)
    monkeypatch.delenv("PA_CREDENTIAL_KEY_FILE", raising=False)
    monkeypatch.delenv("PROVISION_REQUIRE_KEY", raising=False)
    sdir = tmp_path / "store"
    sdir.mkdir()
    (sdir / ".encryption_key").write_bytes(partial)  # simulate a half-written file

    store = CredentialStore(storage_dir=sdir)  # must not raise
    store.store_credentials("a1", "pg", {"password": "secret"})
    assert store.get_credentials("a1", "pg") == {"password": "secret"}


def test_credential_store_concurrent_init_converges_on_one_key(tmp_path: Path, monkeypatch) -> None:
    """Concurrent first-time inits converge on a single key and never clobber
    a key a peer has already published and encrypted credentials under."""
    import threading

    from agent_provisioning_team.shared.credential_store import CredentialStore

    monkeypatch.delenv("PROVISION_CREDENTIAL_KEY", raising=False)
    monkeypatch.delenv("PA_CREDENTIAL_KEY_FILE", raising=False)
    monkeypatch.delenv("PROVISION_REQUIRE_KEY", raising=False)
    sdir = tmp_path / "shared"
    n = 12
    errors: list[Exception] = []
    barrier = threading.Barrier(n)

    def _build(i: int) -> None:
        try:
            barrier.wait()  # release all threads into key-init at once
            store = CredentialStore(storage_dir=sdir)
            store.store_credentials(f"agent{i}", "pg", {"password": f"p{i}"})
        except Exception as exc:  # pragma: no cover - only on regression
            errors.append(exc)

    threads = [threading.Thread(target=_build, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"concurrent init raised: {errors}"
    # If any init had clobbered another's published key, the credentials that
    # peer encrypted would now be undecryptable. A fresh store must decrypt all.
    fresh = CredentialStore(storage_dir=sdir)
    for i in range(n):
        assert fresh.get_credentials(f"agent{i}", "pg") == {"password": f"p{i}"}


def test_credential_store_invalid_key_raises(tmp_path: Path) -> None:
    from agent_provisioning_team.shared.credential_store import (
        CredentialStore,
        CredentialStoreConfigError,
    )

    with pytest.raises(CredentialStoreConfigError):
        CredentialStore(storage_dir=tmp_path, encryption_key="not-a-real-key")


def test_credential_store_rotate_skips_corrupt_files(tmp_path: Path) -> None:
    from cryptography.fernet import Fernet

    from agent_provisioning_team.shared.credential_store import CredentialStore

    k = Fernet.generate_key().decode()
    store = CredentialStore(storage_dir=tmp_path, encryption_key=k)
    store.store_credentials("a1", "pg", {"password": "p"})

    # Drop a corrupt .enc file alongside the valid one.
    (tmp_path / "garbage.enc").write_bytes(b"not encrypted")

    new_k = Fernet.generate_key().decode()
    rotated = store.rotate_key(new_k)
    # Valid file rotated, garbage skipped — count is 1.
    assert rotated == 1
