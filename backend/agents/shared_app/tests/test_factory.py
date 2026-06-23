"""Unit tests for the shared team FastAPI app factory (DB-free)."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import shared_app.factory as factory
from shared_app import create_team_app


@pytest.fixture(autouse=True)
def _stub_otel(monkeypatch) -> None:
    """Neutralize OTel global side effects; record that they were invoked."""
    calls = {"init": [], "instrument": []}
    monkeypatch.setattr(factory, "init_otel", lambda **kw: calls["init"].append(kw))
    monkeypatch.setattr(
        factory, "instrument_fastapi_app", lambda app, **kw: calls["instrument"].append(kw)
    )
    factory._test_calls = calls  # type: ignore[attr-defined]
    yield
    delattr(factory, "_test_calls")


def test_create_team_app_wires_otel_and_returns_app() -> None:
    app = create_team_app(service_name="svc", team_key="tk", title="T", version="9.9")
    assert isinstance(app, FastAPI)
    assert app.title == "T" and app.version == "9.9"
    assert factory._test_calls["init"] == [{"service_name": "svc", "team_key": "tk"}]
    # excluded_urls is always forwarded to the instrumentor (None when unset).
    assert factory._test_calls["instrument"] == [{"team_key": "tk", "excluded_urls": None}]


def test_excluded_urls_forwarded_to_instrument() -> None:
    create_team_app(service_name="svc", team_key="tk", title="T", excluded_urls="metrics,healthz")
    assert factory._test_calls["instrument"] == [
        {"team_key": "tk", "excluded_urls": "metrics,healthz"}
    ]


def test_fastapi_kwargs_passthrough() -> None:
    app = create_team_app(
        service_name="svc", team_key="tk", title="T", description="d", docs_url="/d"
    )
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


def test_no_postgres_schema_skips_db_wiring(monkeypatch) -> None:
    import shared_postgres

    called = []
    monkeypatch.setattr(shared_postgres, "register_team_schemas", lambda s: called.append(s))
    monkeypatch.setattr(shared_postgres, "close_pool", lambda: called.append("close"))

    app = create_team_app(service_name="svc", team_key="tk", title="T")
    with TestClient(app):
        pass
    assert called == []  # postgres_schema=None ⇒ no register / no close


def test_postgres_schema_registers_on_startup_and_closes_on_shutdown(monkeypatch) -> None:
    import shared_postgres

    events = []
    monkeypatch.setattr(
        shared_postgres, "register_team_schemas", lambda s: events.append(("register", s))
    )
    monkeypatch.setattr(shared_postgres, "close_pool", lambda: events.append(("close", None)))

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
    import shared_postgres

    def boom(_schema) -> None:
        raise RuntimeError("pg down")

    monkeypatch.setattr(shared_postgres, "register_team_schemas", boom)
    monkeypatch.setattr(shared_postgres, "close_pool", lambda: None)

    app = create_team_app(service_name="svc", team_key="tk", title="T", postgres_schema=object())
    # Startup must not raise despite the registration failure.
    with TestClient(app):
        pass


def test_close_pool_failure_is_swallowed(monkeypatch) -> None:
    import shared_postgres

    monkeypatch.setattr(shared_postgres, "register_team_schemas", lambda s: None)

    def boom() -> None:
        raise RuntimeError("close failed")

    monkeypatch.setattr(shared_postgres, "close_pool", boom)
    app = create_team_app(service_name="svc", team_key="tk", title="T", postgres_schema=object())
    with TestClient(app):
        pass  # shutdown must not raise


def test_on_startup_failure_still_closes_pool(monkeypatch) -> None:
    # A raising on_startup must NOT leak the pool register_team_schemas opened:
    # teardown (close_pool) runs regardless.
    import shared_postgres

    closed = []
    monkeypatch.setattr(shared_postgres, "register_team_schemas", lambda s: None)
    monkeypatch.setattr(shared_postgres, "close_pool", lambda: closed.append(True))

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
