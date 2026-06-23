"""Tests for shared_postgres.secrets (Fernet round-trip + Postgres gating)."""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

import shared_postgres.secrets as secrets_mod


class _FakeCursor:
    def __init__(self, store):
        self.store = store
        self._row = None
        self._rows: list = []

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False

    def execute(self, sql, params=None):
        s = sql.strip().upper()
        if s.startswith("CREATE TABLE"):  # idempotent _ensure_table() DDL (no params)
            return
        if (
            "ANY(" in s
        ):  # batched get_secrets: SELECT key, ciphertext WHERE service AND key = ANY(...)
            service, keys = params
            self._rows = [(k, self.store[(service, k)]) for k in keys if (service, k) in self.store]
        elif s.startswith("SELECT"):
            cipher = self.store.get((params[0], params[1]))
            self._row = (cipher,) if cipher is not None else None
        elif "INSERT" in s:
            self.store[(params[0], params[1])] = params[2]
        elif s.startswith("DELETE"):
            self.store.pop((params[0], params[1]), None)

    def fetchone(self):
        return self._row

    def fetchall(self):
        return self._rows


class _FakeConn:
    def __init__(self, store):
        self.store = store

    def cursor(self):
        return _FakeCursor(self.store)


def _fake_get_conn(store):
    class _Ctx:
        def __enter__(self):
            return _FakeConn(store)

        def __exit__(self, *_a):
            return False

    def _factory():
        return _Ctx()

    return _factory


@pytest.fixture
def store(monkeypatch):
    db: dict = {}
    monkeypatch.setattr(secrets_mod, "is_postgres_enabled", lambda: True)
    monkeypatch.setattr(secrets_mod, "get_conn", _fake_get_conn(db))
    monkeypatch.setenv("INTEGRATION_ENCRYPTION_KEY", Fernet.generate_key().decode())
    secrets_mod._reset_fernet_for_testing()
    yield db
    secrets_mod._reset_fernet_for_testing()


def test_set_get_round_trip(store):
    secrets_mod.set_secret("llm_config", "claude_api_key", "sk-secret")
    # stored value is ciphertext, not plaintext
    stored = store[("llm_config", "claude_api_key")]
    assert stored != "sk-secret"
    assert secrets_mod.get_secret("llm_config", "claude_api_key") == "sk-secret"


def test_empty_value_deletes(store):
    secrets_mod.set_secret("llm_config", "k", "v")
    assert ("llm_config", "k") in store
    secrets_mod.set_secret("llm_config", "k", "")
    assert ("llm_config", "k") not in store


def test_get_missing_returns_empty(store):
    assert secrets_mod.get_secret("llm_config", "nope") == ""


def test_get_secrets_batched_round_trip(store):
    secrets_mod.set_secret("llm_config", "provider", "claude")
    secrets_mod.set_secret("llm_config", "model", "claude-opus-4-8")
    out = secrets_mod.get_secrets("llm_config", ["provider", "model", "absent"])
    assert out == {"provider": "claude", "model": "claude-opus-4-8"}  # absent omitted


def test_set_secrets_batched_round_trip(store):
    secrets_mod.set_secrets(
        "llm_config",
        {"provider": "claude", "claude_model": "claude-opus-4-8", "claude_api_key": "sk-x"},
    )
    # Each value is stored as ciphertext and decrypts back to the plaintext.
    assert store[("llm_config", "claude_api_key")] != "sk-x"
    assert secrets_mod.get_secret("llm_config", "provider") == "claude"
    assert secrets_mod.get_secret("llm_config", "claude_model") == "claude-opus-4-8"
    assert secrets_mod.get_secret("llm_config", "claude_api_key") == "sk-x"


def test_set_secrets_uses_one_transaction(store, monkeypatch):
    # All keys must be written within ONE connection/transaction so a partial
    # failure can never commit a half-applied config.
    secrets_mod.get_secret("llm_config", "warm")  # one-time _ensure_table() DDL
    calls = {"n": 0}
    real = secrets_mod.get_conn

    def _counting(*a, **k):
        calls["n"] += 1
        return real(*a, **k)

    monkeypatch.setattr(secrets_mod, "get_conn", _counting)
    secrets_mod.set_secrets("llm_config", {"a": "1", "b": "2", "c": "3"})
    assert calls["n"] == 1


