"""Tests for the issue grooming Temporal module.

Covers the Pattern-A export contract (importing never boots a worker or
touches ``TEMPORAL_ADDRESS``, ``WORKFLOWS``/``ACTIVITIES`` are exported, the
typed request payload validates), the still-stub activity (raises
``NotImplementedError`` rather than silently doing nothing -- its real body
is a follow-up change), and ``IssueGroomingWorkflow.run``, which now really
schedules that activity and propagates both success and failure outcomes
unchanged. A monkeypatched tier (no Temporal server) proves the scheduling
shape; a ``WorkflowEnvironment`` integration tier proves dispatch-by-
registered-name against the real (still-stub) activity.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest
from pydantic import ValidationError

from software_engineering_team.temporal.issue_grooming_workflow import (
    ACTIVITIES,
    WORKFLOWS,
    IssueGroomingRunRequest,
    IssueGroomingRunResult,
    IssueGroomingWorkflow,
    run_issue_grooming_activity,
)

_VALID_REQUEST = {"job_id": "j1", "owner": "acme", "repo": "widgets", "issue_number": 42}


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


def test_activity_stub_raises_not_implemented() -> None:
    with pytest.raises(NotImplementedError):
        run_issue_grooming_activity(_VALID_REQUEST)


def test_activity_stub_validates_before_raising() -> None:
    with pytest.raises(ValidationError):
        run_issue_grooming_activity({"owner": "acme"})


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
    """An activity failure propagates uncaught -- no try/except swallows or
    rewraps it, so Temporal marks the workflow itself terminally failed."""
    _patch_execute(monkeypatch, error=RuntimeError("boom"))
    workflow_obj = IssueGroomingWorkflow()

    with pytest.raises(RuntimeError, match="boom"):
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


# --------------------------------------------------------------------------- WorkflowEnvironment


@contextlib.asynccontextmanager
async def _workflow_environment_worker(activities=None):
    """Start a time-skipping ``WorkflowEnvironment`` with an ``IssueGroomingWorkflow``
    worker attached, on a self-contained local task queue.

    Preconditions: none.
    Postconditions:
        - Yields a live ``WorkflowEnvironment`` with a ``Worker`` listening on
          a literal test-only task queue (no shared task-queue constants
          module exists for grooming yet -- worker/task-queue wiring onto the
          coding-team worker is a follow-up change, out of scope here).
          ``activities`` defaults to the real, production ``ACTIVITIES`` (the
          still-stub activity); pass a substitute registered under the same
          ``"issue_grooming_run"`` name to drive the workflow without the
          real stub. Skips (rather than fails) when the ephemeral Temporal
          test-server binary can't be downloaded (no egress) -- same caveat
          as ``test_coding_team_temporal_workflow.py``'s helper of the same
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
            activities=activities if activities is not None else ACTIVITIES,
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


@pytest.mark.integration
@pytest.mark.asyncio
async def test_workflow_against_real_stub_activity_surfaces_not_implemented() -> None:
    """Acceptance: dispatching through a real Temporal worker to the real
    (still-stub) ``run_issue_grooming_activity`` -- registered by name, not
    monkeypatched -- fails the workflow with the activity's
    ``NotImplementedError``. This is the direct proof that "terminal
    failures surface as a failed workflow/job outcome" today, before the
    real Phase A->B activity body lands in a follow-up change.
    """
    from temporalio.client import WorkflowFailureError

    async with _workflow_environment_worker() as env:
        with pytest.raises(WorkflowFailureError) as exc_info:
            await env.client.execute_workflow(
                IssueGroomingWorkflow.run,
                _VALID_REQUEST,
                id="issue-grooming-workflow-not-implemented-test",
                task_queue="issue-grooming-test-queue",
            )

    cause = exc_info.value.cause
    assert cause is not None
    assert "issue grooming activity body is implemented in a follow-up change" in _error_chain_text(
        cause
    )
