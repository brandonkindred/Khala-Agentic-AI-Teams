"""Factory for the standard team FastAPI app wiring.

Almost every team's ``api/main.py`` opens with the same four moves: call
:func:`shared.observability.init_otel`, build a ``FastAPI`` with a lifespan that
registers the team's Postgres schema on startup and closes the pool on shutdown,
then :func:`shared.observability.instrument_fastapi_app`. :func:`create_team_app`
collapses that boilerplate into one call while leaving room for team-specific
startup/shutdown work via optional hooks.

Degrades cleanly: ``postgres_schema=None`` skips all Postgres wiring, and the
``shared.postgres`` import is lazy so a team without it is never forced to depend
on it. Schema registration and pool teardown are defensive (logged, never raised
into app startup), matching the per-team lifespans this replaces.
"""

from __future__ import annotations

import inspect
import logging
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Optional, Sequence, Union

from fastapi import FastAPI

from shared.observability import init_otel, instrument_fastapi_app

if TYPE_CHECKING:
    from shared.postgres import TeamSchema

logger = logging.getLogger(__name__)

# A startup/shutdown hook may be a plain callable or a coroutine function.
LifecycleHook = Callable[[], Union[None, Awaitable[None]]]


async def _maybe_call(hook: Optional[LifecycleHook]) -> None:
    """Invoke ``hook`` if given, awaiting it when it returns a coroutine.

    Postconditions:
        - ``None`` hook is a no-op; a sync hook runs to completion; an async hook
          is awaited. The hook's own exceptions propagate (the caller decides).
    """
    if hook is None:
        return
    result = hook()
    if inspect.isawaitable(result):
        await result


def _register_usage_flusher(team_key: str) -> None:
    """Register the process-local LLM usage observer + drain heartbeat.

    The observer registry lives in this process. Unified API registration cannot
    see LLM calls made by a team-service worker, so every team app must register
    here. Idempotent; no-op when Postgres is unset. Failures are logged and never
    raised into app startup.

    Preconditions:
        - ``team_key`` is a non-empty string used only in log messages.
    Postconditions:
        - ``register_usage_flusher`` has been invoked, or a failure was logged
          and startup continues.
    """
    try:
        from llm_service.usage_flusher import register_usage_flusher

        register_usage_flusher()
    except Exception:
        logger.warning("%s llm usage flusher registration failed", team_key, exc_info=True)


def _shutdown_usage_flusher(team_key: str) -> None:
    """Drain buffered LLM usage rows before the process-wide Postgres pool closes.

    Preconditions:
        - ``team_key`` is a non-empty string used only in log messages.
    Postconditions:
        - ``usage_flusher.shutdown`` has been invoked (unregister then drain), or
          a failure was logged. Never raises.
    """
    try:
        from llm_service.usage_flusher import shutdown as usage_flush_shutdown

        usage_flush_shutdown()
    except Exception:
        logger.warning("%s llm usage flusher shutdown failed", team_key, exc_info=True)


