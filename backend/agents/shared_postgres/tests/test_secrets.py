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

    def execute(self, sql, params):
        s = sql.strip().upper()
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
    with pytest.raises(AssertionError):
        secrets_mod.get_secret("", "k")
    with pytest.raises(AssertionError):
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
    assert (tmp_path / "integration.key").exists()
    # Second load reads the persisted file (same key).
    key2 = secrets_mod._load_or_create_key()
    assert key1 == key2
