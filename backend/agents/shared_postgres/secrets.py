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
import tempfile
import threading
from pathlib import Path
from typing import Any, Optional

from shared_postgres.client import get_conn, is_postgres_enabled
from shared_postgres.metrics import timed_query

logger = logging.getLogger(__name__)

_STORE = "shared_postgres_secrets"
_DEFAULT_CACHE_DIR = ".agent_cache"

_LOCK = threading.Lock()
_fernet = None  # cached cryptography.fernet.Fernet


def load_or_create_key() -> bytes:
    """Return the Fernet key, matching ``unified_api`` precedence exactly.

    Priority:
      1. ``INTEGRATION_ENCRYPTION_KEY`` env var (base64-url-safe 32-byte key)
      2. Persisted key file at ``{AGENT_CACHE}/integration.key``
      3. Generate a new key, persist it atomically, return it

    Preconditions: none.
    Postconditions: returns a valid 32-byte url-safe base64 Fernet key as
        ``bytes``; the same key every call within a process (the file/env are
        stable). A pre-existing, unreadable or empty key file raises
        ``RuntimeError`` rather than silently generating a NEW key (which would
        destroy every existing encrypted secret). When no key exists it is created
        atomically (written to a temp file then linked into place) so concurrent
        first-starts converge on one key instead of last-writer-wins clobbering
        each other's, and a concurrent reader never observes a partial/empty file.
    """
    env_key = os.getenv("INTEGRATION_ENCRYPTION_KEY", "").strip()
    if env_key:
        return env_key.encode()

    from cryptography.fernet import Fernet

    cache_dir = os.getenv("AGENT_CACHE", _DEFAULT_CACHE_DIR)
    key_path = Path(cache_dir) / "integration.key"
    key_path.parent.mkdir(parents=True, exist_ok=True)

    if key_path.exists():
        return _read_key_file(key_path)

    # No persisted key and no env override: auto-generate for dev convenience, but
    # warn — a generated key lives only on this container's volume, so a multi-
    # container production deployment must set INTEGRATION_ENCRYPTION_KEY or each
    # container would derive its own key and be unable to read the others' secrets.
    key = Fernet.generate_key()
    logger.warning(
        "No INTEGRATION_ENCRYPTION_KEY set and no key file at %s; generating one. "
        "Set INTEGRATION_ENCRYPTION_KEY in production so every container shares one key.",
        key_path,
    )
    return _persist_new_key(key_path, key)


def _read_key_file(key_path: Path) -> bytes:
    """Read and validate the persisted Fernet key.

    Preconditions: ``key_path`` exists.
    Postconditions: returns the non-empty, stripped key bytes. Raises
        ``RuntimeError`` when the file is unreadable or empty rather than silently
        falling through to generate a NEW key — overwriting an existing key would
        render every row already encrypted under the old key permanently
        undecryptable, so an operator must fix permissions / restore the file
        instead. Our own writer publishes the final path only once fully written
        (atomic link/replace), so an empty read implies an out-of-band/corrupt file.
    """
    try:
        data = key_path.read_bytes().strip()
    except OSError as e:
        raise RuntimeError("Integration key file exists but is unreadable") from e
    if not data:
        raise RuntimeError(f"Integration key file at {key_path} is empty")
    return data


def _persist_new_key(key_path: Path, key: bytes) -> bytes:
    """Atomically persist a freshly generated Fernet key, converging on one winner.

    Writes ``key`` to a unique temp file in the same directory (fsynced), then
    hard-links it into place at ``key_path``. ``os.link`` is atomic and fails with
    ``FileExistsError`` when the final path already exists, so when several
    containers first-start against a shared volume exactly one wins the create and
    the losers adopt the winner's key instead of clobbering it last-writer-wins.
    Because the final path only ever appears as a fully written file (never the
    in-progress temp), a concurrent reader can never observe a partial/empty key.

    Preconditions: ``key_path`` did not exist when the caller checked; ``key`` is a
        valid Fernet key; ``key_path.parent`` exists.
    Postconditions: returns the key now persisted at ``key_path`` — the one this
        process wrote, or, on a lost create race, the winner's key read back from
        disk. On a filesystem that cannot persist it (read-only volume, or no
        hardlink support and the atomic-replace fallback also fails) the in-memory
        ``key`` is returned with a warning so the dev case still works (it just
        won't survive a restart). Never raises for an I/O failure.
    """
    tmp_path: Optional[Path] = None
    try:
        fd, tmp_name = tempfile.mkstemp(
            dir=str(key_path.parent), prefix=".integration.key.", suffix=".tmp"
        )
        tmp_path = Path(tmp_name)
        with os.fdopen(fd, "wb") as f:
            f.write(key)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp_path, 0o600)
    except OSError as e:
        logger.warning("Failed to stage integration key near %s: %s", key_path, e)
        if tmp_path is not None:
            _unlink_quietly(tmp_path)
        return key

    try:
        os.link(tmp_path, key_path)  # atomic create-if-absent; readers never see a partial file
        return key
    except FileExistsError:
        # Another process won the create between the caller's exists() check and
        # now; adopt its key so every container converges on a single key.
        return _read_key_file(key_path)
    except OSError:
        # Filesystem without hardlink support: fall back to an atomic rename. This
        # is last-writer-wins under a true concurrent first-start, but still never
        # exposes a partial file to a reader.
        try:
            os.replace(tmp_path, key_path)
            tmp_path = None  # replace consumed the temp; nothing to clean up
            return key
        except OSError as e:
            logger.warning("Failed to persist integration key to %s: %s", key_path, e)
            return key
    finally:
        if tmp_path is not None:
            _unlink_quietly(tmp_path)


