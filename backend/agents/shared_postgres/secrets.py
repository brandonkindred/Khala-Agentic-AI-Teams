"""Shared Fernet-encrypted secret store over ``encrypted_integration_credentials``.

This is the cross-container read/write layer for small operator-managed secrets
(currently the runtime LLM provider configuration). It deliberately mirrors the
key management and table shape of ``unified_api/integration_credentials.py`` +
``unified_api/postgres_encrypted_credentials.py`` so the two interoperate — same
``encrypted_integration_credentials`` table, same Fernet key precedence — but it
lives in ``shared_postgres`` so **team containers can read secrets without
importing ``unified_api``** (which they must never do).

Why a separate module instead of reusing the unified_api one: agent teams run in
their own containers and only depend on ``shared_*`` packages. As long as every
container shares the same Postgres and the same Fernet key (the
``INTEGRATION_ENCRYPTION_KEY`` env var, or the ``integration.key`` file on the
shared ``agents_data`` volume), a value written by the unified_api endpoint is
readable by every team.

Invariants:
    - All ciphertext is Fernet tokens produced by the process-wide key.
    - Every operation is a no-op / empty-string when Postgres is disabled
      (``POSTGRES_HOST`` unset), so non-Postgres dev and tests are unaffected.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Optional

from shared_postgres.client import get_conn, is_postgres_enabled
from shared_postgres.metrics import timed_query

logger = logging.getLogger(__name__)

_STORE = "shared_postgres_secrets"
_DEFAULT_CACHE_DIR = ".agent_cache"

_LOCK = threading.Lock()
_fernet = None  # cached cryptography.fernet.Fernet


def _load_or_create_key() -> bytes:
    """Return the Fernet key, matching ``unified_api`` precedence exactly.

    Priority:
      1. ``INTEGRATION_ENCRYPTION_KEY`` env var (base64-url-safe 32-byte key)
      2. Persisted key file at ``{AGENT_CACHE}/integration.key``
      3. Generate a new key, persist it, return it

    Preconditions: none.
    Postconditions: returns a valid 32-byte url-safe base64 Fernet key as
        ``bytes``; the same key every call within a process (the file/env are
        stable). Never raises for a missing file — it is created.
    """
    env_key = os.getenv("INTEGRATION_ENCRYPTION_KEY", "").strip()
    if env_key:
        return env_key.encode()

    from cryptography.fernet import Fernet

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


def _get_fernet():
    """Return the process-wide cached Fernet instance (lazy)."""
    global _fernet
    if _fernet is not None:
        return _fernet
    with _LOCK:
        if _fernet is None:
            from cryptography.fernet import Fernet

            _fernet = Fernet(_load_or_create_key())
    return _fernet


def get_fernet():
    """Public accessor for the process-wide Fernet used by the secret store.

    This is the single Fernet-key derivation for the platform; ``unified_api``'s
    credential modules delegate here so there is exactly one key implementation
    (env ``INTEGRATION_ENCRYPTION_KEY`` -> ``$AGENT_CACHE/integration.key``) shared
    by the unified API and every team container.

    Postconditions: returns a ready ``cryptography.fernet.Fernet``.
    """
    return _get_fernet()


@timed_query(store=_STORE, op="get_secret")
def get_secret(service: str, key: str) -> str:
    """Return the decrypted secret for ``(service, key)``, or ``""``.

    Preconditions: ``service`` and ``key`` are non-empty strings.
    Postconditions: returns the decrypted plaintext, or ``""`` when the row is
        missing, Postgres is disabled, or decryption fails (logged). Never raises
        for an absent value.
    """
    assert service and key, "service and key must be non-empty"
    if not is_postgres_enabled():
        return ""
    row: Optional[tuple] = None
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT ciphertext FROM encrypted_integration_credentials "
                "WHERE service = %s AND credential_key = %s",
                (service, key),
            )
            row = cur.fetchone()
    except Exception as e:  # noqa: BLE001 - read must never crash a caller
        logger.warning("shared secret read failed (%s/%s): %s", service, key, e)
        return ""
    if not row:
        return ""
    try:
        return _get_fernet().decrypt(row[0].encode()).decode()
    except Exception as e:  # noqa: BLE001 - corrupt/foreign ciphertext
        logger.error("Failed to decrypt shared secret %s/%s: %s", service, key, e)
        return ""


@timed_query(store=_STORE, op="get_secrets")
def get_secrets(service: str, keys: "list[str] | tuple[str, ...]") -> dict[str, str]:
    """Return decrypted values for several keys of one ``service`` in ONE query.

    Batched counterpart to :func:`get_secret` — a single
    ``SELECT … WHERE service = %s`` round-trip instead of one per key.

    Preconditions: ``service`` is non-empty; ``keys`` is a non-empty iterable of
        non-empty strings.
    Postconditions: returns a dict mapping each requested key present in the store
        to its decrypted plaintext; absent keys and keys whose ciphertext fails to
        decrypt (logged) are omitted. Returns ``{}`` when Postgres is disabled or
        the read fails. Never raises for absent values.
    """
    assert service, "service must be non-empty"
    wanted = {k for k in keys if k}
    if not wanted or not is_postgres_enabled():
        return {}
    rows: list[tuple] = []
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT credential_key, ciphertext FROM encrypted_integration_credentials "
                "WHERE service = %s AND credential_key = ANY(%s)",
                (service, list(wanted)),
            )
            rows = cur.fetchall()
    except Exception as e:  # noqa: BLE001 - read must never crash a caller
        logger.warning("shared secrets batch read failed (%s): %s", service, e)
        return {}
    fernet = _get_fernet()
    out: dict[str, str] = {}
    for key, ciphertext in rows:
        try:
            out[key] = fernet.decrypt(ciphertext.encode()).decode()
        except Exception as e:  # noqa: BLE001 - corrupt/foreign ciphertext
            logger.error("Failed to decrypt shared secret %s/%s: %s", service, key, e)
    return out


@timed_query(store=_STORE, op="set_secret")
def set_secret(service: str, key: str, value: str) -> None:
    """Encrypt and upsert ``value`` for ``(service, key)``; delete when empty.

    Preconditions: ``service`` and ``key`` are non-empty strings; Postgres is
        enabled (``POSTGRES_HOST`` set) — raises ``RuntimeError`` otherwise so the
        caller (the settings endpoint) fails loudly rather than silently dropping
        the write.
    Postconditions: a non-empty ``value`` is stored encrypted (insert or update);
        an empty ``value`` removes the row.
    """
    assert service and key, "service and key must be non-empty"
    if not is_postgres_enabled():
        raise RuntimeError("POSTGRES_HOST is not set; cannot persist shared secrets.")
    if not value:
        delete_secret(service, key)
        return
    encrypted = _get_fernet().encrypt(value.encode()).decode()
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO encrypted_integration_credentials (service, credential_key, ciphertext, updated_at)
            VALUES (%s, %s, %s, NOW())
            ON CONFLICT (service, credential_key)
            DO UPDATE SET ciphertext = EXCLUDED.ciphertext, updated_at = NOW()
            """,
            (service, key, encrypted),
        )


