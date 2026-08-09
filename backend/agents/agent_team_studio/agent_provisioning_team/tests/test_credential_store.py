"""Unit tests for the encrypted credential store (`shared/credential_store.py`).

Covers key resolution (env var, key file, legacy dirs, blank/partial keys),
credential round-trips, corruption tolerance, key rotation, and concurrent
first-time initialisation."""

from __future__ import annotations

from pathlib import Path

import pytest


def test_credential_store_defaults_under_agent_cache(tmp_path: Path, monkeypatch) -> None:
    """Encrypted credentials land on the durable AGENT_CACHE volume path."""
    from agent_team_studio.agent_provisioning_team.shared.credential_store import (
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

    from agent_team_studio.agent_provisioning_team.shared.credential_store import CredentialStore

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
    from agent_team_studio.agent_provisioning_team.shared.credential_store import CredentialStore

    monkeypatch.setenv("PA_CREDENTIAL_KEY_FILE", str(tmp_path / "ghost"))
    monkeypatch.delenv("PROVISION_CREDENTIAL_KEY", raising=False)
    monkeypatch.delenv("PROVISION_REQUIRE_KEY", raising=False)
    # No key file → falls through to auto-generated dev key.
    store = CredentialStore(storage_dir=tmp_path / "store")
    assert store.fernet is not None


def test_credential_store_generate_key_static() -> None:
    from agent_team_studio.agent_provisioning_team.shared.credential_store import CredentialStore

    k = CredentialStore.generate_key()
    assert isinstance(k, str) and len(k) > 40


def test_credential_store_username_sanitization() -> None:
    from agent_team_studio.agent_provisioning_team.shared.credential_store import CredentialStore

    u = CredentialStore.generate_username("agent-1!@#", "pg/sql")
    # Only alnum + _ in the output
    for c in u:
        assert c.isalnum() or c == "_"


def test_credential_store_get_credentials_corrupt(tmp_path: Path) -> None:
    from cryptography.fernet import Fernet

    from agent_team_studio.agent_provisioning_team.shared.credential_store import CredentialStore

    k1 = Fernet.generate_key().decode()
    store = CredentialStore(storage_dir=tmp_path, encryption_key=k1)
    # Hand-write a corrupt file
    path = store._agent_file("agent-x")
    path.write_bytes(b"garbage")
    assert store.get_credentials("agent-x") is None


def test_credential_store_rotate_key_invalid_raises(tmp_path: Path) -> None:
    from agent_team_studio.agent_provisioning_team.shared.credential_store import (
        CredentialStore,
        CredentialStoreConfigError,
    )

    store = CredentialStore(storage_dir=tmp_path)
    with pytest.raises(CredentialStoreConfigError):
        store.rotate_key("not-a-valid-fernet-key")


def test_credential_store_delete_missing_returns_false(tmp_path: Path) -> None:
    from agent_team_studio.agent_provisioning_team.shared.credential_store import CredentialStore

    store = CredentialStore(storage_dir=tmp_path)
    assert store.delete_credentials("nobody") is False


def test_credential_store_delete_tool_credentials_removes_one_tool_only(tmp_path: Path) -> None:
    """Deleting one tool's entry must leave the agent's other tools intact."""
    from agent_team_studio.agent_provisioning_team.shared.credential_store import CredentialStore

    store = CredentialStore(storage_dir=tmp_path)
    store.store_credentials("a1", "pg", {"password": "p"})
    store.store_credentials("a1", "redis", {"password": "r"})

    assert store.delete_tool_credentials("a1", "pg") is True

    assert store.get_credentials("a1", "pg") is None
    assert store.get_credentials("a1", "redis") == {"password": "r"}


def test_credential_store_delete_tool_credentials_missing_agent_returns_false(
    tmp_path: Path,
) -> None:
    from agent_team_studio.agent_provisioning_team.shared.credential_store import CredentialStore

    store = CredentialStore(storage_dir=tmp_path)
    assert store.delete_tool_credentials("nobody", "pg") is False


def test_credential_store_delete_tool_credentials_missing_tool_returns_false(
    tmp_path: Path,
) -> None:
    """The agent exists but never had this tool — nothing to remove."""
    from agent_team_studio.agent_provisioning_team.shared.credential_store import CredentialStore

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
    from agent_team_studio.agent_provisioning_team.shared.credential_store import CredentialStore

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
    from agent_team_studio.agent_provisioning_team.shared.credential_store import CredentialStore

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


def test_credential_store_delete_tool_credentials_removes_emptied_primary_to_expose_legacy(
    tmp_path: Path, monkeypatch
) -> None:
    """An emptied primary file must be unlinked, not left behind as an empty blob.

    _read_agent_credentials stops at the first candidate path that EXISTS,
    regardless of its content — so a primary rewritten to an encrypted
    ``{}`` after its only tool is purged would mask an untouched legacy
    entry for a completely different tool from ever being read again by
    the same CredentialStore instance. Unlinking the emptied primary lets
    that read fall through to the legacy candidate that still legitimately
    owns it, exactly like an emptied legacy candidate already does.
    """
    from agent_team_studio.agent_provisioning_team.shared.credential_store import CredentialStore

    monkeypatch.chdir(tmp_path)
    key = CredentialStore.generate_key()
    primary = tmp_path / "primary"
    store = CredentialStore(storage_dir=primary, encryption_key=key)
    store.store_credentials("a1", "pg", {"password": "current"})  # primary's only tool

    legacy_dir = tmp_path / ".agent_cache" / "provisioning_credentials"
    legacy_dir.mkdir(parents=True)
    legacy_store = CredentialStore(storage_dir=legacy_dir, encryption_key=key)
    legacy_store.store_credentials("a1", "redis", {"password": "r"})  # legacy-only survivor

    assert store.delete_tool_credentials("a1", "pg") is True

    assert not (primary / "a1.enc").exists()
    # Reads on the very same store instance now fall through to legacy and
    # still find the untouched "redis" entry instead of being masked by an
    # emptied-but-still-present primary file.
    assert store.get_credentials("a1", "redis") == {"password": "r"}
    assert store.get_credentials("a1", "pg") is None


def test_credential_store_delete_tool_credentials_sentinel_only_primary_preserves_mark(
    tmp_path: Path,
) -> None:
    """Purging the last real tool must KEEP the sentinel-only blob so the fencing
    high-water mark survives and a later stale write cannot resurrect a secret.

    ``delete_tool_credentials`` does not tombstone, but it must not drop the
    mark either: when the last real tool is removed from a primary that carries
    the fencing sentinel, the leftover ``{sentinel}`` blob is retained. Dropping
    it would let a resumed stale caller's ``store_credentials`` bootstrap-accept
    (``current_token=None``) and resurrect a secret a newer owner tore down.
    """
    from agent_team_studio.agent_provisioning_team.shared.credential_store import CredentialStore
    from agent_team_studio.agent_provisioning_team.shared.fencing import StaleFencingTokenError

    key = CredentialStore.generate_key()
    store = CredentialStore(storage_dir=tmp_path / "store", encryption_key=key)
    # A newer owner (token 6) has provisioned then purges its only tool.
    store.store_credentials("a1", "pg", {"password": "current"}, fencing_token=6)
    assert store.delete_tool_credentials("a1", "pg", fencing_token=6) is True

    # The blob is kept (only the sentinel remains) so it reads as absent to
    # ordinary callers but still holds the mark...
    assert store.get_credentials("a1") is None
    assert store.get_credentials("a1", "pg") is None
    # ...and a resumed stale worker (token 5) is rejected, not bootstrap-accepted.
    with pytest.raises(StaleFencingTokenError):
        store.store_credentials("a1", "pg", {"password": "resurrected"}, fencing_token=5)


def test_credential_store_delete_tool_credentials_purges_legacy_when_primary_lacks_tool(
    tmp_path: Path, monkeypatch
) -> None:
    """A legacy-only copy of ``tool_name`` must be purged even when the primary
    file exists but never held that tool.

    Gating the whole method on whichever single file ``_read_agent_credentials``
    preferred (primary, since it exists) would return ``False`` immediately and
    never even look at the legacy candidate — leaving its stale copy of the
    tool's secret behind. Each candidate must be inspected independently.
    """
    from agent_team_studio.agent_provisioning_team.shared.credential_store import CredentialStore

    monkeypatch.chdir(tmp_path)
    key = CredentialStore.generate_key()
    primary = tmp_path / "primary"
    store = CredentialStore(storage_dir=primary, encryption_key=key)
    store.store_credentials("a1", "redis", {"password": "current"})  # no "pg" here

    legacy_dir = tmp_path / ".agent_cache" / "provisioning_credentials"
    legacy_dir.mkdir(parents=True)
    legacy_file = legacy_dir / "a1.enc"
    legacy_store = CredentialStore(storage_dir=legacy_dir, encryption_key=key)
    legacy_store.store_credentials("a1", "pg", {"password": "stale"})
    assert legacy_file.exists()

    assert store.delete_tool_credentials("a1", "pg") is True

    # Primary untouched: its own "redis" entry survives, still no "pg".
    assert store.get_credentials("a1", "redis") == {"password": "current"}
    # Legacy: the stale "pg" copy is gone, and the now-empty legacy file removed.
    assert not legacy_file.exists()


def test_credential_store_list_tool_names_merges_primary_and_legacy(
    tmp_path: Path, monkeypatch
) -> None:
    """list_tool_names must return the union of every candidate's keys.

    get_credentials/_read_agent_credentials stop at the first candidate that
    exists, so a tool name that lives ONLY in a legacy file (never migrated
    to primary) would otherwise never be discoverable by a caller that needs
    to enumerate every tool this agent currently has credentials for.
    """
    from agent_team_studio.agent_provisioning_team.shared.credential_store import CredentialStore

    monkeypatch.chdir(tmp_path)
    key = CredentialStore.generate_key()
    primary = tmp_path / "primary"
    store = CredentialStore(storage_dir=primary, encryption_key=key)
    store.store_credentials("a1", "pg", {"password": "p"})

    legacy_dir = tmp_path / ".agent_cache" / "provisioning_credentials"
    legacy_dir.mkdir(parents=True)
    legacy_store = CredentialStore(storage_dir=legacy_dir, encryption_key=key)
    legacy_store.store_credentials("a1", "redis", {"password": "r"})

    assert store.list_tool_names("a1") == {"pg", "redis"}


def test_credential_store_list_tool_names_missing_agent_returns_empty_set(
    tmp_path: Path,
) -> None:
    from agent_team_studio.agent_provisioning_team.shared.credential_store import CredentialStore

    store = CredentialStore(storage_dir=tmp_path)
    assert store.list_tool_names("nobody") == set()


def test_credential_store_list_agents(tmp_path: Path) -> None:
    from agent_team_studio.agent_provisioning_team.shared.credential_store import CredentialStore

    store = CredentialStore(storage_dir=tmp_path)
    store.store_credentials("a1", "pg", {"password": "p"})
    store.store_credentials("a2", "redis", {"password": "p"})
    out = sorted(store.list_agents())
    assert out == ["a1", "a2"]


def test_credential_store_reads_legacy_path(tmp_path: Path, monkeypatch) -> None:
    """Pre-cutover ``.agent_cache/provisioning_credentials`` files remain readable."""
    from agent_team_studio.agent_provisioning_team.shared.credential_store import CredentialStore

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
    from agent_team_studio.agent_provisioning_team.shared.credential_store import CredentialStore

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
    from agent_team_studio.agent_provisioning_team.shared.credential_store import CredentialStore

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
    from agent_team_studio.agent_provisioning_team.shared.credential_store import CredentialStore

    store = CredentialStore(storage_dir=tmp_path)
    path = store._agent_file("a1")
    path.write_bytes(b"garbage")

    store.store_credentials("a1", "pg", {"password": "p"})
    assert store.get_credentials("a1", "pg") == {"password": "p"}


def test_credential_store_get_credentials_returns_all_when_no_tool(tmp_path: Path) -> None:
    from agent_team_studio.agent_provisioning_team.shared.credential_store import CredentialStore

    store = CredentialStore(storage_dir=tmp_path)
    store.store_credentials("a1", "pg", {"password": "p1"})
    store.store_credentials("a1", "redis", {"password": "p2"})
    out = store.get_credentials("a1")
    assert set(out.keys()) == {"pg", "redis"}


def test_credential_store_write_methods_reject_stale_token(tmp_path: Path) -> None:
    from agent_team_studio.agent_provisioning_team.shared.credential_store import CredentialStore
    from agent_team_studio.agent_provisioning_team.shared.fencing import StaleFencingTokenError

    store = CredentialStore(storage_dir=tmp_path)
    store.store_credentials("a1", "pg", {"password": "p1"}, fencing_token=5)

    with pytest.raises(StaleFencingTokenError):
        store.store_credentials("a1", "pg", {"password": "p2"}, fencing_token=4)
    with pytest.raises(StaleFencingTokenError):
        store.delete_credentials("a1", fencing_token=4)

    # Neither rejected call mutated the stored credentials.
    assert store.get_credentials("a1", "pg") == {"password": "p1"}


def test_credential_store_write_methods_accept_equal_and_higher_token(tmp_path: Path) -> None:
    from agent_team_studio.agent_provisioning_team.shared.credential_store import CredentialStore

    store = CredentialStore(storage_dir=tmp_path)
    store.store_credentials("a1", "pg", {"password": "p1"}, fencing_token=5)
    store.store_credentials("a1", "redis", {"password": "p2"}, fencing_token=5)
    store.store_credentials("a1", "git", {"password": "p3"}, fencing_token=6)
    assert store.delete_credentials("a1", fencing_token=6) is True


def test_credential_store_fencing_token_none_is_full_noop(tmp_path: Path) -> None:
    from agent_team_studio.agent_provisioning_team.shared.credential_store import CredentialStore

    store = CredentialStore(storage_dir=tmp_path)
    store.store_credentials("a1", "pg", {"password": "p1"}, fencing_token=5)
    store.store_credentials("a1", "redis", {"password": "p2"})
    assert store.delete_credentials("a1") is True


def test_credential_store_get_credentials_never_surfaces_fencing_sentinel(tmp_path: Path) -> None:
    """The reserved fencing-token key must never appear as a fake 'tool' in
    the whole-dict get_credentials(agent_id) return path."""
    from agent_team_studio.agent_provisioning_team.shared.credential_store import CredentialStore

    store = CredentialStore(storage_dir=tmp_path)
    store.store_credentials("a1", "pg", {"password": "p1"}, fencing_token=5)
    out = store.get_credentials("a1")
    assert set(out.keys()) == {"pg"}


def test_credential_store_load_key_with_blank_env(tmp_path: Path, monkeypatch) -> None:
    """Empty PROVISION_CREDENTIAL_KEY is treated as unset."""
    from agent_team_studio.agent_provisioning_team.shared.credential_store import CredentialStore

    monkeypatch.setenv("PROVISION_CREDENTIAL_KEY", "")
    monkeypatch.delenv("PA_CREDENTIAL_KEY_FILE", raising=False)
    monkeypatch.delenv("PROVISION_REQUIRE_KEY", raising=False)
    store = CredentialStore(storage_dir=tmp_path)
    assert store.fernet is not None


@pytest.mark.parametrize("partial", [b"", b"   ", b"not-a-valid-fernet-key"])
def test_credential_store_tolerates_partial_key_file(tmp_path: Path, monkeypatch, partial) -> None:
    """A present-but-invalid key file (the concurrent-write race window) is
    replaced rather than crashing the store with an invalid Fernet key."""
    from agent_team_studio.agent_provisioning_team.shared.credential_store import CredentialStore

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

    from agent_team_studio.agent_provisioning_team.shared.credential_store import CredentialStore

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
    from agent_team_studio.agent_provisioning_team.shared.credential_store import (
        CredentialStore,
        CredentialStoreConfigError,
    )

    with pytest.raises(CredentialStoreConfigError):
        CredentialStore(storage_dir=tmp_path, encryption_key="not-a-real-key")


def test_credential_store_rotate_skips_corrupt_files(tmp_path: Path) -> None:
    from cryptography.fernet import Fernet

    from agent_team_studio.agent_provisioning_team.shared.credential_store import CredentialStore

    k = Fernet.generate_key().decode()
    store = CredentialStore(storage_dir=tmp_path, encryption_key=k)
    store.store_credentials("a1", "pg", {"password": "p"})

    # Drop a corrupt .enc file alongside the valid one.
    (tmp_path / "garbage.enc").write_bytes(b"not encrypted")

    new_k = Fernet.generate_key().decode()
    rotated = store.rotate_key(new_k)
    # Valid file rotated, garbage skipped — count is 1.
    assert rotated == 1
