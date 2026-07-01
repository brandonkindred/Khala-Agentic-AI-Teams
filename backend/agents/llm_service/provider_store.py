"""Ordered multi-provider LLM configuration with usage-limit fallback state.

The LLM Provider settings UI lets an operator register **several** provider
entries (e.g. an Anthropic API entry and an Ollama Local entry), ordered from
most to least preferred. The effective default is the most-preferred entry that
is not currently usage-limited; when a provider returns a 429 the entry is marked
``limit_exceeded`` with a ``reset_at`` time, and selection skips it until that
time passes (then the record is reset and the provider is used again).

This module is the cross-container data layer for that list. It lives in
``llm_service`` (not ``unified_api``) so **every team container** can read the
ordered list on the hot ``get_client`` path and mark limit-state at call time,
exactly like :mod:`llm_service.runtime_config`. It depends only on
``shared_postgres`` (``get_conn`` / ``is_postgres_enabled`` / ``get_fernet`` /
``timed_query``) — never on ``unified_api``.

Storage is a dedicated ``llm_provider_configs`` table (not a JSON blob) so a
single-row ``UPDATE`` marks/clears limit-state without a lost-update-prone
read-modify-write of the whole list under concurrent writers. The API key column
holds a Fernet token produced by the one process-wide key
(``shared_postgres.secrets.get_fernet``) — the plaintext key is never stored.

Invariants:
    - ``api_key_ciphertext`` is always either ``""`` (no key) or a Fernet token
      decryptable by the process-wide key.
    - Every operation is a no-op / empty result when Postgres is disabled
      (``POSTGRES_HOST`` unset), so non-Postgres dev and tests fall back to the
      legacy flat-key / env configuration path unchanged.
    - ``reset_entry``/``mark_exhausted`` are idempotent single-row writes, safe
      under concurrent callers across containers (last-writer-wins).
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# Canonical DDL for the ordered-provider table. Kept here (the data layer) so the
# lazy self-heal below and the schema registered by ``unified_api/postgres`` apply
# the exact same definition and cannot drift — mirrors how ``SECRETS_TABLE_DDL``
# lives in ``shared_postgres.secrets``. ``CREATE TABLE IF NOT EXISTS`` is idempotent.
PROVIDER_TABLE_DDL = (
    "CREATE TABLE IF NOT EXISTS llm_provider_configs ("
    "id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY, "
    "label TEXT NOT NULL, "
    "provider TEXT NOT NULL, "
    "model TEXT NOT NULL DEFAULT '', "
    "base_url TEXT NOT NULL DEFAULT '', "
    "api_key_ciphertext TEXT NOT NULL DEFAULT '', "
    "sort_order INTEGER NOT NULL, "
    "limit_exceeded BOOLEAN NOT NULL DEFAULT FALSE, "
    "limit_type TEXT NOT NULL DEFAULT '', "
    "reset_at TIMESTAMPTZ, "
    "created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), "
    "updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())"
)
PROVIDER_TABLE_INDEX_DDL = (
    "CREATE INDEX IF NOT EXISTS ix_llm_provider_configs_order ON llm_provider_configs (sort_order)"
)
# The statements ``unified_api/postgres`` registers and this module self-heals.
PROVIDER_TABLE_STATEMENTS = (PROVIDER_TABLE_DDL, PROVIDER_TABLE_INDEX_DDL)
TABLE_NAME = "llm_provider_configs"

ENV_RUNTIME_TTL = "LLM_RUNTIME_CONFIG_TTL_S"
_DEFAULT_TTL_S = 30.0

_table_ensured = False
_ensure_lock = threading.Lock()

_cache_lock = threading.Lock()
_cache: Optional[list["ProviderEntry"]] = None
_cache_ts: float = 0.0


@dataclass(frozen=True)
class ProviderEntry:
    """A single configured LLM provider in the fallback list.

    ``api_key`` is the DECRYPTED key, populated only for internal consumers (the
    factory, which needs it to build a client). The API layer must never return
    it — it exposes ``bool(api_key)`` as an ``api_key_configured`` flag instead.

    Invariants:
        - ``provider`` is a lowercase provider id (``"ollama"`` / ``"claude"``).
        - ``reset_at`` is timezone-aware (UTC) when present, else ``None``.
        - ``limit_exceeded`` False implies ``reset_at`` is ignored by selection.
    """

    id: int
    label: str
    provider: str
    model: str
    base_url: str
    api_key: str
    sort_order: int
    limit_exceeded: bool
    limit_type: str
    reset_at: Optional[datetime]
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


def _utcnow() -> datetime:
    """Return the current UTC time (timezone-aware). Isolated for test patching."""
    return datetime.now(timezone.utc)


def _ttl_seconds() -> float:
    """Return the list-cache TTL in seconds (env override, defensive).

    Shares ``LLM_RUNTIME_CONFIG_TTL_S`` with :mod:`llm_service.runtime_config` so
    a UI change to either surface propagates to all containers within one window.

    Postconditions: returns a non-negative float; missing/unparseable env yields
        ``_DEFAULT_TTL_S``; a negative value floors to ``0.0``. Never raises.
    """
    raw = os.environ.get(ENV_RUNTIME_TTL)
    if not raw:
        return _DEFAULT_TTL_S
    try:
        return max(0.0, float(raw))
    except ValueError:
        return _DEFAULT_TTL_S


def _postgres_enabled() -> bool:
    """True when Postgres is configured. Lazy import so non-PG envs stay clean."""
    try:
        from shared_postgres import is_postgres_enabled

        return is_postgres_enabled()
    except Exception:  # noqa: BLE001 - shared_postgres optional at import time
        return False


def _ensure_table() -> None:
    """Idempotently create the provider table + index on first use.

    Self-heals the table so a team container that marks limit-state before the
    unified_api migration has run still works (mirrors
    ``shared_postgres.secrets._ensure_table``).

    Preconditions: none (no-op when Postgres is disabled).
    Postconditions: the ``llm_provider_configs`` table and its index exist after a
        successful call; success is cached process-wide. A DDL failure is logged
        and retried on the next call. Never raises.
    """
    global _table_ensured
    if _table_ensured or not _postgres_enabled():
        return
    with _ensure_lock:
        if _table_ensured:
            return
        try:
            from shared_postgres import get_conn

            with get_conn() as conn, conn.cursor() as cur:
                cur.execute(PROVIDER_TABLE_DDL)
                cur.execute(PROVIDER_TABLE_INDEX_DDL)
                conn.commit()
            _table_ensured = True
        except Exception as e:  # noqa: BLE001 - reads still fall back; retried next call
            logger.warning("llm_provider_configs table ensure failed: %s", e)


def _encrypt_key(plaintext: str) -> str:
    """Return the Fernet token for ``plaintext``, or ``""`` for an empty key.

    Reuses the single process-wide key (``get_fernet``) — never a second key path.

    Preconditions: ``plaintext`` is a string. Postconditions: ``""`` -> ``""``;
        otherwise a Fernet token decryptable by :func:`_decrypt_key`. Never stores
        plaintext. Raises only if the shared key cannot be derived (a hard
        misconfiguration the caller should surface, not swallow).
    """
    if not plaintext:
        return ""
    from shared_postgres import get_fernet

    return get_fernet().encrypt(plaintext.encode()).decode()


def _decrypt_key(ciphertext: str) -> str:
    """Return the plaintext for a Fernet ``ciphertext``, or ``""`` on absence/failure.

    Postconditions: ``""`` -> ``""``; a corrupt/foreign token logs and yields
        ``""`` (mirrors ``secrets.get_secret``) so one bad row never breaks the
        whole list read. Never raises.
    """
    if not ciphertext:
        return ""
    try:
        from shared_postgres import get_fernet

        return get_fernet().decrypt(ciphertext.encode()).decode()
    except Exception as e:  # noqa: BLE001 - corrupt/foreign ciphertext
        logger.error("Failed to decrypt LLM provider api key: %s", e)
        return ""


# Column list for SELECTs. The ORDER here is the single source of truth that
# ``_row_to_entry`` unpacks positionally — keep the two in lockstep when editing.
_SELECT_COLUMNS = (
    "id, label, provider, model, base_url, api_key_ciphertext, sort_order, "
    "limit_exceeded, limit_type, reset_at, created_at, updated_at"
)


def _row_to_entry(row: tuple) -> ProviderEntry:
    """Map a SELECT row (``_SELECT_COLUMNS`` order) to a :class:`ProviderEntry`.

    Unpacks the row into named locals in ``_SELECT_COLUMNS`` order so a column-order
    change surfaces here (a length mismatch raises) instead of silently mis-mapping
    by positional index.

    Postconditions: the api key is decrypted; ``reset_at`` is timezone-aware UTC
        when present. Never raises for a well-formed row.
    """
    (
        id_,
        label,
        provider,
        model,
        base_url,
        api_key_ciphertext,
        sort_order,
        limit_exceeded,
        limit_type,
        reset_at,
        created_at,
        updated_at,
    ) = row
    if reset_at is not None and reset_at.tzinfo is None:
        reset_at = reset_at.replace(tzinfo=timezone.utc)
    return ProviderEntry(
        id=int(id_),
        label=label,
        provider=(provider or "").lower().strip(),
        model=model or "",
        base_url=base_url or "",
        api_key=_decrypt_key(api_key_ciphertext or ""),
        sort_order=int(sort_order),
        limit_exceeded=bool(limit_exceeded),
        limit_type=limit_type or "",
        reset_at=reset_at,
        created_at=created_at,
        updated_at=updated_at,
    )


def _load_ordered_uncached() -> list[ProviderEntry]:
    """Read every provider entry ordered by ``sort_order`` (ascending). ``[]`` on failure.

    Postconditions: returns the full list most->least preferred; any error yields
        ``[]`` so the caller falls back to the legacy flat-key/env path. Never raises.
    """
    if not _postgres_enabled():
        return []
    _ensure_table()
    try:
        from shared_postgres import get_conn

        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT {_SELECT_COLUMNS} FROM llm_provider_configs "
                "ORDER BY sort_order ASC, id ASC"
            )
            rows = cur.fetchall()
    except Exception as e:  # noqa: BLE001 - read must never crash a caller
        logger.debug("llm_provider_configs read failed: %s", e)
        return []
    return [_row_to_entry(r) for r in rows]


def load_ordered_entries(*, use_cache: bool = True) -> list[ProviderEntry]:
    """Return the provider entries ordered most->least preferred (cached).

    Preconditions: none. Postconditions: returns a list (possibly empty) ordered
        by ``sort_order``; with ``use_cache`` the result is at most ``_ttl_seconds``
        old. Returns ``[]`` when Postgres is disabled (never serving a stale cache,
        so a disabled store always routes callers to the legacy path). Never raises
        (a read failure yields ``[]``).
    """
    if not _postgres_enabled():
        return []
    if not use_cache:
        return _load_ordered_uncached()
    global _cache, _cache_ts
    ttl = _ttl_seconds()
    with _cache_lock:
        if _cache is not None and (time.monotonic() - _cache_ts) < ttl:
            return list(_cache)
    fresh = _load_ordered_uncached()
    with _cache_lock:
        _cache = list(fresh)
        _cache_ts = time.monotonic()
    return list(fresh)


def clear_cache() -> None:
    """Drop the cached list so the next read reloads from the store.

    Postconditions: the next :func:`load_ordered_entries` reloads from Postgres.
        Safe to call when nothing is cached.
    """
    global _cache, _cache_ts
    with _cache_lock:
        _cache = None
        _cache_ts = 0.0


def get_entry(entry_id: int) -> Optional[ProviderEntry]:
    """Return the entry with ``entry_id`` (fresh read), or ``None`` when absent.

    Preconditions: ``entry_id`` is an int. Postconditions: returns the current
        stored entry or ``None``; never raises.
    """
    if not _postgres_enabled():
        return None
    _ensure_table()
    try:
        from shared_postgres import get_conn

        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT {_SELECT_COLUMNS} FROM llm_provider_configs WHERE id = %s",
                (entry_id,),
            )
            row = cur.fetchone()
    except Exception as e:  # noqa: BLE001 - read must never crash a caller
        logger.debug("llm_provider_configs get_entry failed: %s", e)
        return None
    return _row_to_entry(row) if row else None


def create_entry(
    *,
    label: str,
    provider: str,
    model: str = "",
    base_url: str = "",
    api_key: str = "",
) -> Optional[ProviderEntry]:
    """Append a provider entry at the end of the fallback list.

    The new ``sort_order`` is ``max(sort_order)+1`` (0 for the first entry),
    computed in the INSERT. Two appends racing in separate transactions can still
    read the same ``MAX`` and produce a duplicate ``sort_order``; this is harmless —
    ``reorder`` is the canonical ordering mechanism and selection /
    ``load_ordered_entries`` tie-break deterministically on ``sort_order ASC, id ASC``,
    so a transient duplicate only affects the relative order of two same-rank entries
    until the next reorder. No unique constraint is imposed (it would make a
    concurrent append fail rather than degrade gracefully).

    Preconditions: ``label`` and ``provider`` are non-empty; Postgres is enabled.
    Postconditions: the entry is persisted (api key Fernet-encrypted), the cache
        is cleared, and the created entry is returned. Raises ``RuntimeError`` when
        Postgres is disabled (the caller surfaces a 503) — a create has no
        meaningful no-op fallback, unlike a read.
    """
    if not label or not label.strip():
        raise ValueError("label must be non-empty")
    if not provider or not provider.strip():
        raise ValueError("provider must be non-empty")
    if not _postgres_enabled():
        raise RuntimeError("Postgres is not configured; cannot persist provider config")
    _ensure_table()
    ciphertext = _encrypt_key(api_key.strip())
    from shared_postgres import get_conn

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO llm_provider_configs "
            "(label, provider, model, base_url, api_key_ciphertext, sort_order) "
            "VALUES (%s, %s, %s, %s, %s, "
            "COALESCE((SELECT MAX(sort_order) + 1 FROM llm_provider_configs), 0)) "
            f"RETURNING {_SELECT_COLUMNS}",
            (
                label.strip(),
                provider.lower().strip(),
                model.strip(),
                base_url.strip(),
                ciphertext,
            ),
        )
        row = cur.fetchone()
        conn.commit()
    clear_cache()
    return _row_to_entry(row) if row else None


def update_entry(
    entry_id: int,
    *,
    label: Optional[str] = None,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
) -> Optional[ProviderEntry]:
    """Edit fields of one entry; ``None`` args leave the stored value untouched.

    An ``api_key`` of ``None`` keeps the existing key (so the UI can save other
    edits without re-entering it); an empty string ``""`` explicitly clears it.
    Editing provider/model/base_url/key clears any stale limit-state for the entry
    (a new key or model may have a fresh budget), mirroring the intent of the
    settings save.

    Preconditions: ``entry_id`` exists; Postgres enabled. Postconditions: the named
        fields are updated, the cache cleared, and the fresh entry returned (or
        ``None`` if the id vanished). Raises ``RuntimeError`` when Postgres disabled.
    """
    if not _postgres_enabled():
        raise RuntimeError("Postgres is not configured; cannot persist provider config")
    _ensure_table()
    sets: list[str] = []
    params: list[object] = []
    if label is not None:
        sets.append("label = %s")
        params.append(label.strip())
    if provider is not None:
        sets.append("provider = %s")
        params.append(provider.lower().strip())
    if model is not None:
        sets.append("model = %s")
        params.append(model.strip())
    if base_url is not None:
        sets.append("base_url = %s")
        params.append(base_url.strip())
    if api_key is not None:
        sets.append("api_key_ciphertext = %s")
        params.append(_encrypt_key(api_key.strip()))
    # Clear stale limit-state ONLY when a *connection-affecting* field changed
    # (provider/model/base_url/api_key): a rotated key or changed model may have an
    # unexhausted budget, and leaving it marked would needlessly skip the entry. A
    # cosmetic edit (e.g. label only) must NOT un-mark a still-rate-limited provider,
    # which would cause premature retries / failover.
    config_changed = any(v is not None for v in (provider, model, base_url, api_key))
    if config_changed:
        sets.extend(["limit_exceeded = FALSE", "limit_type = ''", "reset_at = NULL"])
    sets.append("updated_at = NOW()")
    from shared_postgres import get_conn

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            f"UPDATE llm_provider_configs SET {', '.join(sets)} WHERE id = %s "
            f"RETURNING {_SELECT_COLUMNS}",
            (*params, entry_id),
        )
        row = cur.fetchone()
        conn.commit()
    clear_cache()
    return _row_to_entry(row) if row else None


def delete_entry(entry_id: int) -> bool:
    """Remove an entry from the list.

    Preconditions: Postgres enabled. Postconditions: the row is gone and the cache
        cleared; returns True iff a row was deleted. Raises ``RuntimeError`` when
        Postgres disabled.
    """
    if not _postgres_enabled():
        raise RuntimeError("Postgres is not configured; cannot modify provider config")
    _ensure_table()
    from shared_postgres import get_conn

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM llm_provider_configs WHERE id = %s", (entry_id,))
        deleted = cur.rowcount > 0
        conn.commit()
    clear_cache()
    return deleted


class ReorderMismatchError(ValueError):
    """Raised when a reorder's ``ids`` are not an exact permutation of the live set."""


