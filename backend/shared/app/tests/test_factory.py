"""Unit tests for the shared team FastAPI app factory (DB-free)."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import shared.app.factory as factory
from shared.app import create_team_app


@pytest.fixture(autouse=True)
def _stub_otel(monkeypatch) -> None:
    """Neutralize OTel global side effects; record that they were invoked."""
    calls = {"init": [], "instrument": []}
    monkeypatch.setattr(factory, "init_otel", lambda **kw: calls["init"].append(kw))
    monkeypatch.setattr(factory, "instrument_fastapi_app", lambda app, **kw: calls["instrument"].append(kw))
    factory._test_calls = calls  # type: ignore[attr-defined]
    yield
    delattr(factory, "_test_calls")


_REAL_REGISTER_USAGE = factory._register_usage_flusher
_REAL_SHUTDOWN_USAGE = factory._shutdown_usage_flusher


@pytest.fixture(autouse=True)
def _stub_usage_flusher(monkeypatch) -> None:
    """Keep factory tests from starting the real usage-flusher heartbeat."""
    monkeypatch.setattr(factory, "_register_usage_flusher", lambda _t: None)
    monkeypatch.setattr(factory, "_shutdown_usage_flusher", lambda _t: None)


def test_create_team_app_wires_otel_and_returns_app() -> None:
    app = create_team_app(service_name="svc", team_key="tk", title="T", version="9.9")
    assert isinstance(app, FastAPI)
    assert app.title == "T" and app.version == "9.9"
    assert factory._test_calls["init"] == [{"service_name": "svc", "team_key": "tk"}]
    # excluded_urls is always forwarded to the instrumentor (None when unset).
    assert factory._test_calls["instrument"] == [{"team_key": "tk", "excluded_urls": None}]


def test_excluded_urls_forwarded_to_instrument() -> None:
    create_team_app(service_name="svc", team_key="tk", title="T", excluded_urls="metrics,healthz")
    assert factory._test_calls["instrument"] == [{"team_key": "tk", "excluded_urls": "metrics,healthz"}]


def test_fastapi_kwargs_passthrough() -> None:
    app = create_team_app(service_name="svc", team_key="tk", title="T", description="d", docs_url="/d")
    assert app.description == "d"
    assert app.docs_url == "/d"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"service_name": "", "team_key": "tk", "title": "T"},
        {"service_name": "svc", "team_key": "", "title": "T"},
        {"service_name": "svc", "team_key": "tk", "title": ""},
        {"service_name": "svc", "team_key": "tk", "title": "T", "version": ""},
    ],
)
def test_create_team_app_rejects_empty_required_strings(kwargs) -> None:
    with pytest.raises(ValueError):
        create_team_app(**kwargs)


def test_postgres_schema_exposed_on_app_state() -> None:
    """The app exposes its schema (or None) on app.state so early bootstrap paths
    (e.g. the team-service wrapper) can register DDL before starting workers."""
    schema = object()
    app = create_team_app(service_name="svc", team_key="tk", title="T", postgres_schema=schema)
    assert app.state.postgres_schema is schema
    # Omitted ⇒ None (no Postgres wiring), still explicitly present on state.
    app_none = create_team_app(service_name="svc", team_key="tk", title="T")
    assert app_none.state.postgres_schema is None


def test_extra_postgres_schemas_exposed_and_registered(monkeypatch) -> None:
    """extra_postgres_schemas combine with the primary schema on app.state and
    all of them are registered on startup, in primary-then-extras order."""
    import shared.postgres

    registered = []
    monkeypatch.setattr(shared.postgres, "register_team_schemas", lambda s: registered.append(s))
    monkeypatch.setattr(shared.postgres, "close_pool", lambda: None)

    schema1, schema2, schema3 = object(), object(), object()
    app = create_team_app(
        service_name="svc",
        team_key="tk",
        title="T",
        postgres_schema=schema1,
        extra_postgres_schemas=[schema2, schema3],
    )
    assert app.state.postgres_schema is schema1  # unchanged, backward compatible
    assert app.state.postgres_schemas == [schema1, schema2, schema3]
    with TestClient(app):
        assert registered == [schema1, schema2, schema3]


def test_extra_schemas_only_no_primary(monkeypatch) -> None:
    """The wiring fires from extra_postgres_schemas alone, with no primary schema."""
    import shared.postgres

    registered = []
    monkeypatch.setattr(shared.postgres, "register_team_schemas", lambda s: registered.append(s))
    monkeypatch.setattr(shared.postgres, "close_pool", lambda: None)

    schema2 = object()
    app = create_team_app(service_name="svc", team_key="tk", title="T", extra_postgres_schemas=[schema2])
    assert app.state.postgres_schema is None
    assert app.state.postgres_schemas == [schema2]
    with TestClient(app):
        assert registered == [schema2]


def test_no_schemas_at_all_exposes_empty_list() -> None:
    app = create_team_app(service_name="svc", team_key="tk", title="T")
    assert app.state.postgres_schemas == []


def test_one_schema_registration_failure_does_not_block_others(monkeypatch) -> None:
    """A single schema's registration failure is logged but does not prevent the
    remaining schemas in the set from registering."""
    import shared.postgres

    schema1, schema2 = object(), object()
    registered = []

    def _register(schema):
        if schema is schema1:
            raise RuntimeError("pg down for schema1")
        registered.append(schema)

    monkeypatch.setattr(shared.postgres, "register_team_schemas", _register)
    monkeypatch.setattr(shared.postgres, "close_pool", lambda: None)

    app = create_team_app(
        service_name="svc",
        team_key="tk",
        title="T",
        postgres_schema=schema1,
        extra_postgres_schemas=[schema2],
    )
    with TestClient(app):
        assert registered == [schema2]  # schema1 failed, schema2 still registered


def test_schema_import_failure_does_not_break_startup(monkeypatch) -> None:
    """If shared.postgres.register_team_schemas can't even be imported, the
    import failure is logged and startup still proceeds."""
    import shared.postgres

    monkeypatch.delattr(shared.postgres, "register_team_schemas", raising=False)
    monkeypatch.setattr(shared.postgres, "close_pool", lambda: None)

    app = create_team_app(service_name="svc", team_key="tk", title="T", postgres_schema=object())
    with TestClient(app):
        pass  # startup must not raise despite the import failure


def test_no_postgres_schema_skips_db_wiring(monkeypatch) -> None:
    import shared.postgres

    called = []
    monkeypatch.setattr(shared.postgres, "register_team_schemas", lambda s: called.append(s))
    monkeypatch.setattr(shared.postgres, "close_pool", lambda: called.append("close"))

    app = create_team_app(service_name="svc", team_key="tk", title="T")
    with TestClient(app):
        pass
    assert called == []  # postgres_schema=None ⇒ no register / no close


def test_postgres_schema_registers_on_startup_and_closes_on_shutdown(monkeypatch) -> None:
    import shared.postgres

    events = []
    monkeypatch.setattr(shared.postgres, "register_team_schemas", lambda s: events.append(("register", s)))
    monkeypatch.setattr(shared.postgres, "close_pool", lambda: events.append(("close", None)))

    schema = object()
    app = create_team_app(service_name="svc", team_key="tk", title="T", postgres_schema=schema)
    with TestClient(app):
        assert events == [("register", schema)]  # registered on startup, not yet closed
    assert events == [("register", schema), ("close", None)]  # closed on shutdown


def test_lifecycle_hooks_run_in_order(monkeypatch) -> None:
    order = []

    def on_startup() -> None:
        order.append("startup")

    async def on_shutdown() -> None:  # async hook is awaited
        order.append("shutdown")

    app = create_team_app(
        service_name="svc",
        team_key="tk",
        title="T",
        on_startup=on_startup,
        on_shutdown=on_shutdown,
    )
    with TestClient(app):
        assert order == ["startup"]
    assert order == ["startup", "shutdown"]


def test_schema_registration_failure_does_not_break_startup(monkeypatch) -> None:
    import shared.postgres

    def boom(_schema) -> None:
        raise RuntimeError("pg down")

    monkeypatch.setattr(shared.postgres, "register_team_schemas", boom)
    monkeypatch.setattr(shared.postgres, "close_pool", lambda: None)

    app = create_team_app(service_name="svc", team_key="tk", title="T", postgres_schema=object())
    # Startup must not raise despite the registration failure.
    with TestClient(app):
        pass


def test_close_pool_failure_is_swallowed(monkeypatch) -> None:
    import shared.postgres

    monkeypatch.setattr(shared.postgres, "register_team_schemas", lambda s: None)

    def boom() -> None:
        raise RuntimeError("close failed")

    monkeypatch.setattr(shared.postgres, "close_pool", boom)
    app = create_team_app(service_name="svc", team_key="tk", title="T", postgres_schema=object())
    with TestClient(app):
        pass  # shutdown must not raise


def test_on_startup_failure_still_closes_pool(monkeypatch) -> None:
    # A raising on_startup must NOT leak the pool register_team_schemas opened:
    # teardown (close_pool) runs regardless.
    import shared.postgres

    closed = []
    monkeypatch.setattr(shared.postgres, "register_team_schemas", lambda s: None)
    monkeypatch.setattr(shared.postgres, "close_pool", lambda: closed.append(True))

    def on_startup() -> None:
        raise RuntimeError("startup boom")

    app = create_team_app(
        service_name="svc",
        team_key="tk",
        title="T",
        postgres_schema=object(),
        on_startup=on_startup,
    )
    with pytest.raises(RuntimeError, match="startup boom"):
        with TestClient(app):
            pass
    assert closed == [True]  # pool closed despite the startup failure


def test_on_shutdown_failure_still_closes_pool(monkeypatch) -> None:
    # A raising on_shutdown must NOT skip close_pool: the pool is always closed
    # on shutdown, and the hook's failure is swallowed (logged), not propagated.
    import shared.postgres

    closed = []
    monkeypatch.setattr(shared.postgres, "register_team_schemas", lambda s: None)
    monkeypatch.setattr(shared.postgres, "close_pool", lambda: closed.append(True))

    def on_shutdown() -> None:
        raise RuntimeError("shutdown boom")

    app = create_team_app(
        service_name="svc",
        team_key="tk",
        title="T",
        postgres_schema=object(),
        on_shutdown=on_shutdown,
    )
    # The raising shutdown hook is swallowed, so exiting the context does not raise.
    with TestClient(app):
        pass
    assert closed == [True]  # pool closed despite the shutdown-hook failure


def test_usage_flusher_registers_before_startup_and_shuts_down_before_pool_close(
    monkeypatch,
) -> None:
    """Team apps register the process-local usage flusher so LLM calls made in
    this worker (not the unified-api gateway) reach llm_call_records."""
    import shared.postgres

    order: list[str] = []
    monkeypatch.setattr(factory, "_register_usage_flusher", lambda _t: order.append("usage_register"))
    monkeypatch.setattr(factory, "_shutdown_usage_flusher", lambda _t: order.append("usage_shutdown"))
    monkeypatch.setattr(shared.postgres, "register_team_schemas", lambda s: order.append("schema"))
    monkeypatch.setattr(shared.postgres, "close_pool", lambda: order.append("close"))

    app = create_team_app(
        service_name="svc",
        team_key="tk",
        title="T",
        postgres_schema=object(),
        on_startup=lambda: order.append("startup"),
        on_shutdown=lambda: order.append("shutdown"),
    )
    with TestClient(app):
        assert order == ["schema", "usage_register", "startup"]
    assert order == [
        "schema",
        "usage_register",
        "startup",
        "shutdown",
        "usage_shutdown",
        "close",
    ]


def test_usage_flusher_registers_without_postgres_schema(monkeypatch) -> None:
    """Usage persistence is independent of the team's own schema."""
    order: list[str] = []
    monkeypatch.setattr(factory, "_register_usage_flusher", lambda _t: order.append("usage_register"))
    monkeypatch.setattr(factory, "_shutdown_usage_flusher", lambda _t: order.append("usage_shutdown"))

    app = create_team_app(service_name="svc", team_key="tk", title="T")
    with TestClient(app):
        assert order == ["usage_register"]
    assert order == ["usage_register", "usage_shutdown"]


