"""The app lifespan starts the Temporal worker as a backstop.

The docker team_service entrypoint boots the worker per uvicorn worker via
TEAM_TEMPORAL_WORKER_MODULE/FUNC, but every other way the app is served (bare
``uvicorn coding_team.api.main:app``, embedding) has no such hook. Without a
lifespan backstop, a TEMPORAL_ADDRESS-enabled standalone run would dispatch to
a worker that never booted. These tests pin the backstop.
"""

from __future__ import annotations

from coding_team.api import lifecycle


def test_startup_runs_provider_probe_and_worker_backstop(monkeypatch):
    calls: list = []
    monkeypatch.setattr(lifecycle, "_warn_if_no_engine_provider", lambda: calls.append("probe"))
    monkeypatch.setattr(
        lifecycle, "_start_temporal_worker_backstop", lambda: calls.append("worker")
    )

    lifecycle._startup()

    assert calls == ["probe", "worker"]


def test_worker_backstop_starts_worker(monkeypatch):
    import coding_team.temporal.worker as worker_mod

    started: list = []
    monkeypatch.setattr(
        worker_mod, "start_coding_team_temporal_worker_thread", lambda: started.append(True) or True
    )

    lifecycle._start_temporal_worker_backstop()

    assert started == [True]


def test_worker_backstop_swallows_errors(monkeypatch):
    """A broken worker must not block the app from serving traffic."""
    import coding_team.temporal.worker as worker_mod

    def _boom():
        raise RuntimeError("temporal unreachable")

    monkeypatch.setattr(worker_mod, "start_coding_team_temporal_worker_thread", _boom)

    # Must not raise.
    lifecycle._start_temporal_worker_backstop()
