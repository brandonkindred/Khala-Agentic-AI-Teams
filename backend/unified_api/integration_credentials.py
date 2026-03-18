"""
Encrypted credential store for service integrations (OAuth client IDs and secrets).

Credentials are stored in a SQLite database table with Fernet symmetric encryption.
The encryption key is read from INTEGRATION_ENCRYPTION_KEY env var; if not set, a key
is auto-generated and persisted to {AGENT_CACHE}/integration.key so it survives restarts.

DB schema (service_integrations table):
  service  TEXT  — e.g. "slack"
  key      TEXT  — e.g. "client_id" or "client_secret"
  value    TEXT  — Fernet-encrypted, base64-encoded ciphertext
  PRIMARY KEY (service, key)
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
from pathlib import Path

from cryptography.fernet import Fernet

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_DEFAULT_CACHE_DIR = ".agent_cache"

_DDL = """
CREATE TABLE IF NOT EXISTS service_integrations (
    service TEXT NOT NULL,
    key     TEXT NOT NULL,
    value   TEXT NOT NULL,
    PRIMARY KEY (service, key)
);
"""


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


# ---------------------------------------------------------------------------
# Database path and connection
# ---------------------------------------------------------------------------


def _get_db_path() -> Path:
    cache_dir = os.getenv("AGENT_CACHE", _DEFAULT_CACHE_DIR)
    path = Path(cache_dir)
    path.mkdir(parents=True, exist_ok=True)
    return path / "integration_credentials.db"


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_get_db_path()), check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(_DDL)
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_credential(service: str, key: str) -> str:
    """Return the decrypted credential value, or empty string if not found."""
    with _LOCK:
        conn = _get_conn()
        try:
            row = conn.execute(
                "SELECT value FROM service_integrations WHERE service = ? AND key = ?",
                (service, key),
            ).fetchone()
        finally:
            conn.close()

    if not row:
        return ""
    try:
        return _get_fernet().decrypt(row[0].encode()).decode()
    except Exception as e:
        logger.error("Failed to decrypt credential %s/%s: %s", service, key, e)
        return ""


def set_credential(service: str, key: str, value: str) -> None:
    """Encrypt and upsert a credential. Deletes the row if value is empty."""
    if not value:
        delete_credential(service, key)
        return
    encrypted = _get_fernet().encrypt(value.encode()).decode()
    with _LOCK:
        conn = _get_conn()
        try:
            conn.execute(
                "INSERT INTO service_integrations (service, key, value) VALUES (?, ?, ?)"
                " ON CONFLICT(service, key) DO UPDATE SET value = excluded.value",
                (service, key, encrypted),
            )
            conn.commit()
        finally:
            conn.close()


def delete_credential(service: str, key: str) -> None:
    """Remove a single credential row."""
    with _LOCK:
        conn = _get_conn()
        try:
            conn.execute(
                "DELETE FROM service_integrations WHERE service = ? AND key = ?",
                (service, key),
            )
            conn.commit()
        finally:
            conn.close()


def delete_service_credentials(service: str) -> None:
    """Remove all credentials for a service."""
    with _LOCK:
        conn = _get_conn()
        try:
            conn.execute("DELETE FROM service_integrations WHERE service = ?", (service,))
            conn.commit()
        finally:
            conn.close()