def create_team_app(
    *,
    service_name: str,
    team_key: str,
    title: str,
    version: str = "1.0.0",
    postgres_schema: "Optional[TeamSchema]" = None,
    extra_postgres_schemas: "Optional[Sequence[TeamSchema]]" = None,
    on_startup: Optional[LifecycleHook] = None,
    on_shutdown: Optional[LifecycleHook] = None,
    excluded_urls: Optional[str] = None,
    **fastapi_kwargs: Any,
) -> FastAPI:
    """Build a fully-wired team :class:`FastAPI` app.

    Preconditions:
        - ``service_name``/``team_key``/``title``/``version`` are non-empty strings.
        - ``postgres_schema`` (when given) is a ``shared.postgres.TeamSchema``.
        - ``extra_postgres_schemas`` (when given) is a sequence of additional
          ``shared.postgres.TeamSchema`` values (no ``None`` elements) a team
          needs registered alongside its primary schema — e.g. a second schema
          it would otherwise register from an ``on_startup`` hook, which runs
          too late to close the Temporal cold-start race (see ``postgres_schemas``
          below).
        - ``fastapi_kwargs`` must not contain ``lifespan`` (set explicitly here);
          passing it raises ``ValueError``. ``title``/``version`` are named
          parameters, so duplicating them raises ``TypeError`` from Python itself.
        - ``on_startup`` runs after schema registration and LLM-usage-flusher
          registration; ``on_shutdown`` runs before the usage flusher drains and
          the pool is closed. Teardown (``on_shutdown`` + usage flush + pool
          close) runs even if ``on_startup`` **or** ``on_shutdown`` raises, so
          neither a startup nor a shutdown failure ever leaks the pool.
    Postconditions:
        - :func:`init_otel` has been called and the returned app is OTel-
          instrumented; ``excluded_urls`` (when given) is forwarded to the
          instrumentor to override its default span-exclusion list — e.g. so a
          business route whose path contains ``metrics`` stays traced. Its
          lifespan registers every schema in ``postgres_schema`` plus
          ``extra_postgres_schemas`` on startup and closes the Postgres pool on
          wrapping the optional hooks. The same lifespan registers the process-local
          LLM usage flusher after schema registration and shuts it down after
          ``on_shutdown`` and before ``close_pool``. A single schema's registration
          failure is logged and does not stop the remaining schemas from registering.
          ``fastapi_kwargs`` pass through to the ``FastAPI`` constructor.
        - The returned app exposes its ``postgres_schema`` (the given
          ``TeamSchema`` or ``None``, unchanged for backward compatibility) via
          ``app.state.postgres_schema``, and the full combined set (primary
          followed by ``extra_postgres_schemas``, ``[]`` when neither is given)
          via ``app.state.postgres_schemas``, so early bootstrap paths (e.g. the
          team-service wrapper) can register every schema's DDL before starting
          background workers that write to it.
    Invariants:
        - Postgres wiring fires iff the combined schema set (``postgres_schema``
          plus ``extra_postgres_schemas``) is non-empty.
    """
    # Validate the required identifiers explicitly (not via assert, so the check
    # holds under ``python -O``): empty values otherwise surface as obscure
    # failures deep inside init_otel/FastAPI.
    for _name, _value in (
        ("service_name", service_name),
        ("team_key", team_key),
        ("title", title),
        ("version", version),
    ):
        if not isinstance(_value, str) or not _value:
            raise ValueError(f"{_name} must be a non-empty string")

    # ``lifespan`` is set explicitly below and is NOT a named parameter of this
    # function, so a caller-supplied one would land in ``fastapi_kwargs`` and
    # collide with ours inside ``FastAPI(...)``. Reject it up front with a clear
    # message. (``title``/``version`` are named parameters, so Python already
    # raises ``TypeError`` on a duplicate — they can never reach ``fastapi_kwargs``.)
    if "lifespan" in fastapi_kwargs:
        raise ValueError("lifespan must not be passed in fastapi_kwargs; it is set by create_team_app")

    init_otel(service_name=service_name, team_key=team_key)

    # Primary schema first, then any extras, in declaration order.
    _all_schemas: "tuple[TeamSchema, ...]" = ((postgres_schema,) if postgres_schema is not None else ()) + (
        tuple(extra_postgres_schemas) if extra_postgres_schemas else ()
    )

    @asynccontextmanager
    async def _lifespan(application: FastAPI):
        if _all_schemas:
            try:
                from shared.postgres import register_team_schemas
            except Exception:
                logger.exception("%s postgres schema registration failed (import)", team_key)
            else:
                for schema in _all_schemas:
                    # Each schema registers independently so one failure doesn't
                    # stop the rest of the team's schemas from registering.
                    try:
                        register_team_schemas(schema)
                    except Exception:
                        logger.exception("%s postgres schema registration failed", team_key)
        # on_startup runs inside the try so that a raising hook still triggers
        # teardown — register_team_schemas may have opened the process-wide pool
        # above, and a startup failure must not leak it. Usage flusher registers
        # before on_startup so team hooks that start Temporal workers already
        # have the process-local observer.
        try:
            _register_usage_flusher(team_key)
            await _maybe_call(on_startup)
            yield
        finally:
            # Guard on_shutdown so a raising hook cannot skip usage-flush or
            # close_pool below — otherwise a shutdown-hook failure would leak
            # the process-wide pool, breaking the "pool is always closed on
            # shutdown" invariant.
            try:
                await _maybe_call(on_shutdown)
            except Exception:
                logger.exception("%s on_shutdown hook failed", team_key)
            # Drain before close_pool so the final INSERT can still use the pool.
            _shutdown_usage_flusher(team_key)
            if _all_schemas:
                try:
                    from shared.postgres import close_pool

                    close_pool()
                except Exception:
                    logger.warning("%s shared.postgres close_pool failed", team_key, exc_info=True)

    app = FastAPI(title=title, version=version, lifespan=_lifespan, **fastapi_kwargs)
    # Expose the team's primary schema (or None) — unchanged, for backward
    # compatibility — plus the full combined set, so early bootstrap paths that
    # run before the lifespan fires — e.g. the team-service wrapper starting a
    # Temporal worker at import time — can register every schema's DDL first and
    # avoid racing a best-effort write against schema creation on a fresh database.
    app.state.postgres_schema = postgres_schema
    app.state.postgres_schemas = list(_all_schemas)
    instrument_fastapi_app(app, team_key=team_key, excluded_urls=excluded_urls)
    return app
