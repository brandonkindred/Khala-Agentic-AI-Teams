"""
Postgres-backed encrypted integration secrets (Fernet).

The canonical store for Slack/Medium/Google-browser-login credentials.
Uses the ``encrypted_integration_credentials`` table which is created at
unified_api startup by ``shared_postgres.register_team_schemas`` (see
``unified_api/postgres/__init__.py``) — no per-call DDL here.

Every public operation is wrapped in ``@timed_query`` so production can
grep structured log lines for slow reads or writes without needing a
Prometheus exporter.
"""

from __future__ import annotations

import logging
import os
import threading

from shared_postgres import dsn as _shared_dsn
from shared_postgres import statement_timeout_ms
from shared_postgres.metrics import timed_query
from unified_api.integration_credentials import get_integration_fernet

logger = logging.getLogger(__name__)

_STORE = "unified_api_credentials"

# This module opens a fresh psycopg connection per call (no pool). ``_LOCK``
# serializes those connects so at most one connection to the credential store is
# open at a time, bounding connection count under load. The trade-off: during a
# Postgres outage, concurrent reads queue and each waits up to the connect timeout
# (``POSTGRES_CONNECT_TIMEOUT_S``), so the Nth caller can block ~N×timeout. That is
# accepted here because credential reads are infrequent (config pages, run/review
# triggers) and the lock's connection-count guarantee matters more than peak
# concurrency on this path. Each connection also carries a ``statement_timeout``
# (see ``_statement_timeout_options``) so a query that stalls *after* connect can't
# pin ``_LOCK`` indefinitely and cascade across every credential consumer.
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


def postgres_credentials_enabled() -> bool:
    return bool(os.getenv("POSTGRES_HOST", "").strip())


def _dsn() -> str:
    """Return the shared libpq keyword DSN for the credential store.

    Preconditions: ``POSTGRES_HOST`` is set (callers gate on
        :func:`postgres_credentials_enabled`).
    Postconditions: delegates to ``shared_postgres.dsn()`` so this direct (un-pooled)
        connection uses the EXACT same builder as the shared pool — every field escaped
        via ``psycopg.conninfo.make_conninfo`` (so a ``POSTGRES_USER``/``POSTGRES_PASSWORD``
        containing ``@``, ``:``, spaces, or other special chars is handled identically)
        and carrying ``connect_timeout``. Having one builder means the reachability probe
        and this live read can't disagree because of escaping. Never raises.
    """
    return _shared_dsn()


def _statement_timeout_options() -> str:
    """libpq ``options`` that bound each credential query with ``statement_timeout``.

    Preconditions: none.
    Postconditions: returns ``-c statement_timeout={ms}`` from the shared
        :func:`shared_postgres.statement_timeout_ms` (default 5000, floor 0); returns
        ``""`` when set to 0 (disabled — WARNING: that removes the post-connect
        ``_LOCK``-release protection below). Sourcing the value from the shared helper
        keeps it in lockstep with the request-level ``wait_for`` budgets that size
        themselves off the same number. Scoped to the credential store's OWN connections
        so a query that stalls *after* connect (which ``connect_timeout`` does not cover)
        errors out and releases ``_LOCK`` instead of pinning it across all credential
        consumers. The shared pool is intentionally NOT bounded this way (it would cap
        legitimate long team queries). Never raises.
    """
    ms = statement_timeout_ms()
    return f"-c statement_timeout={ms}" if ms > 0 else ""


