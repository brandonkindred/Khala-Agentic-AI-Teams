"""Tests for ``api/main.py``'s internal helpers, not its HTTP endpoints
(``test_api.py`` covers the endpoint-level behavior):

* ``mark_all_running_jobs_failed`` (both success and failure paths)
* ``_run_audit_job`` (thread-mode job runner, both branches)
* ``_start_temporal_worker_backstop``
* ``_start_stale_job_monitor`` / ``_on_startup`` (startup wiring)
* the stale-job monitor threshold
* import of ``api.main`` does not start the stale-job monitor

(The job-store write-guard helpers (``_job_is_terminal`` /
``_update_job_terminal`` / ``_update_job_unless_terminal`` / ``_now``) now
live in ``job_store.py`` and are covered by ``test_job_store.py``. The
``run_audit`` Temporal vs thread dispatch branch is covered by
``test_temporal_dispatch.py``.)
"""

from __future__ import annotations

import contextlib
import importlib
import sys
import threading
import unittest.mock as mock
from typing import Any

import pytest
from fastapi.testclient import TestClient

from soc2_compliance_team import job_store
from soc2_compliance_team.api import main as api_main

client = TestClient(api_main.app)


@pytest.fixture(autouse=True)
def _patched(monkeypatch: pytest.MonkeyPatch, fake_job_client):
    monkeypatch.setattr(job_store, "_job_manager", fake_job_client)
    monkeypatch.delenv("TEMPORAL_ADDRESS", raising=False)
    return fake_job_client


# ---------------------------------------------------------------------------
# mark_all_running_jobs_failed
# ---------------------------------------------------------------------------