def test_register_usage_flusher_helper_swallows_failure(monkeypatch, caplog) -> None:
    import logging
    import sys
    import types

    monkeypatch.setattr(factory, "_register_usage_flusher", _REAL_REGISTER_USAGE)
    fake = types.ModuleType("llm_service.usage_flusher")

    def boom() -> None:
        raise RuntimeError("nope")

    fake.register_usage_flusher = boom
    monkeypatch.setitem(sys.modules, "llm_service.usage_flusher", fake)
    with caplog.at_level(logging.WARNING, logger=factory.logger.name):
        factory._register_usage_flusher("tk")
    assert any("llm usage flusher registration failed" in r.getMessage() for r in caplog.records)


def test_shutdown_usage_flusher_helper_swallows_failure(monkeypatch, caplog) -> None:
    import logging
    import sys
    import types

    monkeypatch.setattr(factory, "_shutdown_usage_flusher", _REAL_SHUTDOWN_USAGE)
    fake = types.ModuleType("llm_service.usage_flusher")

    def boom() -> None:
        raise RuntimeError("nope")

    fake.shutdown = boom
    monkeypatch.setitem(sys.modules, "llm_service.usage_flusher", fake)
    with caplog.at_level(logging.WARNING, logger=factory.logger.name):
        factory._shutdown_usage_flusher("tk")
    assert any("llm usage flusher shutdown failed" in r.getMessage() for r in caplog.records)


def test_create_team_app_rejects_lifespan_in_fastapi_kwargs() -> None:
    # lifespan is set by the factory and is not a named param, so a caller-passed
    # one lands in **fastapi_kwargs and would silently collide inside FastAPI;
    # reject it up front with a clear ValueError. (title/version are named params,
    # so Python raises TypeError on a duplicate before this check is even reached.)
    with pytest.raises(ValueError, match="lifespan"):
        create_team_app(service_name="svc", team_key="tk", title="T", lifespan=lambda _app: None)


@pytest.mark.anyio
async def test_maybe_call_handles_none_sync_and_async() -> None:
    ran = []
    await factory._maybe_call(None)  # no-op
    await factory._maybe_call(lambda: ran.append("sync"))

    async def _a() -> None:
        ran.append("async")

    await factory._maybe_call(_a)
    assert ran == ["sync", "async"]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