def reorder(entry_ids: "list[int] | tuple[int, ...]") -> None:
    """Reassign ``sort_order`` to match the given id order (0-based) in ONE txn.

    The id set is validated against the live rows *inside the same transaction*,
    holding a row-level lock (``SELECT ... FOR UPDATE``) so a concurrent
    create/delete can't slip between the check and the writes — the validation and
    the reassignment are atomic. ``entry_ids`` must be an exact permutation of the
    current ids (same length AND same members), else :class:`ReorderMismatchError`
    (a ``ValueError``) is raised and nothing is written.

    Preconditions: ``entry_ids`` lists the ids in the desired most->least preferred
        order; Postgres enabled. Postconditions: each id's ``sort_order`` equals its
        position; the whole reassignment commits atomically (a partial reorder can
        never persist); the cache is cleared. Raises ``RuntimeError`` when Postgres
        disabled, ``ReorderMismatchError`` when ``entry_ids`` is not a permutation.
    """
    if not _postgres_enabled():
        raise RuntimeError("Postgres is not configured; cannot modify provider config")
    _ensure_table()
    from shared_postgres import get_conn

    with get_conn() as conn, conn.cursor() as cur:
        # Lock the full row set for the duration of the transaction so the membership
        # check below can't race a concurrent insert/delete.
        cur.execute("SELECT id FROM llm_provider_configs FOR UPDATE")
        live_ids = [r[0] for r in cur.fetchall()]
        if len(entry_ids) != len(live_ids) or set(entry_ids) != set(live_ids):
            # Roll back the FOR UPDATE lock by raising (get_conn rolls back on error).
            raise ReorderMismatchError(
                "ids must be exactly the current set of provider ids (a permutation of the list)."
            )
        if entry_ids:
            # One bulk UPDATE (CASE id WHEN ... THEN <position>) instead of a write per
            # row: a single round-trip regardless of list size. ``entry_ids`` is an
            # exact permutation of the locked live set, so every id matches a CASE arm.
            when_clauses = " ".join(["WHEN %s THEN %s"] * len(entry_ids))
            placeholders = ", ".join(["%s"] * len(entry_ids))
            params: list = []
            for position, entry_id in enumerate(entry_ids):
                params.extend([entry_id, position])
            params.extend(entry_ids)
            cur.execute(
                f"UPDATE llm_provider_configs SET sort_order = CASE id {when_clauses} END, "
                f"updated_at = NOW() WHERE id IN ({placeholders})",
                params,
            )
        conn.commit()
    clear_cache()


