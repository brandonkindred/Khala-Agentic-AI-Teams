"""Unit tests for unified_api.integration_credentials.

This module is a thin shim around ``postgres_encrypted_credentials``; the
tests cover:

* Fernet key management.
* The ``get_credential`` / ``set_credential`` / ``delete_credential`` /
  ``delete_service_credentials`` delegation to the ``pg_*`` functions.

These tests never touch a real Postgres — they monkey-patch the ``pg_*``
functions to keep the suite fast and self-contained. Integration
coverage with a live ``postgres:16`` service runs in the
``test-shared-postgres`` CI job.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

_backend = Path(__file__).resolve().parent.parent.parent
if str(_backend) not in sys.path:
    sys.path.insert(0, str(_backend))


def _reload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point AGENT_CACHE at tmp_path and reload module to pick up fresh key path."""
    monkeypatch.setenv("AGENT_CACHE", str(tmp_path))
    monkeypatch.delenv("INTEGRATION_ENCRYPTION_KEY", raising=False)
    import unified_api.integration_credentials as mod

    importlib.reload(mod)
    return mod


# ---------------------------------------------------------------------------
# Key management
# ---------------------------------------------------------------------------


def test_load_or_create_key_uses_env_var(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import base64
    import os

    key = base64.urlsafe_b64encode(os.urandom(32)).decode()
    monkeypatch.setenv("INTEGRATION_ENCRYPTION_KEY", key)
    mod = _reload(tmp_path, monkeypatch)
    monkeypatch.setenv("INTEGRATION_ENCRYPTION_KEY", key)
    assert mod._load_or_create_key() == key.encode()


def test_load_or_create_key_generates_and_persists(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    mod = _reload(tmp_path, monkeypatch)
    key1 = mod._load_or_create_key()
    key_path = tmp_path / "integration.key"
    assert key_path.exists()
    key2 = mod._load_or_create_key()
    assert key1 == key2


def test_load_or_create_key_reads_existing_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import base64
    import os

    key = base64.urlsafe_b64encode(os.urandom(32))
    key_path = tmp_path / "integration.key"
    key_path.write_bytes(key)
    mod = _reload(tmp_path, monkeypatch)
    assert mod._load_or_create_key() == key


def test_get_integration_fernet_round_trip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    mod = _reload(tmp_path, monkeypatch)
    fernet = mod.get_integration_fernet()
    token = fernet.encrypt(b"test_data")
    assert fernet.decrypt(token) == b"test_data"


# ---------------------------------------------------------------------------
# CRUD delegation: thin shims call the pg_* functions
# ---------------------------------------------------------------------------


def test_get_credential_returns_empty_when_postgres_disabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """When POSTGRES_HOST is unset, ``get_credential`` returns '' without calling pg_*.

    This is the defensive branch that keeps ``get_slack_config()`` from
    crashing during startup in dev environments without Postgres.
    """
    monkeypatch.delenv("POSTGRES_HOST", raising=False)
    mod = _reload(tmp_path, monkeypatch)

    import unified_api.postgres_encrypted_credentials as pg_mod

    calls = []
    monkeypatch.setattr(pg_mod, "pg_get_credential", lambda *a, **k: calls.append(a) or "from_pg")

    assert mod.get_credential("svc", "key") == ""
    assert calls == []  # never reached pg_get_credential


def test_get_credential_delegates_to_pg_when_enabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("POSTGRES_HOST", "localhost")
    mod = _reload(tmp_path, monkeypatch)

    import unified_api.postgres_encrypted_credentials as pg_mod

    monkeypatch.setattr(pg_mod, "pg_get_credential", lambda svc, key: f"pg:{svc}/{key}")

    assert mod.get_credential("slack", "client_id") == "pg:slack/client_id"


def test_set_credential_delegates_to_pg_set(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("POSTGRES_HOST", "localhost")
    mod = _reload(tmp_path, monkeypatch)

    import unified_api.postgres_encrypted_credentials as pg_mod

    captured = {}

    def _set(svc, key, value):
        captured["args"] = (svc, key, value)

    monkeypatch.setattr(pg_mod, "pg_set_credential", _set)
    monkeypatch.setattr(pg_mod, "pg_delete_credential", lambda *a, **k: None)

    mod.set_credential("slack", "client_id", "my-id")
    assert captured["args"] == ("slack", "client_id", "my-id")


def test_set_credential_empty_value_deletes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """set_credential('', '') delegates to pg_delete_credential instead of pg_set."""
    monkeypatch.setenv("POSTGRES_HOST", "localhost")
    mod = _reload(tmp_path, monkeypatch)

    import unified_api.postgres_encrypted_credentials as pg_mod

    deletes = []
    sets = []
    monkeypatch.setattr(pg_mod, "pg_delete_credential", lambda svc, key: deletes.append((svc, key)))
    monkeypatch.setattr(pg_mod, "pg_set_credential", lambda svc, key, v: sets.append((svc, key, v)))

    mod.set_credential("svc", "k", "")
    assert deletes == [("svc", "k")]
    assert sets == []


def test_delete_credential_delegates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("POSTGRES_HOST", "localhost")
    mod = _reload(tmp_path, monkeypatch)

    import unified_api.postgres_encrypted_credentials as pg_mod

    captured = []
    monkeypatch.setattr(pg_mod, "pg_delete_credential", lambda svc, key: captured.append((svc, key)))

    mod.delete_credential("svc", "k")
    assert captured == [("svc", "k")]


def test_delete_service_credentials_delegates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("POSTGRES_HOST", "localhost")
    mod = _reload(tmp_path, monkeypatch)

    import unified_api.postgres_encrypted_credentials as pg_mod

    captured = []
    monkeypatch.setattr(pg_mod, "pg_delete_service_credentials", lambda svc: captured.append(svc))

    mod.delete_service_credentials("slack")
    assert captured == ["slack"]
