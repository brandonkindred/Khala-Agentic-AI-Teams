"""Contract tests for the job_matching Temporal activity.

``run_scan_activity(job_id, request)`` must drive the shared job store through
the same PENDING -> RUNNING -> COMPLETED/FAILED transitions as the API's
``_run_scan_background``, honour cooperative cancellation, and swallow business
exceptions (record FAILED, do not re-raise) so a deterministic failure does not
trigger a Temporal retry storm.
"""

from __future__ import annotations

import inspect

from temporalio.testing import ActivityEnvironment

from job_matching_team.models import JobMatchResponse, JobPosting, RankedJob
from job_matching_team.profile.model import JobSeekerProfile
from job_matching_team.temporal.workflows import run_scan_activity


class _FakeOrchestrator:
    def __init__(self, *, boom: bool = False) -> None:
        self._boom = boom
        self.calls: list[str | None] = []

    def run(self, request, *, job_id=None, **kwargs):
        self.calls.append(job_id)
        if self._boom:
            raise RuntimeError("scan exploded")
        posting = JobPosting(company="Acme", title="Engineer").ensure_fingerprint()
        return JobMatchResponse(
            run_id="run-1",
            ranked_jobs=[RankedJob(posting=posting, score=0.9, recommendation="apply")],
            total_found=1,
            total_ranked=1,
            profile_snapshot=JobSeekerProfile(),
        )


class _FakeJobStore:
    """Records status transitions and drives cancellation checks.

    ``existing`` is what ``get_job`` returns (default None — job untracked by the
    fake, so the idempotency guard falls through to a normal run).
    """

    def __init__(self, *, cancelled_after: int | None = None, existing: dict | None = None) -> None:
        self.updates: list[dict] = []
        self._cancel_checks = 0
        self._cancelled_after = cancelled_after
        self._existing = existing

    def get_job(self, job_id):
        return self._existing

    def update_job(self, job_id, **fields):
        self.updates.append(fields)

    def is_job_cancelled(self, job_id):
        self._cancel_checks += 1
        if self._cancelled_after is None:
            return False
        return self._cancel_checks > self._cancelled_after

    @property
    def statuses(self) -> list[str]:
        return [u["status"] for u in self.updates if "status" in u]


def _patch(monkeypatch, orch, store):
    monkeypatch.setattr("job_matching_team.orchestrator.JobMatchingOrchestrator", lambda: orch)
    monkeypatch.setattr("job_matching_team.shared.job_store.get_job", store.get_job)
    monkeypatch.setattr("job_matching_team.shared.job_store.update_job", store.update_job)
    monkeypatch.setattr(
        "job_matching_team.shared.job_store.is_job_cancelled", store.is_job_cancelled
    )


def test_activity_drives_store_to_completed(monkeypatch):
    orch = _FakeOrchestrator()
    store = _FakeJobStore()
    _patch(monkeypatch, orch, store)

    result = ActivityEnvironment().run(run_scan_activity, "job-1", {"top_n": 5})

    assert store.statuses == ["running", "completed"]
    assert store.updates[-1]["result"]["ranked_jobs"][0]["posting"]["company"] == "Acme"
    assert result["ranked_jobs"][0]["posting"]["company"] == "Acme"
    # Orchestrator gets the API job id for LLM attribution.
    assert orch.calls == ["job-1"]


def test_activity_records_failed_and_swallows(monkeypatch):
    orch = _FakeOrchestrator(boom=True)
    store = _FakeJobStore()
    _patch(monkeypatch, orch, store)

    result = ActivityEnvironment().run(run_scan_activity, "job-2", {})

    assert result == {}
    assert store.statuses == ["running", "failed"]
    assert store.updates[-1]["error"] == "scan exploded"


def test_activity_skips_when_cancelled_before_start(monkeypatch):
    orch = _FakeOrchestrator()
    # Cancelled on the very first check (cancelled_after=0 => check #1 is True).
    store = _FakeJobStore(cancelled_after=0)
    _patch(monkeypatch, orch, store)

    result = ActivityEnvironment().run(run_scan_activity, "job-3", {})

    assert result == {}
    assert store.statuses == []  # never set RUNNING
    assert orch.calls == []  # orchestrator never ran


def test_activity_skips_completion_when_cancelled_mid_run(monkeypatch):
    orch = _FakeOrchestrator()
    # First check (before start) False, second check (after run) True.
    store = _FakeJobStore(cancelled_after=1)
    _patch(monkeypatch, orch, store)

    result = ActivityEnvironment().run(run_scan_activity, "job-4", {})

    assert result == {}
    assert store.statuses == ["running"]  # RUNNING set, COMPLETED skipped
    assert orch.calls == ["job-4"]


def test_activity_skips_failed_update_when_cancelled(monkeypatch):
    orch = _FakeOrchestrator(boom=True)
    # First check False (proceed + run), except-branch check True (skip FAILED).
    store = _FakeJobStore(cancelled_after=1)
    _patch(monkeypatch, orch, store)

    result = ActivityEnvironment().run(run_scan_activity, "job-5", {})

    assert result == {}
    assert store.statuses == ["running"]  # RUNNING set, FAILED skipped


def test_activity_replays_completed_job_without_rerunning(monkeypatch):
    """A retry landing on an already-COMPLETED job returns the stored result and
    does not re-run the scan or touch the job store."""
    orch = _FakeOrchestrator()
    stored = {"run_id": "run-1", "ranked_jobs": []}
    store = _FakeJobStore(existing={"status": "completed", "result": stored})
    _patch(monkeypatch, orch, store)

    result = ActivityEnvironment().run(run_scan_activity, "job-6", {})

    assert result == stored
    assert orch.calls == []  # scan not re-run
    assert store.statuses == []  # no status mutation


def test_activity_replay_tolerates_missing_result(monkeypatch):
    """Defensive: a COMPLETED row without a stored result returns an empty dict."""
    orch = _FakeOrchestrator()
    store = _FakeJobStore(existing={"status": "completed"})
    _patch(monkeypatch, orch, store)

    result = ActivityEnvironment().run(run_scan_activity, "job-7", {})

    assert result == {}
    assert orch.calls == []


def test_activity_signature_takes_job_id_first():
    """Regression guard: the workflow calls args=[job_id, request]."""
    sig = inspect.signature(run_scan_activity)
    params = list(sig.parameters)
    assert params == ["job_id", "request"]


def test_workflow_run_delegates_to_activity(monkeypatch):
    """JobMatchingWorkflow.run hands (job_id, request) to run_scan_activity with
    the 30-minute timeout and a bounded retry policy."""
    import asyncio
    from datetime import timedelta

    from job_matching_team.temporal import workflows as wf

    captured: dict = {}

    async def _fake_execute_activity(activity_fn, *, args, start_to_close_timeout, retry_policy):
        captured.update(
            fn=activity_fn,
            args=args,
            timeout=start_to_close_timeout,
            retry_policy=retry_policy,
        )
        return {"ok": True}

    monkeypatch.setattr(wf.workflow, "execute_activity", _fake_execute_activity)

    result = asyncio.run(wf.JobMatchingWorkflow().run("job-9", {"top_n": 1}))

    assert result == {"ok": True}
    assert captured["fn"] is wf.run_scan_activity
    assert captured["args"] == ["job-9", {"top_n": 1}]
    assert captured["timeout"] == timedelta(minutes=30)
    # Bounded retry: a non-idempotent scan must not retry-storm on a crash.
    assert captured["retry_policy"].maximum_attempts == 3
