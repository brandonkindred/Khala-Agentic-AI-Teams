"""
Encrypted credential store for service integrations (OAuth client IDs and secrets).

As of PR 1 of the SQLite → Postgres migration this module is a **thin
compatibility shim** around ``postgres_encrypted_credentials``:

- The Fernet key management (``_load_or_create_key``,
  ``get_integration_fernet``) still lives here because both the legacy
  SQLite path and the Postgres path depend on the same key material and
  we want exactly one place that touches ``INTEGRATION_ENCRYPTION_KEY``
  / ``$AGENT_CACHE/integration.key``.
- The CRUD surface (``get_credential``, ``set_credential``,
  ``delete_credential``, ``delete_service_credentials``) now delegates
  to ``pg_*`` functions against the Postgres
  ``encrypted_integration_credentials`` table.
- ``migrate_sqlite_to_postgres_once`` is an opt-in one-shot helper gated
  by ``INTEGRATIONS_MIGRATE_SQLITE=1`` that lifts existing rows out of
  an old ``$AGENT_CACHE/integration_credentials.db`` into Postgres.
  Idempotency is guarded by a ``migration_markers`` row rather than a
  file rename so re-runs are safe.

Security notes:
  * The Fernet key file is persisted at ``$AGENT_CACHE/integration.key``
    with ``chmod 600`` and never logged. If you recreate the
    ``agents_data`` Docker volume you lose the key and every existing
    encrypted row becomes unreadable — set ``INTEGRATION_ENCRYPTION_KEY``
    as a Docker secret / env var in production to avoid that footgun.
  * ``get_credential`` intentionally returns ``""`` when Postgres is
    disabled instead of raising, because ``get_slack_config()`` is
    called during app startup and the UI should load even when the
    credential store is offline.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from cryptography.fernet import Fernet

logger = logging.getLogger(__name__)

_DEFAULT_CACHE_DIR = ".agent_cache"

# Name of the one-shot SQLite→Postgres migration marker row.
_MIGRATION_MARKER = "integration_credentials_v1"


# ---------------------------------------------------------------------------
# Encryption key management
# ---------------------------------------------------------------------------


def _load_or_create_key() -> bytes:
    """
    Return the Fernet encryption key.
    Priority:
      1. INTEGRATION_ENCRYPTION_KEY env var (base64-url-safe 32-byte key)
      2. Persisted key file at {AGENT_CACHE}/integration.key
      3. Generate new key, persist it, return it
    """
    env_key = os.getenv("INTEGRATION_ENCRYPTION_KEY", "").strip()
    if env_key:
        return env_key.encode()

    cache_dir = os.getenv("AGENT_CACHE", _DEFAULT_CACHE_DIR)
    key_path = Path(cache_dir) / "integration.key"
    key_path.parent.mkdir(parents=True, exist_ok=True)

    if key_path.exists():
        try:
            return key_path.read_bytes().strip()
        except OSError as e:
            logger.warning("Failed to read integration key file %s: %s", key_path, e)

    key = Fernet.generate_key()
    try:
        key_path.write_bytes(key)
        key_path.chmod(0o600)
    except OSError as e:
        logger.warning("Failed to persist integration key to %s: %s", key_path, e)
    return key


def _get_fernet() -> Fernet:
    return Fernet(_load_or_create_key())


def get_integration_fernet() -> Fernet:
    """Public accessor for the Fernet key used by the credential store."""
    return _get_fernet()


# ---------------------------------------------------------------------------
# Public CRUD — delegates to the Postgres store
# ---------------------------------------------------------------------------


def get_credential(service: str, key: str) -> str:
    """Return the decrypted credential value, or empty string if not found.

    Defensive: returns ``""`` when Postgres is disabled so that
    ``get_slack_config()``-style startup readers don't crash the app.
    """
    # Local import keeps this module importable by tools / linters that
    # don't have psycopg installed.
    from unified_api.postgres_encrypted_credentials import (
        pg_get_credential,
        postgres_credentials_enabled,
    )

    if not postgres_credentials_enabled():
        return ""
    return pg_get_credential(service, key)


def set_credential(service: str, key: str, value: str) -> None:
    """Encrypt and upsert a credential. Deletes the row if ``value`` is empty."""
    from unified_api.postgres_encrypted_credentials import (
        pg_delete_credential,
        pg_set_credential,
    )

    if not value:
        pg_delete_credential(service, key)
        return
    pg_set_credential(service, key, value)


def delete_credential(service: str, key: str) -> None:
    """Remove a single credential row."""
    from unified_api.postgres_encrypted_credentials import pg_delete_credential

    pg_delete_credential(service, key)


def delete_service_credentials(service: str) -> None:
    """Remove all credentials for a service."""
    from unified_api.postgres_encrypted_credentials import pg_delete_service_credentials

    pg_delete_service_credentials(service)


# ---------------------------------------------------------------------------
# One-shot SQLite → Postgres migration helper
# ---------------------------------------------------------------------------


def _legacy_sqlite_path() -> Path:
    cache_dir = os.getenv("AGENT_CACHE", _DEFAULT_CACHE_DIR)
    return Path(cache_dir) / "integration_credentials.db"


def _read_sqlite_rows(db_path: Path) -> list[tuple[str, str, str]]:
    """Read ``(service, key, decrypted_plaintext)`` tuples from the legacy SQLite file.

    Returns an empty list if the file is missing, the table is absent,
    or every row fails to decrypt. Rows whose individual decrypt fails
    are logged at WARNING and skipped so one corrupt row doesn't block
    the rest of the migration.
    """
    import sqlite3

    if not db_path.exists():
        return []

    fernet = _get_fernet()
    out: list[tuple[str, str, str]] = []
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            rows = conn.execute("SELECT service, key, value FROM service_integrations").fetchall()
        except sqlite3.OperationalError as e:
            # Table missing or schema mismatch — nothing to migrate.
            logger.info("Legacy SQLite credentials table not found (%s); skipping migration", e)
            return []
        finally:
            conn.close()
    except sqlite3.Error as e:
        logger.warning("Could not open legacy SQLite credentials file %s: %s", db_path, e)
        return []

    for service, key, encrypted in rows:
        if not isinstance(encrypted, str) or not encrypted:
            continue
        try:
            plaintext = fernet.decrypt(encrypted.encode()).decode()
        except Exception as e:
            logger.warning(
                "Skipping corrupt/un-decryptable legacy credential %s/%s: %s",
                service,
                key,
                e,
            )
            continue
        out.append((service, key, plaintext))
    return out


def migrate_sqlite_to_postgres_once() -> dict[str, int | bool | str]:
    """Lift credentials from the legacy SQLite store into Postgres, once.

    Behaviour:
      * No-op and returns ``{"status": "skipped", "reason": ...}`` when
        Postgres is disabled, when the legacy SQLite file is missing, or
        when the ``migration_markers`` row already exists.
      * Otherwise opens a **single** ``shared_postgres.get_conn()``
        transaction, re-encrypts each row with the current Fernet key,
        upserts them via ``pg_upsert_credentials_bulk``, and inserts the
        ``integration_credentials_v1`` marker row in the **same**
        transaction so re-runs are idempotent and crashes mid-migration
        don't leave half-migrated state.

    Returns a small status dict suitable for logging:
      ``{"status": "migrated", "rows": N}`` on success,
      ``{"status": "skipped", "reason": "..."}`` when no work happened.
    """
    # Import late so tests and linters don't force a shared_postgres
    # import chain.
    from shared_postgres import get_conn, is_postgres_enabled
    from unified_api.postgres_encrypted_credentials import pg_upsert_credentials_bulk

    if not is_postgres_enabled():
        return {"status": "skipped", "reason": "postgres_disabled"}

    db_path = _legacy_sqlite_path()
    if not db_path.exists():
        return {"status": "skipped", "reason": "no_sqlite_file"}

    fernet = _get_fernet()
    plaintext_rows = _read_sqlite_rows(db_path)
    if not plaintext_rows:
        # Nothing to migrate, but still record the marker so we don't
        # re-read the SQLite file on every startup.
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO migration_markers (name, applied_at, detail) "
                "VALUES (%s, NOW(), %s) ON CONFLICT (name) DO NOTHING",
                (_MIGRATION_MARKER, "0 rows (empty sqlite)"),
            )
        return {"status": "skipped", "reason": "empty_sqlite"}

    # Re-encrypt once outside the transaction so the connection is held
    # for the shortest possible window.
    encrypted_rows = [
        (service, key, fernet.encrypt(plaintext.encode()).decode()) for service, key, plaintext in plaintext_rows
    ]

    with get_conn() as conn, conn.cursor() as cur:
        # Short-circuit inside the same connection: if another process
        # wrote the marker between is_postgres_enabled() and now, bail.
        cur.execute(
            "SELECT 1 FROM migration_markers WHERE name = %s",
            (_MIGRATION_MARKER,),
        )
        if cur.fetchone() is not None:
            return {"status": "skipped", "reason": "marker_present"}

        applied = pg_upsert_credentials_bulk(cur, encrypted_rows)
        cur.execute(
            "INSERT INTO migration_markers (name, applied_at, detail) "
            "VALUES (%s, NOW(), %s) ON CONFLICT (name) DO NOTHING",
            (_MIGRATION_MARKER, f"{applied} rows"),
        )

    logger.info(
        "integration_credentials SQLite → Postgres: migrated %d row(s) from %s",
        applied,
        db_path,
    )
    return {"status": "migrated", "rows": applied}