def _unlink_quietly(path: Path) -> None:
    """Best-effort removal of a temp file; never raises.

    Postconditions: ``path`` is gone if it existed and was removable; any OSError
        (already gone, permissions) is swallowed.
    """
    try:
        path.unlink()
    except OSError:
        pass


# Back-compat private alias: callers that imported the underscore-prefixed name
# still work, but new code (and ``unified_api``) should import the public
# ``load_or_create_key``.
_load_or_create_key = load_or_create_key


def _get_fernet():
    """Return the process-wide cached Fernet instance (lazy).

    Preconditions: none.
    Postconditions: returns the single ``cryptography.fernet.Fernet`` for this
        process (built once from :func:`load_or_create_key`) — the same instance
        every call. Never raises for a missing key file; it is created.
    """
    global _fernet
    if _fernet is not None:
        return _fernet
    with _LOCK:
        if _fernet is None:
            from cryptography.fernet import Fernet

            _fernet = Fernet(load_or_create_key())
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


# Canonical DDL for the shared secrets table. This store is shared infrastructure,
# so it self-heals the table on first use: a team container may read runtime config
# before the unified API has run its migration. ``unified_api/postgres`` imports
# this same constant for its registered schema, so there is exactly one CREATE
# TABLE definition; ``CREATE TABLE IF NOT EXISTS`` is idempotent.
SECRETS_TABLE_DDL = (
    "CREATE TABLE IF NOT EXISTS encrypted_integration_credentials ("
    "service TEXT NOT NULL, "
    "credential_key TEXT NOT NULL, "
    "ciphertext TEXT NOT NULL, "
    "updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), "
    "PRIMARY KEY (service, credential_key))"
)
_table_ensured = False


def _ensure_table() -> None:
    """Idempotently create the shared secrets table on first use.

    Preconditions: none (no-op when Postgres is disabled).
    Postconditions: the ``encrypted_integration_credentials`` table exists after a
        successful call; success is cached process-wide so later calls are free. A
        DDL failure is logged and retried on the next call. Never raises.
    """
    global _table_ensured
    if _table_ensured or not is_postgres_enabled():
        return
    with _LOCK:
        if _table_ensured:
            return
        try:
            with get_conn() as conn, conn.cursor() as cur:
                cur.execute(SECRETS_TABLE_DDL)
            _table_ensured = True
        except Exception as e:  # noqa: BLE001 - reads still fall back to ""; retried next call
            logger.warning("shared secrets table ensure failed: %s", e)


@timed_query(store=_STORE, op="get_secret")
def get_secret(service: str, key: str) -> str:
    """Return the decrypted secret for ``(service, key)``, or ``""``.

    Preconditions: ``service`` and ``key`` are non-empty strings.
    Postconditions: returns the decrypted plaintext, or ``""`` when the row is
        missing, Postgres is disabled, or decryption fails (logged). Never raises
        for an absent value.
    """
    if not service or not key:
        raise ValueError("service and key must be non-empty")
    if not is_postgres_enabled():
        return ""
    _ensure_table()
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
    if not service:
        raise ValueError("service must be non-empty")
    wanted = {k for k in keys if k}
    if not wanted or not is_postgres_enabled():
        return {}
    _ensure_table()
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