def mark_exhausted(entry_id: int, *, limit_type: str, reset_at: datetime) -> None:
    """Mark one entry usage-limited until ``reset_at``.

    A single-row idempotent UPDATE — concurrent marks from multiple containers are
    last-writer-wins (both compute ~the same ``reset_at``), so no row locking is
    needed.

    Preconditions: ``reset_at`` is timezone-aware; Postgres enabled. Postconditions:
        the entry has ``limit_exceeded=TRUE``, the given ``limit_type`` and
        ``reset_at``; the cache is cleared so the next selection observes the mark.
        A read/write failure is logged and swallowed (marking must never crash the
        LLM call that triggered it).
    """
    if not _postgres_enabled():
        return
    _ensure_table()
    try:
        from shared_postgres import get_conn

        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE llm_provider_configs "
                "SET limit_exceeded = TRUE, limit_type = %s, reset_at = %s, updated_at = NOW() "
                "WHERE id = %s",
                (limit_type, reset_at, entry_id),
            )
            conn.commit()
    except Exception as e:  # noqa: BLE001 - marking must never crash the LLM call
        logger.warning("Failed to mark LLM provider %s exhausted: %s", entry_id, e)
    clear_cache()


def reset_entry(entry_id: int) -> None:
    """Clear the limit-state of one entry (its reset window has elapsed).

    Conditional on ``limit_exceeded=TRUE`` AND the *stored* ``reset_at`` actually
    being in the past (``reset_at <= NOW()``). The reset-time guard is what makes
    this safe across containers: a worker acting on a TTL-cached list with a
    now-expired ``reset_at`` must NOT clear a mark that another worker has since
    refreshed with a *future* ``reset_at`` (which would put a still-limited provider
    back into rotation). Comparing against the DB clock means such a stale reset
    matches zero rows and no-ops instead. (A ``NULL`` ``reset_at`` — a provider
    limited without a known window — never satisfies ``reset_at <= NOW()``, so it is
    left limited until explicitly re-marked or edited, matching
    :func:`select_active_entry`, which never treats a ``NULL`` window as expired.)

    Preconditions: Postgres enabled. Postconditions: the entry has
        ``limit_exceeded=FALSE``, ``limit_type=''``, ``reset_at=NULL`` iff it was
        marked with a now-expired window; a fresher future mark is preserved; the
        cache is cleared. A failure is logged and swallowed.
    """
    if not _postgres_enabled():
        return
    _ensure_table()
    try:
        from shared_postgres import get_conn

        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE llm_provider_configs "
                "SET limit_exceeded = FALSE, limit_type = '', reset_at = NULL, updated_at = NOW() "
                "WHERE id = %s AND limit_exceeded = TRUE AND reset_at IS NOT NULL AND reset_at <= NOW()",
                (entry_id,),
            )
            conn.commit()
    except Exception as e:  # noqa: BLE001 - reset must never crash selection
        logger.warning("Failed to reset LLM provider %s: %s", entry_id, e)
    clear_cache()