@timed_query(store=_STORE, op="pg_get_credential")
def pg_get_credential_status(service: str, key: str) -> tuple[str, bool]:
    """Return ``(decrypted_value, store_reachable)``.

    The core read. ``store_reachable`` is ``False`` ONLY when the read failed because
    the store/connection itself errored, so a caller can tell a genuinely-absent
    credential ("", reachable) from a down store ("", not reachable) from a SINGLE
    read — no separate connectivity probe, and therefore no TOCTOU window between the
    read and the probe.

    Preconditions: none.
    Postconditions:
        - Disabled store (``POSTGRES_HOST`` unset) → ``("", True)``: that is a
          configuration state ("absent"), not an outage; callers gate "configured"
          via :func:`postgres_credentials_enabled` separately.
        - Connection/query error → ``("", False)``.
        - Row missing, or present-but-undecryptable → ``("", True)`` (store answered;
          the credential is effectively absent/unusable).
        - Row present and decryptable → ``(plaintext, True)``.
        Never raises.
    """
    if not postgres_credentials_enabled():
        return "", True
    row: tuple | None = None
    with _LOCK:
        import psycopg

        try:
            with (
                psycopg.connect(_dsn(), autocommit=True, options=_statement_timeout_options()) as conn,
                conn.cursor() as cur,
            ):
                cur.execute(
                    "SELECT ciphertext FROM encrypted_integration_credentials "
                    "WHERE service = %s AND credential_key = %s",
                    (service, key),
                )
                row = cur.fetchone()
        except Exception as e:
            logger.warning("Postgres credential read failed (%s/%s): %s", service, key, e)
            return "", False

    if not row:
        return "", True
    try:
        return get_integration_fernet().decrypt(row[0].encode()).decode(), True
    except Exception as e:
        logger.error("Failed to decrypt Postgres credential %s/%s: %s", service, key, e)
        return "", True


def pg_get_credential(service: str, key: str) -> str:
    """Return decrypted plaintext, or empty string when missing/disabled/unreachable.

    Preconditions: ``service`` and ``key`` are non-empty strings.
    Postconditions: returns the decrypted value, or ``""`` for every non-value state
        (disabled / missing / undecryptable / store unreachable) — i.e. it collapses
        :func:`pg_get_credential_status`'s reachability flag away. Thin wrapper over
        that single implementation (one DB round-trip) for callers that don't need to
        distinguish those states. Never raises.
    """
    return pg_get_credential_status(service, key)[0]


@timed_query(store=_STORE, op="pg_set_credential")
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

    with (
        _LOCK,
        psycopg.connect(_dsn(), autocommit=True, options=_statement_timeout_options()) as conn,
        conn.cursor() as cur,
    ):
        cur.execute(
            """
                    INSERT INTO encrypted_integration_credentials (service, credential_key, ciphertext, updated_at)
                    VALUES (%s, %s, %s, NOW())
                    ON CONFLICT (service, credential_key)
                    DO UPDATE SET ciphertext = EXCLUDED.ciphertext, updated_at = NOW()
                    """,
            (service, key, encrypted),
        )


@timed_query(store=_STORE, op="pg_delete_credential")
def pg_delete_credential(service: str, key: str) -> None:
    if not postgres_credentials_enabled():
        return
    psycopg = _get_psycopg()
    if psycopg is None:
        return

    with _LOCK:
        try:
            with (
                psycopg.connect(_dsn(), autocommit=True, options=_statement_timeout_options()) as conn,
                conn.cursor() as cur,
            ):
                cur.execute(
                    "DELETE FROM encrypted_integration_credentials WHERE service = %s AND credential_key = %s",
                    (service, key),
                )
        except Exception as e:
            logger.warning("Postgres credential delete failed (%s/%s): %s", service, key, e)


@timed_query(store=_STORE, op="pg_delete_service_credentials")
def pg_delete_service_credentials(service: str) -> None:
    """Remove every credential row for ``service``.

    Silent no-op when Postgres is disabled or psycopg is missing,
    matching the pattern of ``pg_delete_credential``.
    """
    if not postgres_credentials_enabled():
        return
    psycopg = _get_psycopg()
    if psycopg is None:
        return

    with _LOCK:
        try:
            with (
                psycopg.connect(_dsn(), autocommit=True, options=_statement_timeout_options()) as conn,
                conn.cursor() as cur,
            ):
                cur.execute(
                    "DELETE FROM encrypted_integration_credentials WHERE service = %s",
                    (service,),
                )
        except Exception as e:
            logger.warning("Postgres service credential delete failed (%s): %s", service, e)