@timed_query(store=_STORE, op="delete_secret")
def delete_secret(service: str, key: str) -> None:
    """Remove the secret row for ``(service, key)``.

    Preconditions: ``service`` and ``key`` are non-empty strings.
    Postconditions: the row is removed if present; a no-op when Postgres is
        disabled. Never raises for an absent row.
    """
    assert service and key, "service and key must be non-empty"
    if not is_postgres_enabled():
        return
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "DELETE FROM encrypted_integration_credentials "
                "WHERE service = %s AND credential_key = %s",
                (service, key),
            )
    except Exception as e:  # noqa: BLE001 - delete is best-effort
        logger.warning("shared secret delete failed (%s/%s): %s", service, key, e)


@timed_query(store=_STORE, op="delete_service_secrets")
def delete_service_secrets(service: str) -> None:
    """Remove every secret row for ``service``.

    Preconditions: ``service`` is a non-empty string.
    Postconditions: all rows for ``service`` are removed; a no-op when Postgres is
        disabled. Never raises.
    """
    assert service, "service must be non-empty"
    if not is_postgres_enabled():
        return
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "DELETE FROM encrypted_integration_credentials WHERE service = %s",
                (service,),
            )
    except Exception as e:  # noqa: BLE001 - delete is best-effort
        logger.warning("shared service secret delete failed (%s): %s", service, e)


def _reset_fernet_for_testing() -> None:
    """Drop the cached Fernet so a test can swap the key/env. Tests only."""
    global _fernet
    with _LOCK:
        _fernet = None
