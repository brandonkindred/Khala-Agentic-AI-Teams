"""Unit tests for unified_api.integration_credentials.

After PR 1 of the SQLite → Postgres migration this module is a thin
shim around ``postgres_encrypted_credentials`` plus a one-shot migration
helper. The tests here cover:

* Fernet key management (unchanged from the pre-migration module).
* The ``get_credential`` / ``set_credential`` / ``delete_credential`` /
  ``delete_service_credentials`` delegation to the ``pg_*`` functions.
* ``migrate_sqlite_to_postgres_once`` idempotency, empty-SQLite, and
  Postgres-disabled short-circuits.

These tests never touch a real Postgres — they monkey-patch
``shared_postgres.get_conn`` and the ``pg_*`` functions to keep the
suite fast and self-contained. Integration coverage with a live
``postgres:16`` service runs in the ``test-shared-postgres`` CI job.
"""

from __future__ import annotations

import importlib
import sqlite3
import sys
from contextlib import contextmanager
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
# Key management (unchanged from pre-migration module)
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


# ---------------------------------------------------------------------------
# migrate_sqlite_to_postgres_once
# ---------------------------------------------------------------------------


def _write_legacy_sqlite(tmp_path: Path, mod, rows: list[tuple[str, str, str]]) -> Path:
    """Create a legacy SQLite file at ``$AGENT_CACHE/integration_credentials.db``.

    Each row is ``(service, key, plaintext_value)`` — the helper
    encrypts with the reloaded module's Fernet key and writes the row.
    """
    db_path = tmp_path / "integration_credentials.db"
    fernet = mod.get_integration_fernet()
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS service_integrations (
            service TEXT NOT NULL,
            key TEXT NOT NULL,
            value TEXT NOT NULL,
            PRIMARY KEY (service, key)
        )
        """
    )
    for service, key, plaintext in rows:
        encrypted = fernet.encrypt(plaintext.encode()).decode()
        conn.execute(
            "INSERT INTO service_integrations (service, key, value) VALUES (?, ?, ?)",
            (service, key, encrypted),
        )
    conn.commit()
    conn.close()
    return db_path


class _FakeCursor:
    def __init__(self, marker_present: bool = False) -> None:
        self.executed: list[tuple[str, tuple]] = []
        self._marker_present = marker_present
        self._last_fetch: tuple | None = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql: str, params: tuple = ()):
        self.executed.append((sql, params))
        if sql.strip().startswith("SELECT 1 FROM migration_markers"):
            self._last_fetch = (1,) if self._marker_present else None

    def fetchone(self):
        return self._last_fetch


class _FakeConn:
    def __init__(self, cursor: _FakeCursor) -> None:
        self._cursor = cursor

    def cursor(self):
        return self._cursor


def _fake_get_conn_factory(cursor: _FakeCursor):
    @contextmanager
    def _fake_get_conn(database=None):
        yield _FakeConn(cursor)

    return _fake_get_conn


def test_migrate_skipped_when_postgres_disabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("POSTGRES_HOST", raising=False)
    mod = _reload(tmp_path, monkeypatch)
    # Presence of a SQLite file does not matter when Postgres is off.
    _write_legacy_sqlite(tmp_path, mod, [("slack", "client_id", "opaque-value")])
    result = mod.migrate_sqlite_to_postgres_once()
    assert result == {"status": "skipped", "reason": "postgres_disabled"}


def test_migrate_skipped_when_sqlite_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("POSTGRES_HOST", "localhost")
    mod = _reload(tmp_path, monkeypatch)
    result = mod.migrate_sqlite_to_postgres_once()
    assert result == {"status": "skipped", "reason": "no_sqlite_file"}


def test_migrate_happy_path_writes_rows_and_marker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("POSTGRES_HOST", "localhost")
    mod = _reload(tmp_path, monkeypatch)

    _write_legacy_sqlite(
        tmp_path,
        mod,
        [
            ("slack", "client_id", "opaque-slack-client-id"),
            ("slack", "client_secret", "opaque-slack-client-secret"),
            ("medium", "refresh_token", "opaque-medium-refresh-token"),
        ],
    )

    cursor = _FakeCursor(marker_present=False)
    import shared_postgres

    monkeypatch.setattr(shared_postgres, "get_conn", _fake_get_conn_factory(cursor))

    # Observe the bulk helper so we can verify decrypted plaintexts round-trip.
    import unified_api.postgres_encrypted_credentials as pg_mod

    upserted_rows: list[tuple[str, str, str]] = []

    def _bulk(cur, rows):
        rows_list = list(rows)
        # Decrypt each ciphertext to verify plaintext survived the round-trip.
        fernet = mod.get_integration_fernet()
        for service, key, ciphertext in rows_list:
            plaintext = fernet.decrypt(ciphertext.encode()).decode()
            upserted_rows.append((service, key, plaintext))
            cur.execute(
                "INSERT INTO encrypted_integration_credentials (service, credential_key, ciphertext, updated_at) "
                "VALUES (%s, %s, %s, NOW())",
                (service, key, ciphertext),
            )
        return len(rows_list)

    monkeypatch.setattr(pg_mod, "pg_upsert_credentials_bulk", _bulk)

    result = mod.migrate_sqlite_to_postgres_once()
    assert result == {"status": "migrated", "rows": 3}

    plaintexts = {(s, k): v for s, k, v in upserted_rows}
    assert plaintexts[("slack", "client_id")] == "opaque-slack-client-id"
    assert plaintexts[("slack", "client_secret")] == "opaque-slack-client-secret"
    assert plaintexts[("medium", "refresh_token")] == "opaque-medium-refresh-token"

    # The marker INSERT must be issued AFTER the bulk upsert.
    marker_calls = [(sql, params) for sql, params in cursor.executed if "INSERT INTO migration_markers" in sql]
    assert len(marker_calls) == 1
    assert marker_calls[0][1][0] == "integration_credentials_v1"


def test_migrate_idempotent_when_marker_present(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Second run short-circuits inside the same transaction when the marker exists."""
    monkeypatch.setenv("POSTGRES_HOST", "localhost")
    mod = _reload(tmp_path, monkeypatch)
    _write_legacy_sqlite(tmp_path, mod, [("slack", "client_id", "opaque-value")])

    cursor = _FakeCursor(marker_present=True)
    import shared_postgres

    monkeypatch.setattr(shared_postgres, "get_conn", _fake_get_conn_factory(cursor))

    import unified_api.postgres_encrypted_credentials as pg_mod

    bulk_called = {"count": 0}
    monkeypatch.setattr(
        pg_mod,
        "pg_upsert_credentials_bulk",
        lambda cur, rows: bulk_called.__setitem__("count", bulk_called["count"] + 1) or 0,
    )

    result = mod.migrate_sqlite_to_postgres_once()
    assert result == {"status": "skipped", "reason": "marker_present"}
    assert bulk_called["count"] == 0


