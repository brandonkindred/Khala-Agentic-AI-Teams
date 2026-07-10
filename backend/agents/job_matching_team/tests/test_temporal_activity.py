"""Contract tests for the job_matching Temporal activities + workflow.

The scan pipeline is decomposed into per-phase activities
(``prepare_scan`` -> ``build_queries`` -> ``scan`` -> ``rank`` ->
``finalize_scan``, plus a terminal ``fail_scan``) orchestrated by
``JobMatchingWorkflow``. This file validates each activity's contract with
``temporalio.testing.ActivityEnvironment`` (offline — no LLM/web/Postgres) and
pins the workflow's phase sequencing, short-circuits, and failure handling.

The legacy monolith ``run_scan_activity`` is retained for in-flight-history
drain-out; its original contract tests are kept below.

Package bootstrap and the sync->async dispatch bridge (sandbox-safety, worker
start, ``start_job_matching_workflow``) are covered in
``test_temporal_bootstrap.py``.
"""

from __future__ import annotations

import inspect
import uuid

from temporalio.exceptions import ActivityError, RetryState
from temporalio.testing import ActivityEnvironment

from job_matching_team.models import (
    JobMatchResponse,
    JobPosting,
    RankedJob,
)
from job_matching_team.profile.model import JobSeekerProfile
from job_matching_team.temporal import workflows as wf
from job_matching_team.temporal.workflows import (
    build_queries_activity,
    fail_scan_activity,
    finalize_scan_activity,
    prepare_scan_activity,
    rank_activity,
    run_scan_activity,
    scan_activity,
)

# Fixed workflow-owned run id (see _patch_workflow); the real workflow derives
# it deterministically from workflow.uuid4().
_FIXED_RUN_ID = "12345678-1234-5678-1234-567812345678"


# ---------------------------------------------------------------------------
# Shared test doubles
# ---------------------------------------------------------------------------


class _FakeJobStore:
    """Records status transitions and drives cancellation checks.

    ``existing`` is what ``get_job`` returns at entry (drives the idempotency
    short-circuit and the pre-run cancellation check; default None = untracked
    so the activity proceeds). ``cancelled`` is what ``is_job_cancelled``
    returns for the post-run / failure-branch checks.
    """

    def __init__(
        self,
        *,
        existing: dict | None = None,
        cancelled: bool = False,
        cancel_raises: bool = False,
    ) -> None:
        self.updates: list[dict] = []
        self._existing = existing
        self._cancelled = cancelled
        self._cancel_raises = cancel_raises

    def get_job(self, job_id):
        return self._existing

    def update_job(self, job_id, **fields):
        self.updates.append(fields)

    def is_job_cancelled(self, job_id):
        if self._cancel_raises:
            raise RuntimeError("cancel check unreachable")
        return self._cancelled

    @property
    def statuses(self) -> list[str]:
        return [u["status"] for u in self.updates if "status" in u]


class _FakeStore:
    """In-memory stand-in for ``JobMatchingStore`` — records run bookkeeping."""

    def __init__(
        self,
        *,
        create_raises: bool = False,
        save_raises: bool = False,
        mark_failed_raises: bool = False,
        seen_raises: bool = False,
        seen: set[str] | None = None,
    ) -> None:
        self.create_calls: list[tuple] = []
        self.save_calls: list[tuple] = []
        self.mark_failed_calls: list[tuple] = []
        self.seen_calls = 0
        self._create_raises = create_raises
        self._save_raises = save_raises
        self._mark_failed_raises = mark_failed_raises
        self._seen_raises = seen_raises
        self._seen = seen or set()

    def create_run(self, run_id, profile, request):
        self.create_calls.append((run_id, profile, request))
        if self._create_raises:
            raise RuntimeError("db down")

    def seen_fingerprints(self):
        self.seen_calls += 1
        if self._seen_raises:
            raise RuntimeError("seen query failed")
        return set(self._seen)

    def save_results(self, run_id, ranked, *, total_found, scanned_fingerprints):
        self.save_calls.append((run_id, list(ranked), total_found, list(scanned_fingerprints)))
        if self._save_raises:
            raise RuntimeError("save failed")

    def mark_failed(self, run_id, error):
        self.mark_failed_calls.append((run_id, error))
        if self._mark_failed_raises:
            raise RuntimeError("mark_failed failed")


