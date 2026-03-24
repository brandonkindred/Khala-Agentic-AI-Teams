"""Unit tests for unified_api.integration_credentials (SQLite + Fernet store)."""

import importlib
import sqlite3
import sys
from pathlib import Path

import pytest

_backend = Path(__file__).resolve().parent.parent.parent
if str(_backend) not in sys.path:
    sys.path.insert(0, str(_backend))


def _reload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point AGENT_CACHE at tmp_path and reload module to pick up fresh DB + key paths."""
    monkeypatch.setenv("AGENT_CACHE", str(tmp_path))
    monkeypatch.delenv("INTEGRATION_ENCRYPTION_KEY", raising=False)
    import unified_api.integration_credentials as mod

    importlib.reload(mod)
    return mod


# ---------------------------------------------------------------------------
# Key management
# ---------------------------------------------------------------------------


def test_load_or_create_key_uses_env_var(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """_load_or_create_key returns the INTEGRATION_ENCRYPTION_KEY env var when set."""
    import base64
    import os

    key = base64.urlsafe_b64encode(os.urandom(32)).decode()
    monkeypatch.setenv("INTEGRATION_ENCRYPTION_KEY", key)
    mod = _reload(tmp_path, monkeypatch)
    # Re-set after reload so the module picks it up
    monkeypatch.setenv("INTEGRATION_ENCRYPTION_KEY", key)
    assert mod._load_or_create_key() == key.encode()


def test_load_or_create_key_generates_and_persists(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """_load_or_create_key generates a key, saves it, and returns the same key on the next call."""
    mod = _reload(tmp_path, monkeypatch)
    key1 = mod._load_or_create_key()
    key_path = tmp_path / "integration.key"
    assert key_path.exists()
    key2 = mod._load_or_create_key()
    assert key1 == key2


def test_load_or_create_key_reads_existing_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """_load_or_create_key reads an existing integration.key file."""
    import base64
    import os

    key = base64.urlsafe_b64encode(os.urandom(32))
    key_path = tmp_path / "integration.key"
    key_path.write_bytes(key)
    mod = _reload(tmp_path, monkeypatch)
    assert mod._load_or_create_key() == key


def test_get_integration_fernet_returns_fernet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """get_integration_fernet() returns an object that can encrypt and decrypt round-trip."""
    mod = _reload(tmp_path, monkeypatch)
    fernet = mod.get_integration_fernet()
    # Verify it can encrypt and decrypt
    token = fernet.encrypt(b"test_data")
    assert fernet.decrypt(token) == b"test_data"


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


def test_set_and_get_credential_round_trip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """set_credential and get_credential are a lossless round-trip."""
    mod = _reload(tmp_path, monkeypatch)
    mod.set_credential("svc", "api_key", "supersecret")
    assert mod.get_credential("svc", "api_key") == "supersecret"


def test_get_credential_returns_empty_when_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """get_credential returns '' when the key does not exist."""
    mod = _reload(tmp_path, monkeypatch)
    assert mod.get_credential("nonexistent", "key") == ""


def test_set_credential_empty_value_deletes_row(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """set_credential with an empty value removes the existing row."""
    mod = _reload(tmp_path, monkeypatch)
    mod.set_credential("svc", "key", "value")
    mod.set_credential("svc", "key", "")
    assert mod.get_credential("svc", "key") == ""


def test_delete_credential_removes_only_target_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """delete_credential removes one key without touching sibling keys."""
    mod = _reload(tmp_path, monkeypatch)
    mod.set_credential("svc", "a", "aaa")
    mod.set_credential("svc", "b", "bbb")
    mod.delete_credential("svc", "a")
    assert mod.get_credential("svc", "a") == ""
    assert mod.get_credential("svc", "b") == "bbb"


def test_delete_service_credentials_removes_all_keys_for_service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """delete_service_credentials removes all keys for the target service."""
    mod = _reload(tmp_path, monkeypatch)
    mod.set_credential("svc", "key1", "v1")
    mod.set_credential("svc", "key2", "v2")
    mod.set_credential("other", "key", "vother")
    mod.delete_service_credentials("svc")
    assert mod.get_credential("svc", "key1") == ""
    assert mod.get_credential("svc", "key2") == ""
    # Other service is untouched
    assert mod.get_credential("other", "key") == "vother"


def test_delete_credential_nonexistent_is_noop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """delete_credential on a non-existent key does not raise."""
    mod = _reload(tmp_path, monkeypatch)
    mod.delete_credential("svc", "never_existed")  # should not raise


def test_multiple_services_are_independent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Credentials for different services do not interfere with each other."""
    mod = _reload(tmp_path, monkeypatch)
    mod.set_credential("slack", "client_id", "slack-cid")
    mod.set_credential("medium", "refresh_token", "medium-rt")
    assert mod.get_credential("slack", "client_id") == "slack-cid"
    assert mod.get_credential("medium", "refresh_token") == "medium-rt"


# ---------------------------------------------------------------------------
# Encryption assurance
# ---------------------------------------------------------------------------


def test_credentials_not_stored_in_plain_text(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Raw SQLite rows do not contain the plaintext secret value."""
    mod = _reload(tmp_path, monkeypatch)
    mod.set_credential("svc", "secret_key", "my_plaintext_secret_xyz")
    db_path = tmp_path / "integration_credentials.db"
    conn = sqlite3.connect(str(db_path))
    rows = conn.execute("SELECT value FROM service_integrations").fetchall()
    conn.close()
    raw_values = [row[0] for row in rows]
    assert all("my_plaintext_secret_xyz" not in v for v in raw_values)


def test_db_file_is_created_in_agent_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The SQLite DB is created inside the AGENT_CACHE directory."""
    mod = _reload(tmp_path, monkeypatch)
    mod.set_credential("svc", "k", "v")
    db_path = tmp_path / "integration_credentials.db"
    assert db_path.exists()
