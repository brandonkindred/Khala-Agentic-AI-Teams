"""
Encrypted credential store for service integrations (OAuth client IDs and secrets).

This module is the thin public API used by the rest of the unified API.
The Fernet key management lives here; all CRUD delegates to the Postgres
store in ``postgres_encrypted_credentials`` (the ``encrypted_integration_credentials``
table in Khala Postgres).

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

from cryptography.fernet import Fernet

logger = logging.getLogger(__name__)


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

    Delegates to ``shared_postgres.secrets`` so there is exactly ONE Fernet-key
    derivation across the platform: the unified API (here) and every team
    container (via ``shared_postgres``) derive the same key, which is what lets a
    secret written here be decrypted in a team container. Keeping two copies in
    sync by hand was the drift risk this removes.
    """
    from shared_postgres.secrets import load_or_create_key

    return load_or_create_key()


def _get_fernet() -> Fernet:
    """Return the one process-wide Fernet instance shared across the platform.

    Delegates to ``shared_postgres.secrets.get_fernet()`` so the unified API and
    every team container encrypt/decrypt with a single cached Fernet (built once
    from the same key) instead of each module rebuilding its own per call.
    """
    from shared_postgres.secrets import get_fernet

    return get_fernet()


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