class _FakeAgent:
    """Callable-class double: records calls, returns a scripted value.

    ``QueryBuilderAgent()``/``JobScannerAgent()``/``JobRankerAgent()`` are all
    constructed with no args and then have a single work method invoked, so one
    shape covers all three.
    """

    def __init__(self, method: str, result, *, raises: bool = False) -> None:
        self._method = method
        self._result = result
        self._raises = raises
        self.calls: list[tuple] = []
        setattr(self, method, self._invoke)

    def _invoke(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self._raises:
            raise RuntimeError(f"{self._method} exploded")
        return self._result


def _patch_job_store(monkeypatch, store: _FakeJobStore) -> None:
    monkeypatch.setattr("job_matching_team.shared.job_store.get_job", store.get_job)
    monkeypatch.setattr("job_matching_team.shared.job_store.update_job", store.update_job)
    monkeypatch.setattr(
        "job_matching_team.shared.job_store.is_job_cancelled", store.is_job_cancelled
    )


def _serialize_postings(postings: list[JobPosting]) -> list[dict]:
    return [p.ensure_fingerprint().model_dump(mode="json") for p in postings]


def _profile_dict(**kwargs) -> dict:
    return JobSeekerProfile(**kwargs).model_dump(mode="json")


# ===========================================================================
# prepare_scan_activity
# ===========================================================================


def test_prepare_ready_creates_run_and_sets_running(monkeypatch):
    job = _FakeJobStore()  # untracked -> proceeds
    store = _FakeStore()
    _patch_job_store(monkeypatch, job)
    monkeypatch.setattr("job_matching_team.store.get_store", lambda: store)
    monkeypatch.setattr(
        "job_matching_team.profile.loader.load_job_seeker_profile",
        lambda: JobSeekerProfile(target_titles=["Engineer"]),
    )

    result = ActivityEnvironment().run(
        prepare_scan_activity, "job-1", {"max_queries": 3, "max_roles": 20, "top_n": 5}, "run-xyz"
    )

    assert result["status"] == "ready"
    assert "run_id" not in result  # the workflow owns run_id; prepare no longer returns it
    assert result["profile"]["target_titles"] == ["Engineer"]
    assert result["skip"] == []
    assert result["store_ok"] is True
    assert (result["max_queries"], result["max_roles"], result["top_n"]) == (3, 20, 5)
    assert job.statuses == ["running"]
    assert len(store.create_calls) == 1
    assert store.create_calls[0][0] == "run-xyz"  # the passed-in run_id is used


def test_prepare_already_completed_replays(monkeypatch):
    stored = {"run_id": "run-1", "ranked_jobs": []}
    job = _FakeJobStore(existing={"status": "completed", "result": stored})
    store = _FakeStore()
    _patch_job_store(monkeypatch, job)
    monkeypatch.setattr("job_matching_team.store.get_store", lambda: store)

    result = ActivityEnvironment().run(prepare_scan_activity, "job-1", {}, "run-xyz")

    assert result == {"status": "already_completed", "result": stored}
    assert job.statuses == []  # no RUNNING flap
    assert store.create_calls == []  # no run row


def test_prepare_cancelled_at_entry(monkeypatch):
    job = _FakeJobStore(existing={"status": "cancelled"})
    store = _FakeStore()
    _patch_job_store(monkeypatch, job)
    monkeypatch.setattr("job_matching_team.store.get_store", lambda: store)

    result = ActivityEnvironment().run(prepare_scan_activity, "job-1", {}, "run-xyz")

    assert result == {"status": "cancelled"}
    assert job.statuses == []
    assert store.create_calls == []


def test_prepare_store_failure_downgrades(monkeypatch):
    job = _FakeJobStore()
    store = _FakeStore(create_raises=True)
    _patch_job_store(monkeypatch, job)
    monkeypatch.setattr("job_matching_team.store.get_store", lambda: store)
    monkeypatch.setattr(
        "job_matching_team.profile.loader.load_job_seeker_profile", JobSeekerProfile
    )

    result = ActivityEnvironment().run(
        prepare_scan_activity, "job-1", {"exclude_seen": True}, "run-xyz"
    )

    assert result["status"] == "ready"
    assert result["store_ok"] is False
    assert result["skip"] == []  # exclude_seen skipped when the store is down
    assert store.seen_calls == 0  # never queried after a failed create_run
    assert job.statuses == ["running"]


def test_prepare_exclude_seen_loads_sorted_skip(monkeypatch):
    job = _FakeJobStore()
    store = _FakeStore(seen={"fp2", "fp1"})
    _patch_job_store(monkeypatch, job)
    monkeypatch.setattr("job_matching_team.store.get_store", lambda: store)
    monkeypatch.setattr(
        "job_matching_team.profile.loader.load_job_seeker_profile", JobSeekerProfile
    )

    result = ActivityEnvironment().run(
        prepare_scan_activity, "job-1", {"exclude_seen": True}, "run-xyz"
    )

    assert result["skip"] == ["fp1", "fp2"]  # sorted
    assert store.seen_calls == 1


def test_prepare_seen_lookup_failure_swallowed(monkeypatch):
    job = _FakeJobStore()
    store = _FakeStore(seen_raises=True)
    _patch_job_store(monkeypatch, job)
    monkeypatch.setattr("job_matching_team.store.get_store", lambda: store)
    monkeypatch.setattr(
        "job_matching_team.profile.loader.load_job_seeker_profile", JobSeekerProfile
    )

    result = ActivityEnvironment().run(
        prepare_scan_activity, "job-1", {"exclude_seen": True}, "run-xyz"
    )

    assert result["status"] == "ready"
    assert result["skip"] == []  # lookup failure is non-fatal
    assert result["store_ok"] is True


# ===========================================================================
# build_queries_activity
# ===========================================================================


def test_build_queries_returns_agent_output(monkeypatch):
    agent = _FakeAgent("build", ["a", "b", "c"])
    monkeypatch.setattr("job_matching_team.agents.query_builder.QueryBuilderAgent", lambda: agent)

    result = ActivityEnvironment().run(
        build_queries_activity, _profile_dict(target_titles=["X"]), 5, "job-1"
    )

    assert result == ["a", "b", "c"]
    (args, kwargs) = agent.calls[0]
    assert isinstance(args[0], JobSeekerProfile)
    assert args[0].target_titles == ["X"]
    assert kwargs == {"max_queries": 5}


# ===========================================================================
# scan_activity
# ===========================================================================


def test_scan_returns_serialised_postings_and_rebuilds_skip(monkeypatch):
    posting = JobPosting(company="Acme", title="Engineer").ensure_fingerprint()
    agent = _FakeAgent("scan", [posting])
    monkeypatch.setattr("job_matching_team.agents.scanner.JobScannerAgent", lambda: agent)

    result = ActivityEnvironment().run(scan_activity, ["q1"], 10, ["fpX"], "job-1")

    assert isinstance(result, list) and result[0]["company"] == "Acme"
    (args, kwargs) = agent.calls[0]
    assert args == (["q1"],)
    assert kwargs == {"max_roles": 10, "skip_fingerprints": {"fpX"}}  # list -> set


# ===========================================================================
# rank_activity
# ===========================================================================


def test_rank_returns_top_and_totals(monkeypatch):
    postings = [
        JobPosting(company="A", title="Eng").ensure_fingerprint(),
        JobPosting(company="B", title="Eng").ensure_fingerprint(),
        JobPosting(company="C", title="Eng").ensure_fingerprint(),
    ]
    ranked = [
        RankedJob(posting=postings[0], score=0.9, recommendation="apply"),
        RankedJob(posting=postings[1], score=0.5, recommendation="maybe"),
        RankedJob(posting=postings[2], score=0.1, recommendation="skip"),
    ]
    agent = _FakeAgent("rank", ranked)
    monkeypatch.setattr("job_matching_team.agents.ranker.JobRankerAgent", lambda: agent)

    result = ActivityEnvironment().run(
        rank_activity, _serialize_postings(postings), _profile_dict(), 2, "job-1"
    )

    assert len(result["top"]) == 2  # truncated to top_n
    assert result["top"][0]["posting"]["company"] == "A"
    assert result["total_found"] == 3
    assert result["scanned_fingerprints"] == [p.fingerprint for p in postings]
    (args, _) = agent.calls[0]
    assert all(isinstance(p, JobPosting) for p in args[0])
    assert isinstance(args[1], JobSeekerProfile)


# ===========================================================================
# finalize_scan_activity
# ===========================================================================


def _one_ranked_dict() -> list[dict]:
    posting = JobPosting(company="Acme", title="Engineer").ensure_fingerprint()
    return [RankedJob(posting=posting, score=0.9, recommendation="apply").model_dump(mode="json")]


def test_finalize_saves_and_completes(monkeypatch):
    job = _FakeJobStore()
    store = _FakeStore()
    _patch_job_store(monkeypatch, job)
    monkeypatch.setattr("job_matching_team.store.get_store", lambda: store)

    result = ActivityEnvironment().run(
        finalize_scan_activity,
        "job-1",
        "run-1",
        _one_ranked_dict(),
        5,
        ["fp1"],
        _profile_dict(),
        True,
    )

    assert result["run_id"] == "run-1"
    assert result["total_found"] == 5
    assert result["total_ranked"] == 1
    assert result["ranked_jobs"][0]["posting"]["company"] == "Acme"
    assert len(store.save_calls) == 1
    assert store.save_calls[0][0] == "run-1"
    assert store.save_calls[0][2] == 5
    assert store.save_calls[0][3] == ["fp1"]
    assert job.updates == [{"status": "completed", "result": result}]


def test_finalize_cancelled_skips_completion(monkeypatch):
    job = _FakeJobStore(cancelled=True)
    store = _FakeStore()
    _patch_job_store(monkeypatch, job)
    monkeypatch.setattr("job_matching_team.store.get_store", lambda: store)

    result = ActivityEnvironment().run(
        finalize_scan_activity,
        "job-1",
        "run-1",
        _one_ranked_dict(),
        1,
        ["fp1"],
        _profile_dict(),
        True,
    )

    assert result == {}
    assert len(store.save_calls) == 1  # run row still saved
    assert job.statuses == []  # job COMPLETED skipped


def test_finalize_completes_when_cancel_check_errors(monkeypatch):
    # A transient job-store error on the cancellation check must not discard a
    # successfully saved result: the scan still COMPLETEs with its payload.
    job = _FakeJobStore(cancel_raises=True)
    store = _FakeStore()
    _patch_job_store(monkeypatch, job)
    monkeypatch.setattr("job_matching_team.store.get_store", lambda: store)

    result = ActivityEnvironment().run(
        finalize_scan_activity,
        "job-1",
        "run-1",
        _one_ranked_dict(),
        1,
        ["fp1"],
        _profile_dict(),
        True,
    )

    assert result["run_id"] == "run-1"
    assert len(store.save_calls) == 1
    assert job.statuses == ["completed"]  # completed despite the cancel-check error


def test_finalize_store_ok_false_skips_save(monkeypatch):
    job = _FakeJobStore()
    store = _FakeStore()
    _patch_job_store(monkeypatch, job)
    monkeypatch.setattr("job_matching_team.store.get_store", lambda: store)

    result = ActivityEnvironment().run(
        finalize_scan_activity,
        "job-1",
        "run-1",
        _one_ranked_dict(),
        1,
        ["fp1"],
        _profile_dict(),
        False,
    )

    assert result["run_id"] == "run-1"
    assert store.save_calls == []  # no persistence attempted
    assert job.statuses == ["completed"]


def test_finalize_save_failure_marks_run_failed_but_still_completes(monkeypatch):
    job = _FakeJobStore()
    store = _FakeStore(save_raises=True)
    _patch_job_store(monkeypatch, job)
    monkeypatch.setattr("job_matching_team.store.get_store", lambda: store)

    result = ActivityEnvironment().run(
        finalize_scan_activity,
        "job-1",
        "run-1",
        _one_ranked_dict(),
        1,
        ["fp1"],
        _profile_dict(),
        True,
    )

    assert result["run_id"] == "run-1"  # response still returned
    assert store.mark_failed_calls == [("run-1", "persisting results failed")]
    assert job.statuses == ["completed"]  # job completes with the payload


def test_finalize_save_and_markfailed_both_fail_are_swallowed(monkeypatch):
    job = _FakeJobStore()
    store = _FakeStore(save_raises=True, mark_failed_raises=True)
    _patch_job_store(monkeypatch, job)
    monkeypatch.setattr("job_matching_team.store.get_store", lambda: store)

    # Neither the save nor the mark-failed error escapes the activity.
    result = ActivityEnvironment().run(
        finalize_scan_activity,
        "job-1",
        "run-1",
        _one_ranked_dict(),
        1,
        ["fp1"],
        _profile_dict(),
        True,
    )

    assert result["run_id"] == "run-1"
    assert job.statuses == ["completed"]


# ===========================================================================
# fail_scan_activity
# ===========================================================================


def test_fail_marks_run_and_job_failed(monkeypatch):
    job = _FakeJobStore()
    store = _FakeStore()
    _patch_job_store(monkeypatch, job)
    monkeypatch.setattr("job_matching_team.store.get_store", lambda: store)

    ActivityEnvironment().run(fail_scan_activity, "job-1", "run-1", "boom", True)

    assert store.mark_failed_calls == [("run-1", "boom")]
    assert job.updates == [{"status": "failed", "error": "boom"}]


def test_fail_skips_job_when_cancelled(monkeypatch):
    job = _FakeJobStore(cancelled=True)
    store = _FakeStore()
    _patch_job_store(monkeypatch, job)
    monkeypatch.setattr("job_matching_team.store.get_store", lambda: store)

    ActivityEnvironment().run(fail_scan_activity, "job-1", "run-1", "boom", True)

    assert store.mark_failed_calls == [("run-1", "boom")]  # run row still marked failed
    assert job.statuses == []  # job FAILED skipped


def test_fail_store_ok_false_skips_run_mark(monkeypatch):
    job = _FakeJobStore()
    store = _FakeStore()
    _patch_job_store(monkeypatch, job)
    monkeypatch.setattr("job_matching_team.store.get_store", lambda: store)

    ActivityEnvironment().run(fail_scan_activity, "job-1", "run-1", "boom", False)

    assert store.mark_failed_calls == []
    assert job.updates == [{"status": "failed", "error": "boom"}]


def test_fail_swallows_mark_failed_error(monkeypatch):
    job = _FakeJobStore()
    store = _FakeStore(mark_failed_raises=True)
    _patch_job_store(monkeypatch, job)
    monkeypatch.setattr("job_matching_team.store.get_store", lambda: store)

    # mark_failed raising must not stop the job-store FAILED write.
    ActivityEnvironment().run(fail_scan_activity, "job-1", "run-1", "boom", True)

    assert job.updates == [{"status": "failed", "error": "boom"}]


def test_fail_swallows_job_store_outage(monkeypatch):
    store = _FakeStore()
    monkeypatch.setattr("job_matching_team.store.get_store", lambda: store)

    def _down(job_id):
        raise RuntimeError("job service unreachable")

    monkeypatch.setattr("job_matching_team.shared.job_store.is_job_cancelled", _down)
    monkeypatch.setattr("job_matching_team.shared.job_store.update_job", _down)

    # is_job_cancelled itself raising (job service down) must be swallowed.
    result = ActivityEnvironment().run(fail_scan_activity, "job-1", "run-1", "boom", False)

    assert result is None  # no exception escaped


# ===========================================================================
# Signature guards
# ===========================================================================


def test_prepare_activity_signature():
    assert list(inspect.signature(prepare_scan_activity).parameters) == [
        "job_id",
        "request",
        "run_id",
    ]


# ===========================================================================
# Workflow orchestration
# ===========================================================================


def _activity_error(activity_type: str) -> ActivityError:
    return ActivityError(
        f"{activity_type} failed",
        scheduled_event_id=1,
        started_event_id=2,
        identity="test",
        activity_type=activity_type,
        activity_id="a1",
        retry_state=RetryState.MAXIMUM_ATTEMPTS_REACHED,
    )


class _WorkflowStub:
    """Replaces ``workflow.execute_activity`` with a scripted dispatcher.

    Returns canned results per activity fn, records every call, and optionally
    raises an ``ActivityError`` for one named phase to exercise the failure path.
    """

    def __init__(self, *, fail_on=None, fail_scan_raises=False) -> None:
        self.calls: list[dict] = []
        self._fail_on = fail_on
        self._fail_scan_raises = fail_scan_raises
        self._canned = {
            wf.prepare_scan_activity: {
                "status": "ready",
                "profile": {"p": 1},
                "skip": ["s"],
                "store_ok": True,
                "max_queries": 3,
                "max_roles": 9,
                "top_n": 2,
            },
            wf.build_queries_activity: ["q1", "q2"],
            wf.scan_activity: [{"company": "Acme"}],
            wf.rank_activity: {
                "top": [{"x": 1}],
                "total_found": 1,
                "scanned_fingerprints": ["fp"],
            },
            wf.finalize_scan_activity: {"run_id": _FIXED_RUN_ID, "final": True},
            wf.fail_scan_activity: None,
        }

    async def __call__(self, activity_fn, *, args, start_to_close_timeout, retry_policy):
        self.calls.append(
            {
                "fn": activity_fn,
                "args": args,
                "timeout": start_to_close_timeout,
                "retry": retry_policy,
            }
        )
        if self._fail_scan_raises and activity_fn is wf.fail_scan_activity:
            raise _activity_error("job_matching_fail_scan")
        if self._fail_on is not None and activity_fn is self._fail_on:
            raise _activity_error("job_matching_scan")
        return self._canned[activity_fn]

    def with_prepare(self, prep: dict) -> "_WorkflowStub":
        self._canned[wf.prepare_scan_activity] = prep
        return self


def _patch_workflow(monkeypatch, stub) -> None:
    """Patch the workflow's Temporal touchpoints for bare-``asyncio.run`` tests.

    ``workflow.uuid4`` and ``workflow.logger`` both need a live workflow context,
    which ``asyncio.run`` of the run coroutine does not provide, so we pin the id
    to a fixed value and route the logger to a plain stdlib logger.
    """
    import logging

    monkeypatch.setattr(wf.workflow, "execute_activity", stub)
    monkeypatch.setattr(wf.workflow, "uuid4", lambda: uuid.UUID(_FIXED_RUN_ID))
    monkeypatch.setattr(wf.workflow, "logger", logging.getLogger("job_matching.wf.test"))


def _run_workflow(job_id="job-1", request=None):
    import asyncio

    return asyncio.run(wf.JobMatchingWorkflow().run(job_id, request or {"top_n": 2}))


def test_workflow_run_delegates_through_phases(monkeypatch):
    stub = _WorkflowStub()
    _patch_workflow(monkeypatch, stub)

    result = _run_workflow()

    assert result == {"run_id": _FIXED_RUN_ID, "final": True}
    # Phases run in order.
    assert [c["fn"] for c in stub.calls] == [
        wf.prepare_scan_activity,
        wf.build_queries_activity,
        wf.scan_activity,
        wf.rank_activity,
        wf.finalize_scan_activity,
    ]
    # Data threading between phases; the workflow-owned run_id reaches prepare
    # and finalize.
    assert stub.calls[0]["args"] == ["job-1", {"top_n": 2}, _FIXED_RUN_ID]
    assert stub.calls[1]["args"] == [{"p": 1}, 3, "job-1"]
    assert stub.calls[2]["args"] == [["q1", "q2"], 9, ["s"], "job-1"]
    assert stub.calls[3]["args"] == [[{"company": "Acme"}], {"p": 1}, 2, "job-1"]
    assert stub.calls[4]["args"] == [
        "job-1",
        _FIXED_RUN_ID,
        [{"x": 1}],
        1,
        ["fp"],
        {"p": 1},
        True,
    ]
    # Every phase carries the bounded default retry policy.
    assert all(c["retry"].maximum_attempts == 3 for c in stub.calls)


def test_workflow_already_completed_short_circuits(monkeypatch):
    stub = _WorkflowStub().with_prepare({"status": "already_completed", "result": {"cached": 1}})
    _patch_workflow(monkeypatch, stub)

    result = _run_workflow()

    assert result == {"cached": 1}
    assert len(stub.calls) == 1  # no downstream phases


def test_workflow_already_completed_missing_result_returns_empty(monkeypatch):
    stub = _WorkflowStub().with_prepare({"status": "already_completed"})
    _patch_workflow(monkeypatch, stub)

    assert _run_workflow() == {}


def test_workflow_cancelled_short_circuits(monkeypatch):
    stub = _WorkflowStub().with_prepare({"status": "cancelled"})
    _patch_workflow(monkeypatch, stub)

    result = _run_workflow()

    assert result == {}
    assert len(stub.calls) == 1


def test_workflow_phase_failure_records_fail_and_returns_empty(monkeypatch):
    stub = _WorkflowStub(fail_on=wf.scan_activity)
    _patch_workflow(monkeypatch, stub)

    result = _run_workflow()

    assert result == {}
    # prepare, build, scan(raises), then fail_scan.
    fns = [c["fn"] for c in stub.calls]
    assert fns[-1] is wf.fail_scan_activity
    assert wf.rank_activity not in fns  # short-circuited on scan failure
    fail_call = stub.calls[-1]
    assert fail_call["args"][:2] == ["job-1", _FIXED_RUN_ID]
    assert fail_call["args"][3] is True  # store_ok threaded through
    assert "job_matching_scan failed" in fail_call["args"][2]
    # fail_scan only flips statuses (idempotent), so it retries like the phases.
    assert fail_call["retry"].maximum_attempts == 3


def test_workflow_fail_scan_error_does_not_fail_workflow(monkeypatch):
    # If even the terminal fail_scan bookkeeping can't be recorded, the workflow
    # must still return {} rather than propagate and end in a Failed state.
    stub = _WorkflowStub(fail_on=wf.scan_activity, fail_scan_raises=True)
    _patch_workflow(monkeypatch, stub)

    result = _run_workflow()

    assert result == {}
    assert stub.calls[-1]["fn"] is wf.fail_scan_activity  # attempted, then swallowed


def test_workflow_never_schedules_legacy_run_scan_activity(monkeypatch):
    # The decomposed workflow must NEVER schedule the legacy monolith — it stays
    # registered only for in-flight-history drain-out. Guard against a future
    # edit reintroducing it alongside the phase activities (duplicate scans).
    stub = _WorkflowStub()
    _patch_workflow(monkeypatch, stub)

    _run_workflow()

    scheduled = [c["fn"] for c in stub.calls]
    assert wf.run_scan_activity not in scheduled


def test_workflow_failure_path_never_schedules_legacy_activity(monkeypatch):
    # The invariant also holds on the failure path (which adds fail_scan).
    stub = _WorkflowStub(fail_on=wf.scan_activity)
    _patch_workflow(monkeypatch, stub)

    _run_workflow()

    assert wf.run_scan_activity not in [c["fn"] for c in stub.calls]


# ===========================================================================
# Legacy monolith: run_scan_activity (retained for in-flight history drain-out)
# ===========================================================================


class _FakeOrchestrator:
    """Test double for JobMatchingOrchestrator — no LLM/web calls."""

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


def _patch_legacy(monkeypatch, orch, store):
    monkeypatch.setattr("job_matching_team.orchestrator.JobMatchingOrchestrator", lambda: orch)
    monkeypatch.setattr("job_matching_team.shared.job_store.get_job", store.get_job)
    monkeypatch.setattr("job_matching_team.shared.job_store.update_job", store.update_job)
    monkeypatch.setattr(
        "job_matching_team.shared.job_store.is_job_cancelled", store.is_job_cancelled
    )


def test_legacy_activity_drives_store_to_completed(monkeypatch):
    orch = _FakeOrchestrator()
    store = _FakeJobStore()
    _patch_legacy(monkeypatch, orch, store)

    result = ActivityEnvironment().run(run_scan_activity, "job-1", {"top_n": 5})

    assert store.statuses == ["running", "completed"]
    assert result["ranked_jobs"][0]["posting"]["company"] == "Acme"
    assert orch.calls == ["job-1"]


def test_legacy_activity_records_failed_and_swallows(monkeypatch):
    orch = _FakeOrchestrator(boom=True)
    store = _FakeJobStore()
    _patch_legacy(monkeypatch, orch, store)

    result = ActivityEnvironment().run(run_scan_activity, "job-2", {})

    assert result == {}
    assert store.statuses == ["running", "failed"]
    assert store.updates[-1]["error"] == "scan exploded"


def test_legacy_activity_swallows_job_store_outage_in_except_branch(monkeypatch):
    orch = _FakeOrchestrator(boom=True)
    store = _FakeJobStore()

    def _down(job_id):
        raise RuntimeError("job service unreachable")

    _patch_legacy(monkeypatch, orch, store)
    monkeypatch.setattr("job_matching_team.shared.job_store.is_job_cancelled", _down)

    result = ActivityEnvironment().run(run_scan_activity, "job-2b", {})

    assert result == {}


def test_legacy_activity_skips_when_cancelled_before_start(monkeypatch):
    orch = _FakeOrchestrator()
    store = _FakeJobStore(existing={"status": "cancelled"})
    _patch_legacy(monkeypatch, orch, store)

    result = ActivityEnvironment().run(run_scan_activity, "job-3", {})

    assert result == {}
    assert store.statuses == []
    assert orch.calls == []


def test_legacy_activity_skips_completion_when_cancelled_mid_run(monkeypatch):
    orch = _FakeOrchestrator()
    store = _FakeJobStore(existing={"status": "pending"}, cancelled=True)
    _patch_legacy(monkeypatch, orch, store)

    result = ActivityEnvironment().run(run_scan_activity, "job-4", {})

    assert result == {}
    assert store.statuses == ["running"]
    assert orch.calls == ["job-4"]


def test_legacy_activity_skips_failed_update_when_cancelled(monkeypatch):
    orch = _FakeOrchestrator(boom=True)
    store = _FakeJobStore(existing={"status": "pending"}, cancelled=True)
    _patch_legacy(monkeypatch, orch, store)

    result = ActivityEnvironment().run(run_scan_activity, "job-5", {})

    assert result == {}
    assert store.statuses == ["running"]


def test_legacy_activity_replays_completed_job_without_rerunning(monkeypatch):
    orch = _FakeOrchestrator()
    stored = {"run_id": "run-1", "ranked_jobs": []}
    store = _FakeJobStore(existing={"status": "completed", "result": stored})
    _patch_legacy(monkeypatch, orch, store)

    result = ActivityEnvironment().run(run_scan_activity, "job-6", {})

    assert result == stored
    assert orch.calls == []
    assert store.statuses == []


def test_legacy_activity_replay_tolerates_missing_result(monkeypatch):
    orch = _FakeOrchestrator()
    store = _FakeJobStore(existing={"status": "completed"})
    _patch_legacy(monkeypatch, orch, store)

    result = ActivityEnvironment().run(run_scan_activity, "job-7", {})

    assert result == {}
    assert orch.calls == []


def test_legacy_activity_signature_takes_job_id_first():
    assert list(inspect.signature(run_scan_activity).parameters) == ["job_id", "request"]