def test_set_secrets_empty_value_deletes_in_batch(store):
    secrets_mod.set_secret("llm_config", "k", "v")
    secrets_mod.set_secrets("llm_config", {"k": "", "j": "v2"})
    assert ("llm_config", "k") not in store  # empty value removed the row
    assert secrets_mod.get_secret("llm_config", "j") == "v2"


def test_set_secrets_empty_mapping_is_noop(store):
    secrets_mod.set_secrets("llm_config", {})  # must not raise, must not write
    assert store == {}


def test_set_secrets_when_disabled_raises(monkeypatch):
    monkeypatch.setattr(secrets_mod, "is_postgres_enabled", lambda: False)
    with pytest.raises(RuntimeError):
        secrets_mod.set_secrets("llm_config", {"k": "v"})


def test_set_secrets_blank_args_assert(store):
    # Preconditions are enforced with explicit ValueError (survives python -O).
    with pytest.raises(ValueError):
        secrets_mod.set_secrets("", {"k": "v"})
    with pytest.raises(ValueError):
        secrets_mod.set_secrets("svc", {"": "v"})


def test_get_secrets_empty_when_disabled(monkeypatch):
    monkeypatch.setattr(secrets_mod, "is_postgres_enabled", lambda: False)
    assert secrets_mod.get_secrets("llm_config", ["provider"]) == {}


def test_get_secrets_no_keys_returns_empty(store):
    assert secrets_mod.get_secrets("llm_config", []) == {}


def test_get_fernet_is_usable(store):
    token = secrets_mod.get_fernet().encrypt(b"hi")
    assert secrets_mod.get_fernet().decrypt(token) == b"hi"


def test_delete_removes(store):
    secrets_mod.set_secret("llm_config", "k", "v")
    secrets_mod.delete_secret("llm_config", "k")
    assert ("llm_config", "k") not in store


def test_corrupt_ciphertext_returns_empty(store):
    store[("llm_config", "k")] = "not-a-valid-fernet-token"
    assert secrets_mod.get_secret("llm_config", "k") == ""


def test_get_when_disabled_returns_empty(monkeypatch):
    monkeypatch.setattr(secrets_mod, "is_postgres_enabled", lambda: False)
    assert secrets_mod.get_secret("llm_config", "k") == ""


def test_set_when_disabled_raises(monkeypatch):
    monkeypatch.setattr(secrets_mod, "is_postgres_enabled", lambda: False)
    with pytest.raises(RuntimeError):
        secrets_mod.set_secret("llm_config", "k", "v")


def test_delete_when_disabled_noop(monkeypatch):
    monkeypatch.setattr(secrets_mod, "is_postgres_enabled", lambda: False)
    secrets_mod.delete_secret("llm_config", "k")  # must not raise


def test_blank_args_assert(store):
    # Preconditions are enforced with explicit ValueError (survives python -O).
    with pytest.raises(ValueError):
        secrets_mod.get_secret("", "k")
    with pytest.raises(ValueError):
        secrets_mod.set_secret("svc", "", "v")


def test_delete_swallows_db_error(store, monkeypatch):
    def _boom():
        raise RuntimeError("conn down")

    monkeypatch.setattr(secrets_mod, "get_conn", _boom)
    # delete is best-effort — must not raise even when the DB call fails.
    secrets_mod.delete_secret("llm_config", "k")


def test_get_swallows_db_error(store, monkeypatch):
    def _boom():
        raise RuntimeError("conn down")

    monkeypatch.setattr(secrets_mod, "get_conn", _boom)
    assert secrets_mod.get_secret("llm_config", "k") == ""


def test_load_or_create_key_generates_and_reuses_file(tmp_path, monkeypatch):
    monkeypatch.delenv("INTEGRATION_ENCRYPTION_KEY", raising=False)
    monkeypatch.setenv("AGENT_CACHE", str(tmp_path))
    secrets_mod._reset_fernet_for_testing()
    key1 = secrets_mod._load_or_create_key()
    key_file = tmp_path / "integration.key"
    assert key_file.exists()
    # The persisted file is fully written (no leftover temp), and 0600 perms.
    assert key_file.read_bytes().strip() == key1
    assert not list(tmp_path.glob(".integration.key.*.tmp"))
    # Second load reads the persisted file (same key).
    key2 = secrets_mod._load_or_create_key()
    assert key1 == key2


