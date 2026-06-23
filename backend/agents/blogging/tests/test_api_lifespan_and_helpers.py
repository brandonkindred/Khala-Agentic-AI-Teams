"""Lifespan + shutdown helper coverage for ``api/main.py``."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

_blogging_root = Path(__file__).resolve().parent.parent
if str(_blogging_root) not in sys.path:
    sys.path.insert(0, str(_blogging_root))

_spec = importlib.util.spec_from_file_location(
    "blogging_api_main_lifespan",
    _blogging_root / "api" / "main.py",
)
_api_main = sys.modules.get("blogging_api_main_lifespan")
if _api_main is None:
    _api_main = importlib.util.module_from_spec(_spec)
    sys.modules["blogging_api_main_lifespan"] = _api_main
    _spec.loader.exec_module(_api_main)


def test_publish_terminal_event_swallows_exceptions(monkeypatch) -> None:
    """When ``publish`` raises, ``_publish_terminal_event`` returns silently."""
    from shared import job_event_bus

    def boom(*a, **kw):
        raise RuntimeError("down")

    monkeypatch.setattr(job_event_bus, "publish", boom)
    _api_main._publish_terminal_event("job-x", "complete", status="ok")


def test_publish_terminal_event_swallows_import_error(monkeypatch) -> None:
    """If job_event_bus is unimportable, the helper still returns silently."""
    import importlib

    sys.modules.pop("shared.job_event_bus", None)

    real_import = importlib.import_module

    def bomb(name, *a, **kw):
        if "job_event_bus" in name:
            raise ImportError("missing")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(importlib, "import_module", bomb)
    _api_main._publish_terminal_event("job-x", "complete")


def test_run_blogging_service_shutdown_runs_with_helpers(monkeypatch) -> None:
    from shared import blog_job_store

    called = {"stop": False, "temporal": False, "job_svc": False}

    monkeypatch.setattr(
        blog_job_store, "stop_blog_stale_monitor", lambda: called.__setitem__("stop", True)
    )

    class _StubClient:
        def __init__(self, team):
            called["job_svc"] = True

        def mark_all_active_jobs_interrupted(self, *a, **kw):
            pass

    monkeypatch.setattr("job_service_client.JobServiceClient", _StubClient)

    fake_temporal_module = type(sys)("blogging.temporal.worker")

    def shutdown_blogging_temporal_components(worker_shutdown_timeout=8.0):
        called["temporal"] = True

    fake_temporal_module.shutdown_blogging_temporal_components = (
        shutdown_blogging_temporal_components
    )
    monkeypatch.setitem(sys.modules, "blogging.temporal.worker", fake_temporal_module)

    _api_main._run_blogging_service_shutdown()
    assert called["stop"] is True
    assert called["job_svc"] is True


def test_run_blogging_service_shutdown_swallows_inner_errors(monkeypatch) -> None:
    def boom(*a, **kw):
        raise RuntimeError("kaboom")

    from shared import blog_job_store

    monkeypatch.setattr(blog_job_store, "stop_blog_stale_monitor", boom)

    class _AngryClient:
        def __init__(self, team):
            raise RuntimeError("job-svc-down")

    monkeypatch.setattr("job_service_client.JobServiceClient", _AngryClient)

    fake_temporal_module = type(sys)("blogging.temporal.worker")

    def shutdown_blogging_temporal_components(worker_shutdown_timeout=8.0):
        raise RuntimeError("temporal-down")

    fake_temporal_module.shutdown_blogging_temporal_components = (
        shutdown_blogging_temporal_components
    )
    monkeypatch.setitem(sys.modules, "blogging.temporal.worker", fake_temporal_module)

    _api_main._run_blogging_service_shutdown()


def test_blogging_app_lifespan_runs_in_event_loop(monkeypatch) -> None:
    """The factory-built ``app`` registers the Postgres schema on startup and runs
    the real blogging shutdown hook (``_run_blogging_service_shutdown``) on teardown.

    The hook reference is bound into the app at ``create_team_app(...)`` time, so it
    cannot be monkeypatched after the fact; instead we monkeypatch the first thing the
    hook touches (``stop_blog_stale_monitor``) and assert it ran during teardown.
    """
    fake_shared_postgres = type(sys)("shared_postgres")
    registered: list = []
    fake_shared_postgres.register_team_schemas = lambda s: registered.append(s)
    fake_shared_postgres.close_pool = lambda: None
    monkeypatch.setitem(sys.modules, "shared_postgres", fake_shared_postgres)

    from shared import blog_job_store

    shutdown_ran = {"value": False}
    monkeypatch.setattr(
        blog_job_store, "stop_blog_stale_monitor", lambda: shutdown_ran.__setitem__("value", True)
    )

    app = _api_main.app

    async def _drive():
        async with app.router.lifespan_context(app):
            pass

    asyncio.run(_drive())
    assert registered, "expected the blogging Postgres schema to be registered on startup"
    assert shutdown_ran["value"] is True, "expected the blogging shutdown hook to run on teardown"


def test_blogging_app_lifespan_swallows_schema_errors(monkeypatch) -> None:
    """A failing schema registration or pool close must not break app startup/teardown."""
    fake_shared_postgres = type(sys)("shared_postgres")

    def boom_register(*a, **kw):
        raise RuntimeError("register failed")

    def boom_close():
        raise RuntimeError("close failed")

    fake_shared_postgres.register_team_schemas = boom_register
    fake_shared_postgres.close_pool = boom_close
    monkeypatch.setitem(sys.modules, "shared_postgres", fake_shared_postgres)

    # NB: the real _run_blogging_service_shutdown runs on teardown — create_team_app
    # captured it as on_shutdown at construction time, so it cannot be monkeypatched
    # here. It is defensive, so the lifespan completing without raising is what proves
    # the schema register/close failures (boom_*) are swallowed.
    app = _api_main.app

    async def _drive():
        async with app.router.lifespan_context(app):
            pass

    asyncio.run(_drive())
