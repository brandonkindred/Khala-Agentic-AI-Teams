"""Shared Postgres client.

Env-var helpers plus a process-wide ``psycopg_pool.ConnectionPool`` per
database. ``get_conn()`` acquires a pooled connection; ``close_pool()``
tears the pools down at shutdown. Used by ``ensure_team_schema`` at
startup and by team stores on hot paths.

Env vars (identical to ``job_service/db.py`` and
``backend/unified_api/postgres_encrypted_credentials.py``):

    POSTGRES_HOST, POSTGRES_PORT, POSTGRES_USER, POSTGRES_PASSWORD,
    POSTGRES_DB

Pool sizing:

    POSTGRES_POOL_MIN_SIZE  (default 2)
    POSTGRES_POOL_MAX_SIZE  (default 10)

``is_postgres_enabled()`` returns ``True`` only when ``POSTGRES_HOST`` is
set.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from collections.abc import Callable, Generator
from contextlib import contextmanager
from typing import Literal, Optional, TypeVar

from shared_env import parse_int

logger = logging.getLogger(__name__)

# Storage-reachability classification shared by the settings surfaces (LLM Provider
# page, GitHub integration panel) so the three-state contract is single-sourced.
StorageStatus = Literal["available", "unconfigured", "unreachable"]

# Per-database connection pools. Created lazily on first ``get_conn`` call
# for that database name.
_pools_lock = threading.Lock()
_pools: dict[str, object] = {}  # database name -> ConnectionPool


def is_postgres_enabled() -> bool:
    """True when ``POSTGRES_HOST`` is set (e.g. in the Docker stack)."""
    return bool(os.getenv("POSTGRES_HOST", "").strip())


def _default_database() -> str:
    return os.environ.get("POSTGRES_DB", "postgres")


def _connect_timeout() -> int:
    """Return the libpq ``connect_timeout`` (seconds) for new connections.

    Preconditions: none.
    Postconditions: returns a positive int from ``POSTGRES_CONNECT_TIMEOUT_S``
        (default 3, floored to 1). Bounds how long a TCP connect to a down or
        unreachable host can hang before psycopg gives up — without it, opening a
        pool (``open=True``) against an unreachable host can block far longer than
        any caller's own timeout, which is exactly the failure ``check_connection``
        must surface quickly.
    """
    return parse_int("POSTGRES_CONNECT_TIMEOUT_S", 3, minimum=1)


def connect_timeout() -> int:
    """Public accessor for the libpq ``connect_timeout`` (``POSTGRES_CONNECT_TIMEOUT_S``).

    Preconditions: none.
    Postconditions: returns the shared connect-timeout seconds so other modules that
        build their own DSN (e.g. the unified-API credential store) bound the connect
        with the *same* value as the pool, instead of re-deriving the default and
        drifting. Never raises.
    """
    return _connect_timeout()


def statement_timeout_ms() -> int:
    """Public accessor for the credential-store ``statement_timeout`` in milliseconds.

    Preconditions: none.
    Postconditions: returns ``POSTGRES_STATEMENT_TIMEOUT_MS`` (default 5000, floor 0;
        ``0`` disables it). Centralized so a caller sizing a request-level ``wait_for``
        budget uses the SAME number that bounds the query — instead of re-deriving it and
        drifting, which would let the outer guard fire before the bounded query finishes
        (leaving an abandoned worker holding a lock). Never raises.
    """
    return parse_int("POSTGRES_STATEMENT_TIMEOUT_MS", 5000, minimum=0)


def dsn(database: Optional[str] = None) -> str:
    """Build the one shared libpq keyword DSN for ``database`` (defaults to ``POSTGRES_DB``).

    Preconditions: none.
    Postconditions: returns a correctly-escaped libpq conninfo string (built by
        ``psycopg.conninfo.make_conninfo``, which owns libpq's quoting rules — empty,
        whitespace-bearing, quote, and backslash values are all escaped) carrying
        ``connect_timeout``. This is the SINGLE DSN builder for the platform: the pool,
        ``_connect``, and any module that opens its own connection (e.g. the unified-API
        credential store) all call it, so escaping can never drift between the
        reachability probe and the live read. Never raises here — ``psycopg`` is imported
        lazily and is present wherever a connection is actually opened.
    """
    from psycopg.conninfo import make_conninfo

    return make_conninfo(
        host=os.environ.get("POSTGRES_HOST", "localhost"),
        port=os.environ.get("POSTGRES_PORT", "5432"),
        dbname=database or _default_database(),
        user=os.environ.get("POSTGRES_USER", "postgres"),
        password=os.environ.get("POSTGRES_PASSWORD", ""),
        connect_timeout=_connect_timeout(),
    )


def _pool_sizes() -> tuple[int, int]:
    """Return ``(min_size, max_size)`` for the pool, from env vars.

    Postconditions: both sizes are at least 1 (a zero/negative override is
    clamped up, so the pool can never be configured empty), and
    ``max_size >= min_size``.
    """
    min_size = parse_int("POSTGRES_POOL_MIN_SIZE", 2, minimum=1)
    max_size = parse_int("POSTGRES_POOL_MAX_SIZE", 10, minimum=1)
    if max_size < min_size:
        max_size = min_size
    return min_size, max_size


def _connect(database: Optional[str] = None):
    """Open a fresh (unpooled) ``psycopg`` connection.

    Used for the initial DDL ``ensure_team_schema`` path and as a test
    seam. Raises ``RuntimeError`` when Postgres is disabled or psycopg
    is not installed, so callers fail loudly instead of silently
    skipping writes.
    """
    if not is_postgres_enabled():
        raise RuntimeError("POSTGRES_HOST is not set; cannot open a Postgres connection.")
    try:
        import psycopg
    except ImportError as e:
        raise RuntimeError(
            "psycopg is not installed; install psycopg[binary] to use shared_postgres."
        ) from e
    return psycopg.connect(dsn(database))


def _get_or_create_pool(database: Optional[str] = None):
    """Return (creating if needed) the ``ConnectionPool`` for ``database``.

    Raises ``RuntimeError`` when Postgres is disabled or ``psycopg_pool``
    is not installed.
    """
    if not is_postgres_enabled():
        raise RuntimeError("POSTGRES_HOST is not set; cannot open a Postgres connection.")

    db = database or _default_database()
    with _pools_lock:
        pool = _pools.get(db)
        if pool is not None:
            return pool
        try:
            from psycopg_pool import ConnectionPool
        except ImportError as e:
            raise RuntimeError(
                "psycopg_pool is not installed; install psycopg_pool to use shared_postgres."
            ) from e
        min_size, max_size = _pool_sizes()
        pool = ConnectionPool(
            conninfo=dsn(database),
            min_size=min_size,
            max_size=max_size,
            open=True,
            name=f"shared_postgres[{db}]",
        )
        _pools[db] = pool
        logger.info(
            "shared_postgres pool opened: database=%s min_size=%d max_size=%d",
            db,
            min_size,
            max_size,
        )
        return pool


@contextmanager
def get_conn(database: Optional[str] = None) -> Generator:
    """Yield a pooled ``psycopg`` connection for ``database``.

    Commits on clean exit, rolls back on exception, always returns the
    connection to the pool. The first call for a given ``database``
    lazily creates the pool.
    """
    pool = _get_or_create_pool(database)
    # ``ConnectionPool.connection()`` is itself a context manager that
    # commits on clean exit, rolls back on exception, and returns the
    # connection to the pool.
    with pool.connection() as conn:
        yield conn


@contextmanager
def probe_cursor(conn, *, timeout_s: float) -> Generator:
    """Yield a cursor on ``conn`` whose every statement is bounded by ``timeout_s``.

    The single home for the "bound a probe query so a post-connect mid-query stall can't
    pin the pooled connection" pattern. The shared pool deliberately sets no
    ``statement_timeout`` (it would cap legitimate long-running team queries), so each
    *probe* must scope its own bound — every probe path (``check_connection`` and the
    unified-API ``/health`` table-presence check) routes through here so the bound can't
    drift or be forgotten.

    Preconditions: ``conn`` is a live psycopg connection; ``timeout_s`` is a positive,
        real budget (a sub-millisecond value is clamped up to a 1ms ``statement_timeout``).
    Postconditions: opens a transaction on ``conn``, issues ``SET LOCAL statement_timeout``
        (transaction-scoped, so it is rolled back with the probe transaction and never
        leaks onto the next user of a pooled connection), and yields a cursor. Every query
        run on that cursor is cancelled server-side after ``timeout_s``. Note this bounds
        the *query*, not socket teardown: if the TCP path itself wedges (no server response
        at all), ``statement_timeout`` cannot fire and the closing ``ROLLBACK`` can still
        block — that residual case is backstopped by :func:`bounded_probe`'s per-surface
        worker cap, not by this bound.
    """
    stmt_timeout_ms = max(1, int(timeout_s * 1000))
    # ``stmt_timeout_ms`` is a locally-derived int (never caller text), so inlining it is
    # injection-safe; ``SET LOCAL`` is the conventional, single-statement form (no result
    # set to drain, unlike ``SELECT set_config(...)``).
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(f"SET LOCAL statement_timeout = {stmt_timeout_ms}")
        yield cur


def check_connection(database: Optional[str] = None, *, timeout_s: float = 1.5) -> bool:
    """Return True iff Postgres is enabled and answers ``SELECT 1`` quickly.

    A real connectivity probe, distinct from :func:`is_postgres_enabled` (which only
    checks that ``POSTGRES_HOST`` is set). Callers use the pair to tell three states
    apart: not-configured (``is_postgres_enabled()`` false), configured-but-unreachable
    (``is_postgres_enabled()`` true, this false), and healthy (both true).

    Preconditions: ``timeout_s`` is a positive, real budget (see :func:`probe_cursor`).
    Postconditions: returns ``False`` immediately when Postgres is disabled; otherwise
        acquires a pooled connection (waiting at most ``timeout_s`` for a free slot,
        bounded on the TCP connect by ``POSTGRES_CONNECT_TIMEOUT_S``), runs ``SELECT 1``
        via :func:`probe_cursor` (transaction-local ``statement_timeout`` of ``timeout_s``)
        so a server that accepts the connection then stalls mid-query cannot pin the pooled
        connection past the budget, and returns whether the result is ``(1,)``. Swallows
        every error (disabled, psycopg missing, host down, pool exhausted, connect/query
        timeout) and returns ``False`` — never raises. A fully wedged TCP socket (no server
        response) is not covered by ``statement_timeout``; that residual is bounded by the
        caller's :func:`bounded_probe` worker cap. Read-only with no side effects beyond
        opening/reusing the shared pool.
    """
    if not is_postgres_enabled():
        return False
    try:
        # Reach through ``_get_or_create_pool`` (rather than ``get_conn``) so the
        # acquisition can be bounded by ``timeout_s``; the public context manager
        # exposes no timeout knob. Mirrors the bounded probe in the unified API
        # health loop, which now delegates here.
        pool = _get_or_create_pool(database)
        with (
            pool.connection(timeout=timeout_s) as conn,
            probe_cursor(conn, timeout_s=timeout_s) as cur,
        ):
            cur.execute("SELECT 1")
            row = cur.fetchone()
            return row is not None and row[0] == 1
    except Exception:  # noqa: BLE001 - a probe must never raise; any failure is "unreachable"
        return False


def resolve_storage_status(*, timeout_s: float = 1.5) -> StorageStatus:
    """Classify the runtime store into one of three states for operator surfaces.

    The single source of truth for the LLM Provider page and the GitHub integration
    panel, so their three-state contract cannot drift.

    Preconditions: none.
    Postconditions: returns ``"unconfigured"`` when ``POSTGRES_HOST`` is unset,
        ``"unreachable"`` when it is set but :func:`check_connection` (a bounded
        ``SELECT 1``) fails, and ``"available"`` when the store answers. Never raises.
        Runs blocking I/O (the probe), so async callers must offload it (and bound the
        offload, since a connection that stalls *after* connect is not covered by
        ``connect_timeout``).
    """
    if not is_postgres_enabled():
        return "unconfigured"
    return "available" if check_connection(timeout_s=timeout_s) else "unreachable"


def default_probe_budget() -> float:
    """Seconds an offloaded probe/credential read may run before the caller abandons it.

    Preconditions: none.
    Postconditions: returns ``connect_timeout + statement_timeout + 1.0`` seconds — large
        enough that a worker bounded by the TCP connect (``connect_timeout``) and the query
        (``statement_timeout``: the credential store applies ``POSTGRES_STATEMENT_TIMEOUT_MS``,
        the shared-pool ``SELECT 1`` probe applies its own per-call bound — see
        :func:`check_connection`) finishes, releasing any lock/connection it holds, before
        the caller's :func:`bounded_probe` abandons it, and that a within-bounds slow read is
        not falsely reported unreachable. When ``statement_timeout`` is disabled (0) the
        credential-store query is unbounded, so this is then a best-effort cap, not a
        guarantee — :func:`bounded_probe`'s per-surface worker cap is the backstop. Never
        raises.
    """
    return float(_connect_timeout()) + statement_timeout_ms() / 1000.0 + 1.0


_T = TypeVar("_T")

# Concurrency cap on in-flight bounded_probe worker threads. A probe normally completes
# in milliseconds and releases its slot immediately; the cap only bites when that many
# workers are already STUCK (a Postgres that accepts the connection then stalls). Capping
# bounds the abandoned-thread / held-connection count under a sustained stall instead of
# spawning one unbounded daemon thread per request. Keyed PER CALLER (by ``label``) so a
# stall on one surface (e.g. the GitHub credential read) can't starve the cap an unrelated,
# healthy surface (e.g. the LLM Provider page SELECT 1) needs.
_PROBE_SEMS: dict[str, threading.Semaphore] = {}
_PROBE_SEM_LOCK = threading.Lock()
# The platform's real probe surfaces are a small fixed set of static labels. Bound the
# registry so a (mis)caller passing a DYNAMIC label can't grow it without limit (a slow
# memory leak) or defeat the cap (a fresh full budget per unique label → unbounded threads).
# Past ``POSTGRES_PROBE_MAX_KEYS`` distinct labels, further labels share one DEDICATED
# overflow semaphore (a module-level object, NOT an entry in ``_PROBE_SEMS`` — so no caller
# label can collide with it): memory stays bounded and the cap still bites.
_PROBE_OVERFLOW_SEM: Optional[threading.Semaphore] = None


def _make_probe_semaphore() -> threading.Semaphore:
    """Create a fresh probe-concurrency semaphore sized by ``POSTGRES_PROBE_MAX_WORKERS``."""
    return threading.Semaphore(parse_int("POSTGRES_PROBE_MAX_WORKERS", 4, minimum=1))


def _probe_max_keys() -> int:
    """Max distinct per-label probe semaphores before labels collapse onto overflow.

    Preconditions: none.
    Postconditions: returns ``POSTGRES_PROBE_MAX_KEYS`` (default 32, floor 1). Never raises.
    """
    return parse_int("POSTGRES_PROBE_MAX_KEYS", 32, minimum=1)


class _ProbeWorkerError(Exception):
    """Wrapper relayed to the awaiter when a probe worker hits a non-``Exception``.

    Lets :func:`bounded_probe`'s ``except Exception`` degrade-and-log path fire for a
    ``BaseException`` (e.g. ``SystemExit``) without that ``BaseException`` escaping the
    awaiting coroutine — the original is re-raised inside the worker thread, not relayed.
    """


def _probe_semaphore(key: str) -> threading.Semaphore:
    """Return the lazily-created probe-concurrency semaphore for ``key``.

    Preconditions: ``key`` is a stable per-caller string (the ``bounded_probe`` ``label``).
    Postconditions: returns a process-wide ``Semaphore`` for ``key`` whose count is
        ``POSTGRES_PROBE_MAX_WORKERS`` (default 4, floor 1), created once per key. The count
        is read ONCE when the key's semaphore is first created and fixed thereafter — a
        ``Semaphore`` cannot be resized, so unlike the per-call env knobs (connect/statement
        timeout) a runtime change to ``POSTGRES_PROBE_MAX_WORKERS`` takes effect only in a
        fresh process. Distinct keys are bounded by ``POSTGRES_PROBE_MAX_KEYS``; beyond it,
        further labels share one dedicated overflow semaphore so a dynamic-label caller can
        neither leak memory nor escape the cap. Two trade-offs of that backstop, both
        confined to the misuse regime (the platform's real surfaces are a small fixed set
        well under the ceiling): (1) overflow callers share one budget, so they lose the
        per-surface isolation static callers keep; (2) an uncached (e.g. per-request
        dynamic) label can't hit the lock-free fast path, so each such call takes
        ``_PROBE_SEM_LOCK``. Never raises.
    """
    sem = _PROBE_SEMS.get(key)
    if sem is not None:
        return sem
    global _PROBE_OVERFLOW_SEM
    with _PROBE_SEM_LOCK:
        sem = _PROBE_SEMS.get(key)
        if sem is not None:
            return sem
        if len(_PROBE_SEMS) >= _probe_max_keys():
            # Registry full → don't mint a new per-key budget; share the dedicated overflow
            # semaphore. It lives outside _PROBE_SEMS, so no caller label can collide with it.
            if _PROBE_OVERFLOW_SEM is None:
                _PROBE_OVERFLOW_SEM = _make_probe_semaphore()
            return _PROBE_OVERFLOW_SEM
        sem = _make_probe_semaphore()
        _PROBE_SEMS[key] = sem
        return sem


def _drain_future(fut: asyncio.Future) -> None:
    """Consume a settled future's outcome so the loop doesn't log it as unhandled.

    Preconditions: none.
    Postconditions: retrieves ``fut``'s exception (marking it handled) when it carries one;
        a normal result or a cancellation is left as-is. Never raises.
    """
    if not fut.cancelled():
        fut.exception()


async def bounded_probe(
    fn: Callable[[], _T],
    *,
    on_failure: Callable[[], _T],
    budget: Optional[float] = None,
    label: str = "storage probe",
) -> _T:
    """Run blocking ``fn`` off the event loop, bounded so a stalled DB can't hang the request.

    The single home for the "offload a blocking probe, time it out, log + degrade on
    failure" pattern shared by the LLM Provider page and the GitHub config panel.

    Preconditions: ``fn`` is a no-arg blocking callable; ``on_failure`` is a no-arg
        callable returning the degraded result (same type as ``fn``); ``label`` is a stable
        per-caller string (it keys the concurrency cap, so distinct surfaces must pass
        distinct labels).
    Postconditions: returns ``fn()``'s result; on timeout or any exception from ``fn``, logs
        the cause and returns ``on_failure()`` (a non-``Exception`` ``BaseException`` is
        relayed WRAPPED so it degrades like any failure yet does not escape this coroutine,
        and is re-raised inside the worker thread). ``budget`` defaults to
        :func:`default_probe_budget`. The call ALWAYS returns within ``budget`` seconds of
        wall-clock even if ``fn`` keeps blocking: ``fn`` runs in a detached daemon thread
        (NOT the loop executor — ``asyncio.wait_for(asyncio.to_thread(...))`` does not bound
        wall-clock, since an executor future can't be cancelled) and resolves an
        ``asyncio.shield``-ed future via a loop-closed-safe callback. Concurrent workers are
        capped PER ``label`` at ``POSTGRES_PROBE_MAX_WORKERS``; once that many are stuck for a
        surface, its further calls degrade immediately rather than spawn more threads, so a
        sustained stall can't grow threads / held connections without bound and one surface's
        stall can't starve another's cap. A timed-out worker is abandoned: it releases its DB
        connection / lock only when ``fn`` finally returns — bounded by ``statement_timeout``
        on both the credential-store path and the shared-pool ``SELECT 1`` probe (see
        :func:`check_connection`); the per-surface cap is the backstop for any path that
        leaves the query unbounded (e.g. ``POSTGRES_STATEMENT_TIMEOUT_MS=0``). Never raises.
    """
    if budget is None:
        budget = default_probe_budget()
    sem = _probe_semaphore(label)
    if not sem.acquire(blocking=False):
        # That many probes for this surface are already stuck → the store is evidently not
        # answering. Don't pile on another thread; degrade now.
        logger.warning(
            "%s skipped: probe concurrency cap reached; returning degraded result", label
        )
        return on_failure()

    loop = asyncio.get_running_loop()
    result: asyncio.Future = loop.create_future()

    def _settle(value: Optional[_T], error: Optional[BaseException]) -> None:
        if result.done():
            return
        if error is not None:
            result.set_exception(error)
        else:
            result.set_result(value)

    def _safe_settle(value: Optional[_T], error: Optional[BaseException]) -> None:
        # The loop may already be closed (timed-out request torn down, or process shutdown)
        # by the time a stuck worker finishes; call_soon_threadsafe raises RuntimeError then.
        try:
            loop.call_soon_threadsafe(_settle, value, error)
        except RuntimeError:
            pass

    def _runner() -> None:
        try:
            try:
                value = fn()
            except BaseException as e:  # noqa: BLE001 - relayed to the awaiter; degrades the request
                # Relay Exceptions as-is; relay a non-Exception BaseException (SystemExit,
                # etc.) WRAPPED so the awaiter degrades immediately (instead of waiting the
                # full budget) yet the BaseException doesn't escape the awaiting coroutine,
                # then re-raise the original so the worker thread still terminates on it.
                _safe_settle(None, e if isinstance(e, Exception) else _ProbeWorkerError(repr(e)))
                if not isinstance(e, Exception):
                    raise
            else:
                _safe_settle(value, None)
        finally:
            sem.release()

    try:
        threading.Thread(target=_runner, name="bounded_probe", daemon=True).start()
    except BaseException:  # noqa: BLE001 - the worker never ran, so release the slot here
        # _runner (which owns sem.release()) never started, so the slot would leak and the
        # exception would escape, breaking "Never raises". Release and degrade instead.
        sem.release()
        logger.exception("%s could not start probe worker; returning degraded result", label)
        return on_failure()
    try:
        # shield so a timeout cancels only our wait, never the future the detached thread
        # will later resolve (which would raise InvalidStateError in that thread).
        return await asyncio.wait_for(asyncio.shield(result), timeout=budget)
    except asyncio.TimeoutError:
        # Drain the eventual result/exception so the loop doesn't log it as unhandled, but
        # do NOT await it — the worker is abandoned (capped, so this can't grow unbounded).
        result.add_done_callback(_drain_future)
        logger.warning("%s timed out after %.1fs; returning degraded result", label, budget)
        return on_failure()
    except Exception:  # noqa: BLE001 - any relayed failure from fn → logged, degraded result
        logger.exception("%s failed; returning degraded result", label)
        return on_failure()


def close_pool(database: Optional[str] = None) -> None:
    """Close and drop the connection pool(s) opened by ``get_conn``.

    Called from FastAPI lifespan shutdown. Safe to call multiple times;
    safe to call when no pool was ever opened.
    """
    with _pools_lock:
        if database is not None:
            dbs = [database]
        else:
            dbs = list(_pools.keys())
        for db in dbs:
            pool = _pools.pop(db, None)
            if pool is None:
                continue
            try:
                pool.close()
                logger.info("shared_postgres pool closed: database=%s", db)
            except Exception as e:
                logger.warning("shared_postgres pool close failed: database=%s error=%s", db, e)
