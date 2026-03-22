"""
Postgres-backed encrypted integration secrets (Fernet), same key material as SQLite store.

Used when POSTGRES_HOST is set (e.g. docker-compose stack). Table is created on first use.
"""

from __future__ import annotations

import logging
import os
import threading
import urllib.parse

from unified_api.integration_credentials import get_integration_fernet

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_psycopg_module = None
_psycopg_import_failed: bool = False


def _get_psycopg():
    """Lazy import psycopg (optional at dev time; required in Docker when POSTGRES_HOST is set)."""
    global _psycopg_module, _psycopg_import_failed
    if _psycopg_module is not None:
        return _psycopg_module
    if _psycopg_import_failed:
        return None
    try:
        import psycopg

        _psycopg_module = psycopg
        return psycopg
    except ModuleNotFoundError as e:
        _psycopg_import_failed = True
        logger.warning(
            "psycopg is not installed (%s). Postgres encrypted credentials are unavailable; "
            "install psycopg[binary] (see agents/requirements.txt) or unset POSTGRES_HOST.",
            e,
        )
        return None

_DDL = """
CREATE TABLE IF NOT EXISTS encrypted_integration_credentials (
    service TEXT NOT NULL,
    credential_key TEXT NOT NULL,
    ciphertext TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (service, credential_key)
);
"""


def postgres_credentials_enabled() -> bool:
    return bool(os.getenv("POSTGRES_HOST", "").strip())


def _dsn() -> str:
    host = os.environ["POSTGRES_HOST"].strip()
    user = os.getenv("POSTGRES_USER", "postgres").strip()
    password = os.getenv("POSTGRES_PASSWORD", "")
    db = os.getenv("POSTGRES_DB", "postgres").strip()
    port = os.getenv("POSTGRES_PORT", "5432").strip()
    pwd = urllib.parse.quote_plus(password)
    return f"postgresql://{user}:{pwd}@{host}:{port}/{db}"


def _ensure_table(cur) -> None:
    cur.execute(_DDL)


def pg_get_credential(service: str, key: str) -> str:
    """Return decrypted plaintext or empty string."""
    if not postgres_credentials_enabled():
        return ""
    row: tuple | None = None
    with _LOCK:
        import psycopg

        try:
            with psycopg.connect(_dsn(), autocommit=True) as conn, conn.cursor() as cur:
                _ensure_table(cur)
                cur.execute(
                    "SELECT ciphertext FROM encrypted_integration_credentials "
                    "WHERE service = %s AND credential_key = %s",
                    (service, key),
                )
                row = cur.fetchone()
        except Exception as e:
            logger.warning("Postgres credential read failed (%s/%s): %s", service, key, e)
            return ""

    if not row:
        return ""
    try:
        return get_integration_fernet().decrypt(row[0].encode()).decode()
    except Exception as e:
        logger.error("Failed to decrypt Postgres credential %s/%s: %s", service, key, e)
        return ""


def pg_set_credential(service: str, key: str, value: str) -> None:
    if not postgres_credentials_enabled():
        raise RuntimeError("POSTGRES_HOST is not set; cannot use Postgres credential store.")
    psycopg = _get_psycopg()
    if psycopg is None:
        raise RuntimeError(
            "psycopg is not installed; cannot use Postgres credential store. "
            "Install psycopg[binary] (pip install 'psycopg[binary]') or unset POSTGRES_HOST."
        )
    if not value:
        pg_delete_credential(service, key)
        return
    encrypted = get_integration_fernet().encrypt(value.encode()).decode()

    with _LOCK, psycopg.connect(_dsn(), autocommit=True) as conn, conn.cursor() as cur:
        _ensure_table(cur)
        cur.execute(
            """
                    INSERT INTO encrypted_integration_credentials (service, credential_key, ciphertext, updated_at)
                    VALUES (%s, %s, %s, NOW())
                    ON CONFLICT (service, credential_key)
                    DO UPDATE SET ciphertext = EXCLUDED.ciphertext, updated_at = NOW()
                    """,
            (service, key, encrypted),
        )


def pg_delete_credential(service: str, key: str) -> None:
    if not postgres_credentials_enabled():
        return
    psycopg = _get_psycopg()
    if psycopg is None:
        return

    with _LOCK:
        try:
            with psycopg.connect(_dsn(), autocommit=True) as conn, conn.cursor() as cur:
                _ensure_table(cur)
                cur.execute(
                    "DELETE FROM encrypted_integration_credentials WHERE service = %s AND credential_key = %s",
                    (service, key),
                )
        except Exception as e:
            logger.warning("Postgres credential delete failed (%s/%s): %s", service, key, e)