def test_migrate_skips_corrupt_row_without_aborting(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("POSTGRES_HOST", "localhost")
    mod = _reload(tmp_path, monkeypatch)

    # Write one valid row, one un-decryptable row directly.
    db_path = tmp_path / "integration_credentials.db"
    fernet = mod.get_integration_fernet()
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE service_integrations (service TEXT, key TEXT, value TEXT, PRIMARY KEY (service, key))")
    conn.execute(
        "INSERT INTO service_integrations VALUES (?, ?, ?)",
        ("slack", "client_id", fernet.encrypt(b"opaque-good-row").decode()),
    )
    conn.execute(
        "INSERT INTO service_integrations VALUES (?, ?, ?)",
        ("slack", "client_secret", "not-a-valid-fernet-ciphertext"),
    )
    conn.commit()
    conn.close()

    rows = mod._read_sqlite_rows(db_path)
    assert rows == [("slack", "client_id", "opaque-good-row")]


def test_migrate_empty_sqlite_still_writes_marker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A legacy SQLite file with zero rows still records the marker."""
    monkeypatch.setenv("POSTGRES_HOST", "localhost")
    mod = _reload(tmp_path, monkeypatch)
    _write_legacy_sqlite(tmp_path, mod, [])  # file exists, no rows

    cursor = _FakeCursor(marker_present=False)
    import shared_postgres

    monkeypatch.setattr(shared_postgres, "get_conn", _fake_get_conn_factory(cursor))

    result = mod.migrate_sqlite_to_postgres_once()
    assert result == {"status": "skipped", "reason": "empty_sqlite"}

    marker_inserts = [(sql, params) for sql, params in cursor.executed if "INSERT INTO migration_markers" in sql]
    assert len(marker_inserts) == 1
    assert marker_inserts[0][1][0] == "integration_credentials_v1"
