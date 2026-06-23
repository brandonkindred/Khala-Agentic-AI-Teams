"""Factory for the standard team FastAPI app wiring.

Almost every team's ``api/main.py`` opens with the same four moves: call
:func:`shared_observability.init_otel`, build a ``FastAPI`` with a lifespan that
registers the team's Postgres schema on startup and closes the pool on shutdown,
then :func:`shared_observability.instrument_fastapi_app`. :func:`create_team_app`
collapses that boilerplate into one call while leaving room for team-specific
startup/shutdown work via optional hooks.

Degrades cleanly: ``postgres_schema=None`` skips all Postgres wiring, and the
``shared_postgres`` import is lazy so a team without it is never forced to depend
on it. Schema registration and pool teardown are defensive (logged, never raised
into app startup), matching the per-team lifespans this replaces.
"""

from __future__ import annotations

import inspect
import logging
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Optional, Union

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
    on_startup: Optional[LifecycleHook] = None,
    on_shutdown: Optional[LifecycleHook] = None,
    excluded_urls: Optional[str] = None,
    **fastapi_kwargs: Any,
) -> FastAPI:
    """Build a fully-wired team :class:`FastAPI` app.

    Preconditions:
        - ``service_name``/``team_key``/``title``/``version`` are non-empty strings.
        - ``postgres_schema`` (when given) is a ``shared_postgres.TeamSchema``.
        - ``fastapi_kwargs`` must not contain ``title``/``version``/``lifespan``
          (set explicitly here); duplicating them raises ``TypeError``.
        - ``on_startup`` runs after schema registration; ``on_shutdown`` runs
          before the pool is closed. Teardown (``on_shutdown`` + pool close) runs
          even if ``on_startup`` raises, so a startup failure never leaks the pool.
    Postconditions:
        - :func:`init_otel` has been called and the returned app is OTel-
          instrumented; ``excluded_urls`` (when given) is forwarded to the
          instrumentor to override its default span-exclusion list — e.g. so a
          business route whose path contains ``metrics`` stays traced. Its
          lifespan registers ``postgres_schema`` on startup and
          closes the Postgres pool on shutdown (both no-ops/guarded when Postgres
          is unconfigured), wrapping the optional hooks. ``fastapi_kwargs`` pass
          through to the ``FastAPI`` constructor.
    Invariants:
        - Postgres wiring fires iff ``postgres_schema is not None``.
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

    init_otel(service_name=service_name, team_key=team_key)

    @asynccontextmanager
    async def _lifespan(application: FastAPI):
        if postgres_schema is not None:
            try:
                from shared_postgres import register_team_schemas

                register_team_schemas(postgres_schema)
            except Exception:
                logger.exception("%s postgres schema registration failed", team_key)
        # on_startup runs inside the try so that a raising hook still triggers
        # teardown — register_team_schemas may have opened the process-wide pool
        # above, and a startup failure must not leak it.
        try:
            await _maybe_call(on_startup)
            yield
        finally:
            await _maybe_call(on_shutdown)
            if postgres_schema is not None:
                try:
                    from shared_postgres import close_pool

                    close_pool()
                except Exception:
                    logger.warning("%s shared_postgres close_pool failed", team_key, exc_info=True)

    app = FastAPI(title=title, version=version, lifespan=_lifespan, **fastapi_kwargs)
    instrument_fastapi_app(app, team_key=team_key, excluded_urls=excluded_urls)
    return app