def test_mark_all_running_jobs_failed_delegates_to_job_manager(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class _FakeJM:
        def mark_all_active_jobs_failed(self, reason):
            captured["reason"] = reason

    monkeypatch.setattr(job_store, "_job_manager", _FakeJM())
    api_main.mark_all_running_jobs_failed("shutdown")
    assert captured["reason"] == "shutdown"


def test_mark_all_running_jobs_failed_swallows_errors(
    monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    class _FakeJM:
        def mark_all_active_jobs_failed(self, reason):
            raise RuntimeError("db down")

    monkeypatch.setattr(job_store, "_job_manager", _FakeJM())
    with caplog.at_level("WARNING"):
        api_main.mark_all_running_jobs_failed("any")
    assert any("mark_all_running_jobs_failed" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# _run_audit_job — direct (sync) invocation, both branches
# ---------------------------------------------------------------------------


def test_run_audit_job_marks_completed(monkeypatch: pytest.MonkeyPatch, fake_job_client) -> None:
    from soc2_compliance_team.models import SOC2AuditResult

    fake_job_client.create_job("j1", status="pending")

    class _FakeOrch:
        def run(self, repo_path):
            return SOC2AuditResult(status="completed", repo_path=str(repo_path))

    monkeypatch.setattr(api_main, "SOC2AuditOrchestrator", _FakeOrch)

    api_main._run_audit_job("j1", "/some/repo")

    job = fake_job_client.get_job("j1")
    assert job["status"] == "completed"
    assert job["current_stage"] == "Completed"
    assert job["result"]["status"] == "completed"


def test_run_audit_job_failure_branch(
    monkeypatch: pytest.MonkeyPatch, fake_job_client, caplog
) -> None:
    fake_job_client.create_job("j2", status="pending")

    class _BoomOrch:
        def run(self, repo_path):
            raise RuntimeError("audit boom")

    monkeypatch.setattr(api_main, "SOC2AuditOrchestrator", _BoomOrch)

    with caplog.at_level("ERROR"):
        api_main._run_audit_job("j2", "/some/repo")

    job = fake_job_client.get_job("j2")
    assert job["status"] == "failed"
    assert job["current_stage"] == "Failed"
    assert "audit boom" in job["error"]


def test_run_audit_job_marks_failed_when_orchestrator_returns_failed_result(
    monkeypatch: pytest.MonkeyPatch, fake_job_client
) -> None:
    """``SOC2AuditOrchestrator.run`` catches its own internal failures and
    *returns* a ``status="failed"`` result rather than raising — the job's
    terminal status must follow ``result.status``, not be hardcoded to
    "completed" just because no exception propagated."""
    from soc2_compliance_team.models import SOC2AuditResult

    fake_job_client.create_job("j3", status="pending")

    class _FailedOrch:
        def run(self, repo_path):
            return SOC2AuditResult(
                status="failed", repo_path=str(repo_path), error="criteria audit timed out"
            )

    monkeypatch.setattr(api_main, "SOC2AuditOrchestrator", _FailedOrch)

    api_main._run_audit_job("j3", "/some/repo")

    job = fake_job_client.get_job("j3")
    assert job["status"] == "failed"
    assert job["current_stage"] == "Failed"
    assert job["error"] == "criteria audit timed out"
    assert job["result"]["status"] == "failed"


# ---------------------------------------------------------------------------
# _start_temporal_worker_backstop
# ---------------------------------------------------------------------------


def test_temporal_worker_backstop_noop_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """With Temporal disabled the backstop delegates and does not raise."""
    monkeypatch.delenv("TEMPORAL_ADDRESS", raising=False)
    # start_soc2_temporal_worker_thread() returns False when disabled; the
    # backstop must complete cleanly.
    api_main._start_temporal_worker_backstop()


def test_temporal_worker_backstop_swallows_errors(monkeypatch: pytest.MonkeyPatch, caplog) -> None:
    """A worker-boot failure is logged, never propagated (must not abort boot)."""

    def _boom() -> bool:
        raise RuntimeError("worker boom")

    monkeypatch.setattr(
        "soc2_compliance_team.temporal.worker.start_soc2_temporal_worker_thread", _boom
    )
    with caplog.at_level("WARNING"):
        api_main._start_temporal_worker_backstop()
    assert any("backstop failed to start" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# _start_stale_job_monitor / _on_startup
# ---------------------------------------------------------------------------


def test_start_stale_job_monitor_binds_stop_event(monkeypatch: pytest.MonkeyPatch) -> None:
    """Startup helper must call ``start_stale_job_monitor`` with the module
    threshold and bind the returned stop Event on ``_stale_monitor_stop``."""
    stop = threading.Event()
    captured: dict[str, Any] = {}

    def _fake_start(client, *, interval_seconds, stale_after_seconds, reason):
        captured["client"] = client
        captured["interval_seconds"] = interval_seconds
        captured["stale_after_seconds"] = stale_after_seconds
        captured["reason"] = reason
        return stop

    monkeypatch.setattr(api_main, "start_stale_job_monitor", _fake_start)
    monkeypatch.setattr(api_main, "_stale_monitor_stop", None)

    api_main._start_stale_job_monitor()

    assert api_main._stale_monitor_stop is stop
    assert captured["client"] is job_store._job_manager
    assert captured["interval_seconds"] == 15.0
    assert captured["stale_after_seconds"] == api_main._STALE_JOB_THRESHOLD_SECONDS
    assert captured["reason"] == "Job heartbeat stale while pending/running"


def test_on_startup_runs_backstop_then_stale_monitor(monkeypatch: pytest.MonkeyPatch) -> None:
    """``_on_startup`` must invoke both the Temporal backstop and the stale
    monitor (``create_team_app`` accepts only one ``on_startup`` callback)."""
    calls: list[str] = []
    monkeypatch.setattr(
        api_main, "_start_temporal_worker_backstop", lambda: calls.append("backstop")
    )
    monkeypatch.setattr(api_main, "_start_stale_job_monitor", lambda: calls.append("stale"))

    api_main._on_startup()

    assert calls == ["backstop", "stale"]


@contextlib.contextmanager
def _purged(prefix: str):
    """Temporarily evict modules under ``prefix`` from ``sys.modules``, then
    restore the exact pre-purge module objects and parent-package attributes.

    Mirrors the helper in ``test_temporal_bootstrap.py`` so a reimport during
    this test cannot leave a distinct ``api.main`` module object in
    ``sys.modules`` for the rest of the session.
    """
    saved = {
        name: mod
        for name, mod in sys.modules.items()
        if name == prefix or name.startswith(prefix + ".")
    }
    for name in saved:
        del sys.modules[name]
    try:
        yield
    finally:
        for name in list(sys.modules):
            if name == prefix or name.startswith(prefix + "."):
                del sys.modules[name]
        sys.modules.update(saved)
        for name, mod in saved.items():
            parent_name, _, leaf = name.rpartition(".")
            parent = sys.modules.get(parent_name)
            if parent is not None:
                setattr(parent, leaf, mod)


def test_importing_api_main_does_not_start_stale_job_monitor() -> None:
    """Loading ``api.main`` must not start the stale-job monitor; that belongs
    exclusively to the FastAPI ``on_startup`` hook."""
    import job_service_client

    with _purged("soc2_compliance_team.api.main"):
        with mock.patch.object(job_service_client, "start_stale_job_monitor") as monitor:
            importlib.import_module("soc2_compliance_team.api.main")
            assert monitor.call_count == 0, (
                f"Module-level start_stale_job_monitor re-introduced "
                f"(call count = {monitor.call_count})."
            )


# ---------------------------------------------------------------------------
# Stale-job monitor threshold
# ---------------------------------------------------------------------------


def test_stale_job_threshold_covers_longest_temporal_activity_ceiling() -> None:
    """The stale-job monitor's threshold must stay comfortably above the
    decomposed Temporal pipeline's longest schedule-to-close ceiling (the
    criterion fan-out / report-writing activities can each go up to an hour
    with no job-row touch) — otherwise it can mark a legitimate long-running
    audit "failed (stale)" before it finishes, and the terminal-write guard
    would then treat that false failure as authoritative."""
    from soc2_compliance_team.temporal import workflows as wmod

    assert (
        api_main._STALE_JOB_THRESHOLD_SECONDS > wmod.AUDIT_SCHEDULE_TO_CLOSE_TIMEOUT.total_seconds()
    )
    assert (
        api_main._STALE_JOB_THRESHOLD_SECONDS
        > wmod.REPORT_SCHEDULE_TO_CLOSE_TIMEOUT.total_seconds()
    )


def test_stale_job_threshold_covers_thread_mode_combined_ceiling() -> None:
    """Thread mode has no per-stage timeout of its own and no job-row touch
    between the criteria and report stages inside ``SOC2AuditOrchestrator.run``
    — the uninterrupted window the stale monitor must outlast is their
    combined ceiling, not either one alone."""
    from soc2_compliance_team import orchestrator as orch_mod

    combined_thread_mode_ceiling = (
        orch_mod._CRITERIA_TIMEOUT_SECONDS + orch_mod._REPORT_TIMEOUT_SECONDS
    )
    assert api_main._STALE_JOB_THRESHOLD_SECONDS > combined_thread_mode_ceiling
