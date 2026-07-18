"""Factory for the standard team FastAPI app wiring.

Almost every team's ``api/main.py`` opens with the same four moves: call
:func:`shared_observability.init_otel`, build a ``FastAPI`` with a lifespan that
registers the team's Postgres schema(s) on startup and closes the pool on
shutdown, then :func:`shared_observability.instrument_fastapi_app`.
:func:`create_team_app` collapses that boilerplate into one call while leaving
room for team-specific startup/shutdown work via optional hooks. A team can
also depend on another team's schema (e.g. shared user-profile tables) via
``extra_postgres_schemas`` — every schema given, primary and extra alike, is
exposed together on ``app.state.postgres_schemas`` for early bootstrap paths
(e.g. the team-service wrapper) to register before starting background workers.

Degrades cleanly: omitting both ``postgres_schema`` and
``extra_postgres_schemas`` skips all Postgres wiring, and the
``shared_postgres`` import is lazy so a team without it is never forced to
depend on it. Schema registration and pool teardown are defensive (logged,
never raised into app startup), matching the per-team lifespans this replaces.
"""

from __future__ import annotations

import inspect
import logging
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Optional, Sequence, Union

from fastapi import FastAPI

from shared_observability import init_otel, instrument_fastapi_app

if TYPE_CHECKING:
    from shared_postgres import TeamSchema

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


def create_team_app(
    *,
    service_name: str,
    team_key: str,
    title: str,
    version: str = "1.0.0",
    postgres_schema: "Optional[TeamSchema]" = None,
    extra_postgres_schemas: "Sequence[TeamSchema]" = (),
    on_startup: Optional[LifecycleHook] = None,
    on_shutdown: Optional[LifecycleHook] = None,
    excluded_urls: Optional[str] = None,
    **fastapi_kwargs: Any,
) -> FastAPI:
    """Build a fully-wired team :class:`FastAPI` app.

    Preconditions:
        - ``service_name``/``team_key``/``title``/``version`` are non-empty strings.
        - ``postgres_schema`` (when given) is a ``shared_postgres.TeamSchema``.
        - ``extra_postgres_schemas`` (when given) is a sequence of additional
          ``shared_postgres.TeamSchema`` — e.g. a schema owned by another team
          that this one also depends on. Registered after ``postgres_schema``,
          in the given order; ``None`` is treated the same as omitting it (no
          extras). A schema appearing in both ``postgres_schema`` and
          ``extra_postgres_schemas`` is **not** deduplicated — it is registered
          once per occurrence (harmless, since DDL is idempotent, but a
          wasted round-trip callers should avoid by not repeating a schema
          between the two params).
        - ``fastapi_kwargs`` must not contain ``lifespan`` (set explicitly here);
          passing it raises ``ValueError``. ``title``/``version`` are named
          parameters, so duplicating them raises ``TypeError`` from Python itself.
        - ``on_startup`` runs after schema registration; ``on_shutdown`` runs
          before the pool is closed. Teardown (``on_shutdown`` + pool close) runs
          even if ``on_startup`` **or** ``on_shutdown`` raises, so neither a
          startup nor a shutdown failure ever leaks the pool.
    Postconditions:
        - :func:`init_otel` has been called and the returned app is OTel-
          instrumented; ``excluded_urls`` (when given) is forwarded to the
          instrumentor to override its default span-exclusion list — e.g. so a
          business route whose path contains ``metrics`` stays traced. Its
          lifespan registers every schema in ``postgres_schema`` and
          ``extra_postgres_schemas`` on startup via
          :func:`shared_postgres.register_team_schemas_many` — each
          independently best-effort, so one schema's registration failure
          (including one whose ``.team`` attribute is itself unreadable) never
          blocks another's, nor ``on_startup`` — and closes the Postgres pool
          on shutdown (both no-ops/guarded when Postgres is unconfigured),
          wrapping the optional hooks. ``fastapi_kwargs`` pass through to the
          ``FastAPI`` constructor.
        - The returned app exposes its ``postgres_schema`` (the given
          ``TeamSchema`` or ``None``, unchanged) via ``app.state.postgres_schema``,
          and the full ordered set — ``postgres_schema`` first, then
          ``extra_postgres_schemas``, with any ``None`` filtered — via
          ``app.state.postgres_schemas`` (always a tuple, empty when neither is
          given), so early bootstrap paths (e.g. the team-service wrapper) can
          register every schema's DDL before starting background workers that
          write to any of them.
    Invariants:
        - Postgres wiring fires iff ``app.state.postgres_schemas`` is non-empty.
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
        raise ValueError(
            "lifespan must not be passed in fastapi_kwargs; it is set by create_team_app"
        )

    init_otel(service_name=service_name, team_key=team_key)

    # Primary schema first, then extras, in the given order; None filtered so
    # callers never need to check "was a primary schema even given" separately.
    # `extra_postgres_schemas or ()` normalizes an explicit None the same as
    # omitting it, matching postgres_schema's own None-means-none convention.
    # Computed once here (not inside _lifespan) since app.state needs it too.
    all_postgres_schemas: tuple = tuple(
        s for s in (postgres_schema, *(extra_postgres_schemas or ())) if s is not None
    )

    @asynccontextmanager
    async def _lifespan(application: FastAPI):
        if all_postgres_schemas:
            try:
                from shared_postgres import register_team_schemas_many

                # Best-effort per schema (one failure never blocks another's,
                # nor on_startup below) is register_team_schemas_many's own
                # contract; this try only guards against something registering
                # itself can't recover from, e.g. shared_postgres failing to
                # import.
                register_team_schemas_many(all_postgres_schemas)
            except Exception:
                logger.exception("%s postgres schema registration failed", team_key)
        # on_startup runs inside the try so that a raising hook still triggers
        # teardown — register_team_schemas_many may have opened the process-wide
        # pool above, and a startup failure must not leak it.
        try:
            await _maybe_call(on_startup)
            yield
        finally:
            # Guard on_shutdown so a raising hook cannot skip close_pool below —
            # otherwise a shutdown-hook failure would leak the process-wide pool,
            # breaking the "pool is always closed on shutdown" invariant.
            try:
                await _maybe_call(on_shutdown)
            except Exception:
                logger.exception("%s on_shutdown hook failed", team_key)
            if all_postgres_schemas:
                try:
                    from shared_postgres import close_pool

                    close_pool()
                except Exception:
                    logger.warning("%s shared_postgres close_pool failed", team_key, exc_info=True)

    app = FastAPI(title=title, version=version, lifespan=_lifespan, **fastapi_kwargs)
    # Expose the team's schema (or None) so early bootstrap paths that run before
    # the lifespan fires — e.g. the team-service wrapper starting a Temporal worker
    # at import time — can register the DDL first and avoid racing a best-effort
    # write against schema creation on a fresh database. postgres_schemas is the
    # full ordered set (primary + extras) those bootstrap paths should use;
    # postgres_schema stays the primary alone for backward compatibility.
    app.state.postgres_schema = postgres_schema
    app.state.postgres_schemas = all_postgres_schemas
    instrument_fastapi_app(app, team_key=team_key, excluded_urls=excluded_urls)
    return app