# Single home for the credential-row SQL so the single-key (set_secret), batch
# (set_secrets), and delete (delete_secret) paths share one statement and can't
# drift apart when the table shape changes.
_UPSERT_SECRET_SQL = """
    INSERT INTO encrypted_integration_credentials (service, credential_key, ciphertext, updated_at)
    VALUES (%s, %s, %s, NOW())
    ON CONFLICT (service, credential_key)
    DO UPDATE SET ciphertext = EXCLUDED.ciphertext, updated_at = NOW()
"""
_DELETE_SECRET_SQL = (
    "DELETE FROM encrypted_integration_credentials WHERE service = %s AND credential_key = %s"
)


def _upsert_secret_row(cur: Any, service: str, key: str, value: str) -> None:
    """Encrypt ``value`` and stage an upsert of ``(service, key)`` on ``cur``.

    Preconditions: ``cur`` is an open cursor whose connection's transaction the
        caller owns (commit/rollback happens at the caller's ``get_conn`` exit);
        ``service``/``key``/``value`` are non-empty strings.
    Postconditions: one upsert is staged on ``cur`` (not committed here); the
        plaintext ``value`` is never stored — only its Fernet ciphertext.
    """
    encrypted = _get_fernet().encrypt(value.encode()).decode()
    cur.execute(_UPSERT_SECRET_SQL, (service, key, encrypted))


def _delete_secret_row(cur: Any, service: str, key: str) -> None:
    """Stage a delete of the ``(service, key)`` row on ``cur``.

    Preconditions: ``cur`` is an open cursor whose connection's transaction the
        caller owns; ``service``/``key`` are non-empty strings.
    Postconditions: one delete is staged on ``cur`` (not committed here); a no-op
        at the SQL level when the row is absent.
    """
    cur.execute(_DELETE_SECRET_SQL, (service, key))


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
    if not service or not key:
        raise ValueError("service and key must be non-empty")
    if not is_postgres_enabled():
        raise RuntimeError("POSTGRES_HOST is not set; cannot persist shared secrets.")
    _ensure_table()
    if not value:
        delete_secret(service, key)
        return
    with get_conn() as conn, conn.cursor() as cur:
        _upsert_secret_row(cur, service, key, value)


@timed_query(store=_STORE, op="set_secrets")
def set_secrets(service: str, values: "dict[str, str]") -> None:
    """Encrypt and write several keys of one ``service`` in ONE transaction.

    Atomic, transactional counterpart to :func:`set_secret`: every entry in
    ``values`` is applied (non-empty value -> encrypted upsert, empty value ->
    delete) inside a single pooled connection, so the whole batch commits
    together or — on any failure — rolls back together. Use this when several
    related keys must move as a unit (e.g. the LLM-config endpoint switching
    provider + model + API key together), so a mid-write error can never leave a
    partially-applied config behind.

    Preconditions: ``service`` is a non-empty string; every key in ``values`` is
        a non-empty string. An empty ``values`` mapping is a no-op (returns
        without touching Postgres). When ``values`` is non-empty, Postgres must be
        enabled (``POSTGRES_HOST`` set) — raises ``RuntimeError`` otherwise so the
        caller fails loudly rather than silently dropping the write.
    Postconditions: on success every ``(service, key)`` in ``values`` reflects its
        new state (upserted or, for an empty value, removed). On any failure the
        transaction is rolled back — no key is changed — and the exception
        propagates. The batch is all-or-nothing.
    """
    if not service:
        raise ValueError("service must be non-empty")
    if not all(key for key in values):
        raise ValueError("every secret key must be non-empty")
    if not values:
        return
    if not is_postgres_enabled():
        raise RuntimeError("POSTGRES_HOST is not set; cannot persist shared secrets.")
    _ensure_table()
    with get_conn() as conn, conn.cursor() as cur:
        for key, value in values.items():
            if value:
                _upsert_secret_row(cur, service, key, value)
            else:
                _delete_secret_row(cur, service, key)


@timed_query(store=_STORE, op="delete_secret")
def delete_secret(service: str, key: str) -> None:
    """Remove the secret row for ``(service, key)``.

    Preconditions: ``service`` and ``key`` are non-empty strings.
    Postconditions: the row is removed if present; a no-op when Postgres is
        disabled. Never raises for an absent row.
    """
    if not service or not key:
        raise ValueError("service and key must be non-empty")
    if not is_postgres_enabled():
        return
    _ensure_table()
    try:
        with get_conn() as conn, conn.cursor() as cur:
            _delete_secret_row(cur, service, key)
    except Exception as e:  # noqa: BLE001 - delete is best-effort
        logger.warning("shared secret delete failed (%s/%s): %s", service, key, e)


def _reset_fernet_for_testing() -> None:
    """Drop the cached Fernet + table-ensured flag so a test can swap key/env. Tests only."""
    global _fernet, _table_ensured
    with _LOCK:
        _fernet = None
        _table_ensured = False