def select_active_entry(
    entries: "list[ProviderEntry]",
    *,
    now: Optional[datetime] = None,
    reset_expired: bool = True,
) -> Optional[ProviderEntry]:
    """Pure selection over a pre-loaded ``entries`` list (most->least preferred).

    Returns the first entry that is not usage-limited; for a limited entry whose
    ``reset_at`` has passed, resets its record (when ``reset_expired``) and returns
    it. When every entry is still within its window, returns the one whose
    ``reset_at`` is soonest (least-bad) so the call still targets a configured
    provider rather than silently dropping to the env default; an entry marked
    without a ``reset_at`` sorts last among limited entries.

    Preconditions: ``entries`` is ordered by preference. Postconditions: returns an
        entry from ``entries`` or ``None`` (only when ``entries`` is empty);
        ``reset_entry`` may be called as a side effect for an expired entry. Never
        raises for well-formed entries.
    """
    if not entries:
        return None
    current = now or _utcnow()
    limited: list[ProviderEntry] = []
    for entry in entries:
        if not entry.limit_exceeded:
            return entry
        if entry.reset_at is not None and current >= entry.reset_at:
            if reset_expired:
                reset_entry(entry.id)
            return entry
        limited.append(entry)
    # All limited and none expired: pick the soonest reset (None resets sort last).
    far_future = datetime.max.replace(tzinfo=timezone.utc)
    return min(limited, key=lambda e: e.reset_at or far_future)


def list_fingerprint() -> str:
    """Return a stable fingerprint of the ordered list's STRUCTURE.

    Captures the identity that determines client construction and failover behavior
    — each entry's ``sort_order``, ``provider``, ``model``, ``base_url``, and whether
    it has a key — but deliberately EXCLUDES volatile limit-state (``limit_exceeded``
    / ``reset_at`` / ``limit_type``), which the :class:`FailoverLLMClient` handles
    dynamically per call. Consumers (e.g. the Strands model cache key) fold this in
    so adding/removing/reordering/editing a provider — including the empty→non-empty
    transition that enables failover — invalidates warm caches in every worker within
    the read TTL, while a 429 marking never churns them.

    Postconditions: ``"none"`` when the list is empty or Postgres is disabled; else a
        short hex digest. Reads the TTL-cached list (no extra DB round-trip on the hot
        path). Never raises (a read failure yields ``"none"``).
    """
    import hashlib

    entries = load_ordered_entries()
    if not entries:
        return "none"
    payload = "|".join(
        f"{e.sort_order}:{e.provider}:{e.model}:{e.base_url}:{1 if e.api_key else 0}"
        for e in entries
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:16]