def test_load_or_create_key_raises_on_empty_file(tmp_path, monkeypatch):
    """An empty (e.g. truncated/out-of-band) key file must fail loudly, not
    silently regenerate a new key that would orphan every existing secret."""
    monkeypatch.delenv("INTEGRATION_ENCRYPTION_KEY", raising=False)
    monkeypatch.setenv("AGENT_CACHE", str(tmp_path))
    (tmp_path / "integration.key").write_bytes(b"")
    secrets_mod._reset_fernet_for_testing()
    with pytest.raises(RuntimeError, match="is empty"):
        secrets_mod._load_or_create_key()


def test_load_or_create_key_raises_on_invalid_env_key(monkeypatch):
    """A malformed INTEGRATION_ENCRYPTION_KEY must fail fast with a clear message,
    not surface a cryptic Fernet error on the first encrypt/decrypt."""
    monkeypatch.setenv("INTEGRATION_ENCRYPTION_KEY", "not-a-valid-fernet-key")
    secrets_mod._reset_fernet_for_testing()
    with pytest.raises(RuntimeError, match="not a valid Fernet key"):
        secrets_mod._load_or_create_key()


def test_read_key_file_raises_when_unreadable(tmp_path, monkeypatch):
    """An unreadable existing key file raises rather than regenerating."""
    key_path = tmp_path / "integration.key"
    key_path.write_bytes(Fernet.generate_key())

    def _boom(*_a, **_k):
        raise OSError("permission denied")

    monkeypatch.setattr(secrets_mod.Path, "read_bytes", _boom)
    with pytest.raises(RuntimeError, match="unreadable"):
        secrets_mod._read_key_file(key_path)


def test_persist_new_key_adopts_winner_on_create_race(tmp_path):
    """When the final key file already exists (another process won the create),
    _persist_new_key adopts the winner's key instead of clobbering it."""
    key_path = tmp_path / "integration.key"
    winner = Fernet.generate_key()
    key_path.write_bytes(winner)  # simulate the race winner having published its key
    loser = Fernet.generate_key()
    result = secrets_mod._persist_new_key(key_path, loser)
    assert result == winner  # adopted the winner, not the loser's key
    assert key_path.read_bytes().strip() == winner  # file untouched
    assert not list(tmp_path.glob(".integration.key.*.tmp"))  # temp cleaned up


def test_persist_new_key_falls_back_to_replace_without_hardlinks(tmp_path, monkeypatch):
    """On a filesystem without hardlink support, the key is still persisted
    atomically via os.replace, and no partial temp file is left behind."""
    key_path = tmp_path / "integration.key"
    key = Fernet.generate_key()

    def _no_link(*_a, **_k):
        raise OSError("hardlinks unsupported")

    monkeypatch.setattr(secrets_mod.os, "link", _no_link)
    result = secrets_mod._persist_new_key(key_path, key)
    assert result == key
    assert key_path.read_bytes().strip() == key
    assert not list(tmp_path.glob(".integration.key.*.tmp"))


def test_persist_new_key_returns_in_memory_key_when_staging_fails(tmp_path, monkeypatch):
    """If the temp file can't be staged (e.g. read-only volume), fall back to the
    in-memory key so dev still works — it just won't persist."""
    key_path = tmp_path / "integration.key"
    key = Fernet.generate_key()

    def _no_mkstemp(*_a, **_k):
        raise OSError("read-only filesystem")

    monkeypatch.setattr(secrets_mod.tempfile, "mkstemp", _no_mkstemp)
    result = secrets_mod._persist_new_key(key_path, key)
    assert result == key
    assert not key_path.exists()


def test_ensure_table_runs_once_on_first_use(store):
    # The shared store self-heals its table on first use (no reliance on the
    # unified API having run its migration first).
    assert secrets_mod._table_ensured is False
    secrets_mod.get_secret("llm_config", "x")
    assert secrets_mod._table_ensured is True


def test_ensure_table_swallows_ddl_error(monkeypatch):
    monkeypatch.setattr(secrets_mod, "is_postgres_enabled", lambda: True)
    secrets_mod._reset_fernet_for_testing()  # _table_ensured -> False

    def _boom():
        raise RuntimeError("ddl down")

    monkeypatch.setattr(secrets_mod, "get_conn", _boom)
    try:
        secrets_mod._ensure_table()  # must not raise
        assert secrets_mod._table_ensured is False  # retried on the next call
    finally:
        secrets_mod._reset_fernet_for_testing()  # don't leak the process-global flag
