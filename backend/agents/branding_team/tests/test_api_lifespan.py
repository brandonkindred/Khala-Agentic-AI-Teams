"""Lifespan + shutdown-hook coverage for ``branding_team/api/main.py``.

Unlike ``test_api.py`` (entirely ``@pytest.mark.integration``, skipped by default),
this file runs in the default test suite: it drives ``app.router.lifespan_context``
directly and monkeypatches branding's own module globals, so it needs neither a real
Postgres instance nor a real job service. Generic ``create_team_app`` behavior
(schema-registration/close-pool error swallowing) is covered centrally by
``shared_app/tests/test_factory.py``; these tests only assert branding's own
``on_shutdown`` wiring.
"""

from __future__ import annotations

import asyncio
import threading
from unittest.mock import Mock

from branding_team.api import main as api_main

app = api_main.app


def _drive_lifespan() -> None:
    async def _drive() -> None:
        async with app.router.lifespan_context(app):
            pass

    asyncio.run(_drive())


def test_branding_shutdown_hook_stops_monitor_and_notifies_job_service(monkeypatch) -> None:
    stop_event = threading.Event()
    monkeypatch.setattr(api_main, "_stale_monitor_stop", stop_event)

    mock_executor = Mock()
    monkeypatch.setattr(api_main, "_run_executor", mock_executor)

    calls: list = []

    class _StubJobManager:
        def mark_all_active_jobs_interrupted(
            self, reason, *, http_timeout=30.0, http_max_retries=3
        ):
            calls.append((reason, http_timeout, http_max_retries))
            return []

    monkeypatch.setattr(api_main, "_job_manager", _StubJobManager())

    _drive_lifespan()

    assert stop_event.is_set(), "expected the stale-job monitor to be stopped on shutdown"
    mock_executor.shutdown.assert_called_once_with(wait=False, cancel_futures=True)
    assert calls == [("Branding service shutting down", 5.0, 0)]


def test_branding_shutdown_hook_swallows_job_service_errors(monkeypatch) -> None:
    monkeypatch.setattr(api_main, "_stale_monitor_stop", None)
    monkeypatch.setattr(api_main, "_run_executor", Mock())

    class _AngryJobManager:
        def mark_all_active_jobs_interrupted(self, *a, **kw):
            raise RuntimeError("job-service-down")

    monkeypatch.setattr(api_main, "_job_manager", _AngryJobManager())

    _drive_lifespan()


def test_branding_shutdown_hook_skips_job_notification_when_manager_unavailable(
    monkeypatch,
) -> None:
    monkeypatch.setattr(api_main, "_stale_monitor_stop", None)
    mock_executor = Mock()
    monkeypatch.setattr(api_main, "_run_executor", mock_executor)
    monkeypatch.setattr(api_main, "_job_manager", None)

    _drive_lifespan()

    mock_executor.shutdown.assert_called_once_with(wait=False, cancel_futures=True)
