"""Tests for the issue grooming Temporal module.

Covers the Pattern-A export contract (importing never boots a worker or
touches ``TEMPORAL_ADDRESS``, ``WORKFLOWS``/``ACTIVITIES`` are exported, the
typed request payload validates), the real ``run_issue_grooming_activity``
body (missing-token failure, success/cancelled/exception paths, token
scrubbing, heartbeat-interval env parsing, the update-callback's
single-liveness-owner invariant), and ``IssueGroomingWorkflow.run``, which
schedules that activity and propagates both success and failure outcomes
unchanged. A monkeypatched tier (no Temporal server) proves the scheduling
shape; a ``WorkflowEnvironment`` integration tier proves dispatch-by-
registered-name against a substitute activity through a real (ephemeral)
Temporal server.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any, Dict

import pytest
from pydantic import ValidationError
from temporalio import activity

from software_engineering_team.temporal.issue_grooming_workflow import (
    ACTIVITIES,
    WORKFLOWS,
    IssueGroomingRunRequest,
    IssueGroomingRunResult,
    IssueGroomingWorkflow,
    _grooming_heartbeat_interval_s,
    _grooming_update_callback,
    run_issue_grooming_activity,
)

_VALID_REQUEST = {"job_id": "j1", "owner": "acme", "repo": "widgets", "issue_number": 42}


@pytest.fixture
def patched_coding_job_store(monkeypatch: pytest.MonkeyPatch, fake_job_client):
    """Route the coding-team ``job_store._client`` factory through the in-memory fake.

    Mirrors ``conftest.py``'s ``patched_job_store`` fixture, but targets
    ``software_engineering_team.job_store`` (the coding-team job store this
    activity's ``job_id`` actually belongs to) rather than
    ``software_engineering_team.shared.job_store`` (a different job-store, used
    by the SE 4-phase pipeline).
    """
    from software_engineering_team import job_store as js

    monkeypatch.setattr(js, "_client", lambda cache_dir=js.DEFAULT_CACHE_DIR: fake_job_client)
    return fake_job_client


# ---------------------------------------------------------------------------
# Pattern-A export scaffold (unchanged by the real activity body)
# ---------------------------------------------------------------------------


def test_import_does_not_require_temporal_address(monkeypatch: pytest.MonkeyPatch) -> None:
    """Importing the module must not depend on ``TEMPORAL_ADDRESS`` being set.

    Preconditions: none.
    Postconditions: re-importing the already-loaded module raises nothing,
    proving no import-time worker boot or env read is load-bearing.
    """
    monkeypatch.delenv("TEMPORAL_ADDRESS", raising=False)
    import importlib

    import software_engineering_team.temporal.issue_grooming_workflow as mod

    importlib.reload(mod)


def test_exports_workflows_and_activities() -> None:
    assert WORKFLOWS == [IssueGroomingWorkflow]
    assert ACTIVITIES == [run_issue_grooming_activity]


def test_run_request_validates_required_fields() -> None:
    IssueGroomingRunRequest.model_validate(_VALID_REQUEST)
    with pytest.raises(ValidationError):
        IssueGroomingRunRequest.model_validate({"owner": "acme", "repo": "widgets"})


def test_run_result_defaults() -> None:
    result = IssueGroomingRunResult(status="pending")
    assert result.phase is None
    assert result.grooming is None


# --------------------------------------------------------------- IssueGroomingWorkflow.run


def _patch_execute(monkeypatch: pytest.MonkeyPatch, result=None, error: Exception | None = None):
    """Patch ``workflow.execute_activity`` to return ``result`` or raise ``error``.

    Preconditions:
        - Exactly one of ``result``/``error`` is meaningfully set (an ``error``
          takes precedence when both are provided).
    Postconditions:
        - Returns ``calls``, a list that accumulates each ``(fn, request)``
          pair the fake receives, for identity/content assertions.
    """
    calls: list = []

    async def _fake_exec(fn, request, **_kw):
        calls.append((fn, request))
        if error is not None:
            raise error
        return result

    monkeypatch.setattr("temporalio.workflow.execute_activity", _fake_exec)
    return calls


def test_run_happy_path_returns_activity_result_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """A successful activity result is returned as-is -- no re-wrapping into
    ``IssueGroomingRunResult``."""
    canned = {"status": "completed", "phase": "b", "grooming": {"score": 7}}
    calls = _patch_execute(monkeypatch, result=canned)
    workflow_obj = IssueGroomingWorkflow()

    result = asyncio.run(workflow_obj.run(dict(_VALID_REQUEST)))

    assert result == canned
    assert len(calls) == 1
    fn, request = calls[0]
    # fn.__name__ rather than an `is` identity check: an earlier test in this
    # module (test_import_does_not_require_temporal_address) reloads the
    # module under test, rebinding its module-level function objects in
    # place -- an `is` comparison against the name this file imported before
    # that reload would spuriously fail depending on test execution order.
    assert fn.__name__ == run_issue_grooming_activity.__name__ == "run_issue_grooming_activity"
    assert request == _VALID_REQUEST


def test_run_propagates_activity_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    """An activity failure propagates uncaught after the job is terminalized --
    the original exception is never swallowed or rewrapped, so Temporal marks
    the workflow itself terminally failed."""
    _patch_execute(monkeypatch, error=RuntimeError("boom"))
    workflow_obj = IssueGroomingWorkflow()

    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(workflow_obj.run(dict(_VALID_REQUEST)))


# ----------------------------------------------------------- job-store terminalization
#
# Every fake dispatcher below matches on ``fn.__name__`` rather than an `is` identity
# check against the module-level ``run_issue_grooming_activity`` import at the top of
# this file -- same reasoning as ``test_run_happy_path_returns_activity_result_unchanged``:
# an earlier test in this module reloads ``issue_grooming_workflow``, which rebinds that
# name to a new function object, so an identity check would spuriously fail depending on
# test execution order.


def test_run_marks_job_failed_on_activity_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    """A plain activity failure schedules mark-failed with a scrubbed error, then
    re-raises the original exception unchanged."""
    calls: list = []

    async def _fake_exec(fn, request, **_kw):
        calls.append((fn.__name__, dict(request)))
        if fn.__name__ == "run_issue_grooming_activity":
            raise RuntimeError("grooming boom")
        if fn.__name__ == "mark_coding_team_job_failed_activity":
            return {"job_id": request["job_id"], "status": "failed"}
        raise AssertionError(f"unexpected activity {fn.__name__}")

    monkeypatch.setattr("temporalio.workflow.execute_activity", _fake_exec)
    workflow_obj = IssueGroomingWorkflow()

    with pytest.raises(RuntimeError, match="grooming boom"):
        asyncio.run(workflow_obj.run(dict(_VALID_REQUEST)))

    assert [name for name, _ in calls] == [
        "run_issue_grooming_activity",
        "mark_coding_team_job_failed_activity",
    ]
    assert calls[1][1] == {"job_id": "j1", "error": "grooming boom"}


def test_run_marks_job_failed_with_fallback_message_for_empty_str_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An exception whose ``str()`` is empty still produces a non-empty mark-failed error."""

    class _EmptyStrError(RuntimeError):
        def __str__(self) -> str:
            return ""

    calls: list = []

    async def _fake_exec(fn, request, **_kw):
        calls.append((fn.__name__, dict(request)))
        if fn.__name__ == "run_issue_grooming_activity":
            raise _EmptyStrError()
        if fn.__name__ == "mark_coding_team_job_failed_activity":
            return {"job_id": request["job_id"], "status": "failed"}
        raise AssertionError(f"unexpected activity {fn.__name__}")

    monkeypatch.setattr("temporalio.workflow.execute_activity", _fake_exec)
    workflow_obj = IssueGroomingWorkflow()

    with pytest.raises(_EmptyStrError):
        asyncio.run(workflow_obj.run(dict(_VALID_REQUEST)))

    assert calls[1][1]["error"] == "_EmptyStrError: issue grooming run failed"


def test_run_marks_job_cancelled_on_temporal_cancellation(monkeypatch: pytest.MonkeyPatch) -> None:
    """A Temporal cancellation schedules mark-cancelled (not mark-failed), then
    re-raises the cancellation unchanged."""
    calls: list = []

    async def _fake_exec(fn, request, **_kw):
        calls.append((fn.__name__, dict(request)))
        if fn.__name__ == "run_issue_grooming_activity":
            raise asyncio.CancelledError()
        if fn.__name__ == "mark_coding_team_job_cancelled_activity":
            return {"job_id": request["job_id"], "status": "cancelled"}
        raise AssertionError(f"unexpected activity {fn.__name__}")

    monkeypatch.setattr("temporalio.workflow.execute_activity", _fake_exec)
    workflow_obj = IssueGroomingWorkflow()

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(workflow_obj.run(dict(_VALID_REQUEST)))

    assert [name for name, _ in calls] == [
        "run_issue_grooming_activity",
        "mark_coding_team_job_cancelled_activity",
    ]
    assert calls[1][1] == {"job_id": "j1"}


def test_run_marks_job_cancelled_on_activity_error_wrapping_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ``ActivityError`` whose ``cause`` is a ``CancelledError`` (the shape Temporal
    actually raises for an activity-side cancellation, as opposed to a bare
    ``asyncio.CancelledError`` delivered to the workflow itself) is still routed
    through mark-cancelled via the ``is_cancelled_exception`` branch of the generic
    ``except Exception`` handler -- not mark-failed."""
    from temporalio.exceptions import ActivityError
    from temporalio.exceptions import CancelledError as TemporalCancelledError

    cause = TemporalCancelledError("activity cancelled")
    activity_error = ActivityError(
        "activity task cancelled",
        scheduled_event_id=1,
        started_event_id=2,
        identity="test-identity",
        activity_type="issue_grooming_run",
        activity_id="1",
        retry_state=None,
    )
    activity_error.__cause__ = cause

    calls: list = []

    async def _fake_exec(fn, request, **_kw):
        calls.append((fn.__name__, dict(request)))
        if fn.__name__ == "run_issue_grooming_activity":
            raise activity_error
        if fn.__name__ == "mark_coding_team_job_cancelled_activity":
            return {"job_id": request["job_id"], "status": "cancelled"}
        raise AssertionError(f"unexpected activity {fn.__name__}")

    monkeypatch.setattr("temporalio.workflow.execute_activity", _fake_exec)
    workflow_obj = IssueGroomingWorkflow()

    with pytest.raises(ActivityError):
        asyncio.run(workflow_obj.run(dict(_VALID_REQUEST)))

    assert [name for name, _ in calls] == [
        "run_issue_grooming_activity",
        "mark_coding_team_job_cancelled_activity",
    ]
    assert calls[1][1] == {"job_id": "j1"}


def test_run_terminalize_failure_does_not_mask_original_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing mark-failed call is logged, not raised -- the original
    activity exception still propagates unchanged."""

    async def _fake_exec(fn, request, **_kw):
        if fn.__name__ == "run_issue_grooming_activity":
            raise RuntimeError("grooming boom")
        if fn.__name__ == "mark_coding_team_job_failed_activity":
            raise RuntimeError("job service unavailable")
        raise AssertionError(f"unexpected activity {fn.__name__}")

    monkeypatch.setattr("temporalio.workflow.execute_activity", _fake_exec)
    workflow_obj = IssueGroomingWorkflow()

    with pytest.raises(RuntimeError, match="grooming boom"):
        asyncio.run(workflow_obj.run(dict(_VALID_REQUEST)))


def test_run_terminalize_cancellation_does_not_mask_original_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bare ``asyncio.CancelledError`` raised *by the terminalize activity
    itself* (e.g. the whole workflow tearing down concurrently) must be
    swallowed too -- it is a ``BaseException``, not caught by ``except
    Exception`` alone, so ``_best_effort_terminalize`` needs its own explicit
    branch or this cancellation would replace the original activity error
    that ``run`` is supposed to re-raise unchanged."""

    async def _fake_exec(fn, request, **_kw):
        if fn.__name__ == "run_issue_grooming_activity":
            raise RuntimeError("grooming boom")
        if fn.__name__ == "mark_coding_team_job_failed_activity":
            raise asyncio.CancelledError()
        raise AssertionError(f"unexpected activity {fn.__name__}")

    monkeypatch.setattr("temporalio.workflow.execute_activity", _fake_exec)
    workflow_obj = IssueGroomingWorkflow()

    with pytest.raises(RuntimeError, match="grooming boom"):
        asyncio.run(workflow_obj.run(dict(_VALID_REQUEST)))


def test_run_validates_before_scheduling_activity(monkeypatch: pytest.MonkeyPatch) -> None:
    """A malformed request raises ``ValidationError`` before any activity is
    scheduled -- fail fast rather than opaquely failing inside a scheduled
    activity."""

    async def _no_exec(*_a, **_kw):  # pragma: no cover - must not be called
        raise AssertionError("execute_activity must not be called for an invalid request")

    monkeypatch.setattr("temporalio.workflow.execute_activity", _no_exec)
    workflow_obj = IssueGroomingWorkflow()

    with pytest.raises(ValidationError):
        asyncio.run(workflow_obj.run({"owner": "acme"}))


# ---------------------------------------------------------------------------
# run_issue_grooming_activity
# ---------------------------------------------------------------------------


def test_activity_validates_before_touching_job_or_github(monkeypatch: pytest.MonkeyPatch) -> None:
    """A malformed request raises ValidationError before any job-store or GitHub I/O."""
    from software_engineering_team import job_store as js

    def _boom(*_a: Any, **_kw: Any) -> None:
        raise AssertionError("job store must not be touched before request validation")

    monkeypatch.setattr(js, "get_job", _boom)
    monkeypatch.setattr(js, "update_job", _boom)

    with pytest.raises(ValidationError):
        run_issue_grooming_activity({"owner": "acme"})


def test_activity_missing_github_token_marks_job_failed(
    monkeypatch: pytest.MonkeyPatch, patched_coding_job_store
) -> None:
    from software_engineering_team import job_store as js

    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    js.create_job("groom-1", repo_path="n/a")

    with pytest.raises(ValueError, match="no usable GitHub token"):
        run_issue_grooming_activity({**_VALID_REQUEST, "job_id": "groom-1"})

    job = js.get_job("groom-1")
    assert job["status"] == "failed"
    assert "GitHub token" in job["error"]


def test_activity_success_path_returns_result(
    monkeypatch: pytest.MonkeyPatch, patched_coding_job_store
) -> None:
    import software_engineering_team.github_source.issue_grooming_runner as runner_module
    from software_engineering_team import job_store as js
    from software_engineering_team.models import JobStatus

    monkeypatch.setenv("GITHUB_TOKEN", "fake-token")
    js.create_job("groom-2", repo_path="n/a")

    captured_init: Dict[str, Any] = {}

    class _StubRunner:
        def __init__(self, client, *, update_job_fn=None, get_job_fn=None) -> None:
            captured_init["update_job_fn"] = update_job_fn
            captured_init["get_job_fn"] = get_job_fn

        def run(self, job_id: str, owner: str, repo: str, issue_number: int) -> Dict[str, Any]:
            grooming = {"score": {"aggregate": 3}}
            captured_init["update_job_fn"](
                status=JobStatus.COMPLETED.value, phase="done", progress=100, grooming=grooming
            )
            return grooming

    monkeypatch.setattr(runner_module, "IssueGroomingRunner", _StubRunner)

    result = run_issue_grooming_activity({**_VALID_REQUEST, "job_id": "groom-2"})

    assert result == {
        "status": "completed",
        "phase": "done",
        "grooming": {"score": {"aggregate": 3}},
    }
    job = js.get_job("groom-2")
    assert job["status"] == JobStatus.COMPLETED.value
    assert captured_init["get_job_fn"]("groom-2") is not None


def test_activity_cancelled_path_reported_in_result(
    monkeypatch: pytest.MonkeyPatch, patched_coding_job_store
) -> None:
    import software_engineering_team.github_source.issue_grooming_runner as runner_module
    from software_engineering_team import job_store as js
    from software_engineering_team.models import JobStatus

    monkeypatch.setenv("GITHUB_TOKEN", "fake-token")
    js.create_job("groom-3", repo_path="n/a")

    class _CancellingStubRunner:
        def __init__(self, client, *, update_job_fn=None, get_job_fn=None) -> None:
            self._update_job_fn = update_job_fn

        def run(self, job_id: str, owner: str, repo: str, issue_number: int) -> Dict[str, Any]:
            grooming = {"score": {"aggregate": 5}}
            self._update_job_fn(
                status=JobStatus.CANCELLED.value, phase="cancelled", grooming=grooming
            )
            return grooming

    monkeypatch.setattr(runner_module, "IssueGroomingRunner", _CancellingStubRunner)

    result = run_issue_grooming_activity({**_VALID_REQUEST, "job_id": "groom-3"})

    assert result["status"] == "cancelled"
    assert result["phase"] == "cancelled"


def test_activity_exception_path_marks_job_failed_and_reraises(
    monkeypatch: pytest.MonkeyPatch, patched_coding_job_store
) -> None:
    import software_engineering_team.github_source.issue_grooming_runner as runner_module
    from software_engineering_team import job_store as js

    monkeypatch.setenv("GITHUB_TOKEN", "fake-token")
    js.create_job("groom-4", repo_path="n/a")

    class _RaisingStubRunner:
        def __init__(self, client, *, update_job_fn=None, get_job_fn=None) -> None:
            pass

        def run(self, job_id: str, owner: str, repo: str, issue_number: int) -> Dict[str, Any]:
            raise RuntimeError("boom")

    monkeypatch.setattr(runner_module, "IssueGroomingRunner", _RaisingStubRunner)

    with pytest.raises(RuntimeError, match="boom"):
        run_issue_grooming_activity({**_VALID_REQUEST, "job_id": "groom-4"})

    job = js.get_job("groom-4")
    assert job["status"] == "failed"
    assert job["error"] == "boom"
    assert job["status_text"] is None


def test_activity_exception_path_scrubs_token_from_error(
    monkeypatch: pytest.MonkeyPatch, patched_coding_job_store
) -> None:
    """A leaked-credential-shaped exception message must not reach the job store raw."""
    import software_engineering_team.github_source.issue_grooming_runner as runner_module
    from software_engineering_team import job_store as js

    monkeypatch.setenv("GITHUB_TOKEN", "fake-token")
    js.create_job("groom-5", repo_path="n/a")

    leaky_message = (
        "fatal: unable to access 'https://x-access-token:ghp_secret123@github.com/acme/widget.git/'"
    )

    class _LeakyStubRunner:
        def __init__(self, client, *, update_job_fn=None, get_job_fn=None) -> None:
            pass

        def run(self, job_id: str, owner: str, repo: str, issue_number: int) -> Dict[str, Any]:
            raise RuntimeError(leaky_message)

    monkeypatch.setattr(runner_module, "IssueGroomingRunner", _LeakyStubRunner)

    with pytest.raises(RuntimeError):
        run_issue_grooming_activity({**_VALID_REQUEST, "job_id": "groom-5"})

    job = js.get_job("groom-5")
    assert job["status"] == "failed"
    assert "ghp_secret123" not in job["error"]
    assert "https://***@github.com" in job["error"]


def test_grooming_heartbeat_interval_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Valid float honored; a parseable below-floor value clamps to 0.1; garbage/unset fall back to 30s."""
    monkeypatch.setenv("GITHUB_ISSUE_GROOMING_HEARTBEAT_INTERVAL_S", "12.5")
    assert _grooming_heartbeat_interval_s() == 12.5
    monkeypatch.setenv("GITHUB_ISSUE_GROOMING_HEARTBEAT_INTERVAL_S", "0")
    assert _grooming_heartbeat_interval_s() == 0.1
    monkeypatch.setenv("GITHUB_ISSUE_GROOMING_HEARTBEAT_INTERVAL_S", "-5")
    assert _grooming_heartbeat_interval_s() == 0.1
    monkeypatch.setenv("GITHUB_ISSUE_GROOMING_HEARTBEAT_INTERVAL_S", "garbage")
    assert _grooming_heartbeat_interval_s() == 30.0
    monkeypatch.delenv("GITHUB_ISSUE_GROOMING_HEARTBEAT_INTERVAL_S", raising=False)
    assert _grooming_heartbeat_interval_s() == 30.0


def test_grooming_heartbeat_interval_env_floor_boundary_and_whitespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The floor is inclusive (0.1 passes through unclamped); surrounding
    whitespace on an otherwise-valid value does not fall back to the default."""
    monkeypatch.setenv("GITHUB_ISSUE_GROOMING_HEARTBEAT_INTERVAL_S", "0.1")
    assert _grooming_heartbeat_interval_s() == 0.1
    monkeypatch.setenv("GITHUB_ISSUE_GROOMING_HEARTBEAT_INTERVAL_S", "  7  ")
    assert _grooming_heartbeat_interval_s() == 7.0


def test_grooming_update_callback_forwards_without_heartbeat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The callback forwards kwargs to update_job and must NOT heartbeat.

    Liveness is owned solely by the background beater (single-liveness owner), so the
    update callback only persists progress.
    """
    from software_engineering_team import job_store as js
    from software_engineering_team.temporal import issue_grooming_workflow as mod

    captured: Dict[str, Any] = {}
    beats = {"n": 0}
    monkeypatch.setattr(js, "update_job", lambda jid, **kw: captured.update({"jid": jid, **kw}))
    monkeypatch.setattr(
        mod.activity, "heartbeat", lambda *a, **k: beats.__setitem__("n", beats["n"] + 1)
    )

    cb = _grooming_update_callback("job-x")
    cb(status_text="scoring")

    assert captured["jid"] == "job-x"
    assert captured["status_text"] == "scoring"
    assert beats["n"] == 0, "update callback must not emit a heartbeat (single-liveness owner)"


# --------------------------------------------------------------------------- WorkflowEnvironment


@contextlib.asynccontextmanager
async def _workflow_environment_worker(activities):
    """Start a time-skipping ``WorkflowEnvironment`` with an ``IssueGroomingWorkflow``
    worker attached, on a self-contained local task queue.

    Preconditions:
        - ``activities`` is a non-empty list of registered-name substitutes for
          ``run_issue_grooming_activity`` (required, not defaulted to the real
          ``ACTIVITIES``: every caller in this file passes a substitute, since
          exercising the real activity here would require a live
          ``GITHUB_TOKEN`` and GitHub network access -- this tier proves
          Temporal's own name-based dispatch, not the activity body, that's
          the direct-call tests above).
    Postconditions:
        - Yields a live ``WorkflowEnvironment`` with a ``Worker`` listening on
          a literal test-only task queue (no shared task-queue constants
          module exists for grooming yet -- worker/task-queue wiring onto the
          coding-team worker is a follow-up change, out of scope here).
          Skips (rather than fails) when the ephemeral Temporal test-server
          binary can't be downloaded (no egress) -- same caveat as
          ``test_coding_team_temporal_workflow.py``'s helper of the same
          name.
    """
    from temporalio.testing import WorkflowEnvironment
    from temporalio.worker import Worker

    try:
        test_env = await WorkflowEnvironment.start_time_skipping()
    except RuntimeError as exc:
        pytest.skip(f"Temporal ephemeral test server unavailable (no egress?): {exc}")

    async with test_env as env:
        worker = Worker(
            env.client,
            task_queue="issue-grooming-test-queue",
            workflows=[IssueGroomingWorkflow],
            activities=activities,
        )
        async with worker:
            yield env


def _error_chain_text(exc: BaseException) -> str:
    """Return messages from Temporal's nested ``cause`` wrappers.

    Mirrors ``test_coding_team_temporal_workflow.py``/``test_code_review_temporal.py``'s
    helper of the same name -- an ``ActivityError``'s own message rarely
    contains the underlying exception text, which instead lives on a nested
    ``cause``/``__cause__``.
    """
    messages: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        messages.append(str(current))
        current = getattr(current, "cause", None) or current.__cause__
    return " <- ".join(messages)


@activity.defn(name="issue_grooming_run")
def _substitute_grooming_activity_success(request: dict[str, Any]) -> dict[str, Any]:
    """Stand-in for ``run_issue_grooming_activity``, registered under the same
    Temporal activity name, so ``IssueGroomingWorkflow`` dispatches to it
    exactly as it would the real activity -- proving name-based dispatch
    through a live Temporal server without a real GitHub token/network call.
    """
    IssueGroomingRunRequest.model_validate(request)
    return {"status": "completed", "phase": "done", "grooming": {"score": {"aggregate": 1}}}


@activity.defn(name="issue_grooming_run")
def _substitute_grooming_activity_failure(request: dict[str, Any]) -> dict[str, Any]:
    IssueGroomingRunRequest.model_validate(request)
    raise RuntimeError("boom")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_workflow_dispatches_to_activity_by_registered_name() -> None:
    """Acceptance: ``IssueGroomingWorkflow``, run through a real (ephemeral)
    Temporal worker, dispatches to whatever activity is registered under
    ``"issue_grooming_run"`` and returns its result unchanged -- proving
    Temporal's own name-based routing wires workflow -> activity correctly
    end-to-end, which a monkeypatched ``workflow.execute_activity`` cannot
    prove (it bypasses Temporal's routing entirely).
    """
    async with _workflow_environment_worker(
        activities=[_substitute_grooming_activity_success]
    ) as env:
        result = await env.client.execute_workflow(
            IssueGroomingWorkflow.run,
            _VALID_REQUEST,
            id="issue-grooming-workflow-dispatch-test",
            task_queue="issue-grooming-test-queue",
        )

    assert result == {
        "status": "completed",
        "phase": "done",
        "grooming": {"score": {"aggregate": 1}},
    }


@pytest.mark.integration
@pytest.mark.asyncio
async def test_workflow_surfaces_activity_failure_through_real_dispatch() -> None:
    """Acceptance: an activity failure, dispatched through a real Temporal
    worker (not monkeypatched), fails the workflow itself -- proving
    Temporal's own failure propagation, not just this module's own
    exception-handling code.
    """
    from temporalio.client import WorkflowFailureError

    async with _workflow_environment_worker(
        activities=[_substitute_grooming_activity_failure]
    ) as env:
        with pytest.raises(WorkflowFailureError) as exc_info:
            await env.client.execute_workflow(
                IssueGroomingWorkflow.run,
                _VALID_REQUEST,
                id="issue-grooming-workflow-failure-test",
                task_queue="issue-grooming-test-queue",
            )

    cause = exc_info.value.cause
    assert cause is not None
    assert "boom" in _error_chain_text(cause)
