"""Unit tests for CodingTeamWorkflow's submit_answers signal + wait_condition skeleton.

Drives ``.run()`` directly as a plain object (no Temporal server, no
``pytest.mark.integration``), patching ``temporalio.workflow.execute_activity``
and ``temporalio.workflow.wait_condition`` in place -- the same lightweight
pattern ``agentic_team_provisioning/tests/test_temporal_activity.py`` uses for
``AgenticPipelineWorkflow``.

``run_pipeline_activity`` now emits ``{"outcome": "paused", ...}`` under
``pause_strategy="return"`` (see ``system_design/hitl_pause_resume_contract.md``
and ``test_coding_team_temporal_activity.py`` for that activity-side behavior
directly). These tests fake the activity's *return value* directly so the
signal / wait_condition / re-invoke SHAPE -- including resume_token
validation and acknowledged_resume_token -- stays isolated from the real
activity's orchestrator-wiring/job-store dependencies. Buffering an early
signal (before a pause is active) and applying answers into
``request["plan_input"]`` remain open future work (not #3988, whose scope is
limited to an integration test proving the existing pause/resume cycle) and
are not covered here. That integration test -- driving the cycle against a
real ``temporalio.testing.WorkflowEnvironment`` rather than these monkeypatch
fakes -- lives at the bottom of this file.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from software_engineering_team.api.coding_team_models import RunRequest
from software_engineering_team.temporal.coding_team_workflow import CodingTeamWorkflow


def _patch_execute(monkeypatch: pytest.MonkeyPatch, results: list) -> tuple[list, list]:
    """Patch workflow.execute_activity to return successive ``results`` per call.

    Returns ``(calls, snapshots)``: ``calls`` records each ``(fn, request)`` pair
    by reference (for identity assertions -- proving the SAME request object is
    reused across the loop), while ``snapshots`` records a ``dict`` copy of
    ``request`` taken at the moment of each call (for content assertions that
    must not be affected by mutations the workflow makes to that same object on
    a later iteration).
    """
    calls: list = []
    snapshots: list = []
    results_iter = iter(results)

    async def _fake_exec(fn, request, **_kw):
        calls.append((fn, request))
        snapshots.append(dict(request))
        return next(results_iter)

    monkeypatch.setattr("temporalio.workflow.execute_activity", _fake_exec)
    return calls, snapshots


def test_run_returns_immediately_when_not_paused(monkeypatch: pytest.MonkeyPatch) -> None:
    """A terminal (non-'outcome': 'paused') activity result returns on the first
    iteration without ever touching wait_condition -- the common case when no
    HITL gate pauses during the run."""
    workflow_obj = CodingTeamWorkflow()
    calls, _ = _patch_execute(monkeypatch, [{"job_id": "j1", "status": "completed"}])

    async def _no_wait(*_a, **_kw):  # pragma: no cover - must not be called
        raise AssertionError("wait_condition must not be called when not paused")

    monkeypatch.setattr("temporalio.workflow.wait_condition", _no_wait)

    result = asyncio.run(workflow_obj.run({"repo_path": "/repo", "plan_input": {}}))

    assert result == {"job_id": "j1", "status": "completed"}
    assert len(calls) == 1


def test_run_raises_on_paused_result_missing_resume_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A paused result without a valid resume_token must fail the workflow task
    deterministically, not silently assign None and let wait_condition's predicate
    become permanently unsatisfiable (submit_answers drops every signal while
    self._active_resume_token is None) -- an unresolvable hang is a much worse
    failure mode than an immediate, diagnosable error."""
    workflow_obj = CodingTeamWorkflow()
    _patch_execute(monkeypatch, [{"outcome": "paused", "job_id": "j1"}])

    async def _no_wait(*_a, **_kw):  # pragma: no cover - must not be called
        raise AssertionError("wait_condition must not be reached with no resume_token")

    monkeypatch.setattr("temporalio.workflow.wait_condition", _no_wait)

    with pytest.raises(ValueError, match="missing a valid resume_token"):
        asyncio.run(workflow_obj.run({"repo_path": "/repo", "plan_input": {}}))


def test_submit_answers_signal_wakes_wait_condition_and_reloops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 'paused' first result makes run() wait on a token-matching submit_answers
    signal; delivering it (a plain method call, simulating Temporal's dispatch)
    wakes wait_condition, and the workflow re-calls the SAME activity object with
    the SAME request, carrying acknowledged_resume_token for that call only."""
    workflow_obj = CodingTeamWorkflow()
    request = {"repo_path": "/repo", "plan_input": {"objective": "o"}}
    calls, snapshots = _patch_execute(
        monkeypatch,
        [
            {"outcome": "paused", "job_id": "j1", "resume_token": "j1:1"},
            {"job_id": "j1", "status": "completed"},
        ],
    )

    async def _fake_wait(pred, timeout=None):
        workflow_obj.submit_answers({"resume_token": "j1:1", "answers": [{"question_id": "q1"}]})
        assert pred()  # the predicate must observe the signal we just delivered

    monkeypatch.setattr("temporalio.workflow.wait_condition", _fake_wait)

    result = asyncio.run(workflow_obj.run(request))

    assert result == {"job_id": "j1", "status": "completed"}
    assert len(calls) == 2
    # Same activity function, same request object, reused across both calls.
    assert calls[0][0] is calls[1][0]
    assert calls[0][1] is request
    assert calls[1][1] is request
    # The first call precedes any pause, so no token has been acknowledged yet.
    assert "acknowledged_resume_token" not in snapshots[0]
    # The second call carries the just-resolved pause's token.
    assert snapshots[1]["acknowledged_resume_token"] == "j1:1"
    # Popped once that call returns -- the field's job is done either way.
    assert "acknowledged_resume_token" not in request


def test_submit_answers_ignores_mismatched_resume_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """A submission for a different (stale, or already-resolved) pause must not
    wake wait_condition -- token validation is the workflow's defense against a
    retried or duplicate HTTP call resolving the wrong pause."""
    workflow_obj = CodingTeamWorkflow()
    workflow_obj._active_resume_token = "current-token"

    workflow_obj.submit_answers({"resume_token": "stale-token", "answers": [{"question_id": "q1"}]})

    assert workflow_obj._submitted_answers is None


def test_submit_answers_ignores_second_submission_for_same_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A double-submit (or two clients racing to answer the same pause) must not
    overwrite the first accepted batch -- first submission per token wins."""
    workflow_obj = CodingTeamWorkflow()
    workflow_obj._active_resume_token = "tok-1"
    first = [{"question_id": "q1", "answer": "yes"}]
    workflow_obj.submit_answers({"resume_token": "tok-1", "answers": first})

    workflow_obj.submit_answers(
        {"resume_token": "tok-1", "answers": [{"question_id": "q1", "answer": "no"}]}
    )

    assert workflow_obj._submitted_answers == first


def test_submit_answers_ignores_signal_with_no_active_pause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A signal arriving before any pause is active (self._active_resume_token is
    None) is dropped rather than buffered -- buffering early signals remains
    open future work (not #3988, which only adds an integration test for the
    existing, intentionally non-buffering cycle)."""
    workflow_obj = CodingTeamWorkflow()
    assert workflow_obj._active_resume_token is None

    workflow_obj.submit_answers({"resume_token": "any-token", "answers": [{"question_id": "q1"}]})

    assert workflow_obj._submitted_answers is None


def test_submit_answers_sets_state_directly(monkeypatch: pytest.MonkeyPatch) -> None:
    """Signal handler contract in isolation: a payload whose resume_token matches
    the active pause has its answers stored (not the whole payload -- the
    resume_token itself is already tracked separately in _active_resume_token)."""
    workflow_obj = CodingTeamWorkflow()
    workflow_obj._active_resume_token = "tok-1"
    assert workflow_obj._submitted_answers is None

    answers = [{"question_id": "q1", "answer": "yes"}]
    workflow_obj.submit_answers({"resume_token": "tok-1", "answers": answers})

    assert workflow_obj._submitted_answers == answers


def test_submit_answers_ignores_non_dict_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    """A malformed signal payload (not a dict) must be dropped, not raise -- an
    unhandled exception here would fail the workflow task and, since Temporal
    replays history, would fail identically forever, stranding the workflow."""
    workflow_obj = CodingTeamWorkflow()
    workflow_obj._active_resume_token = "tok-1"

    workflow_obj.submit_answers("not-a-dict")  # type: ignore[arg-type]

    assert workflow_obj._submitted_answers is None


def test_submit_answers_ignores_non_list_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    """A payload with a matching resume_token but a non-list 'answers' value
    must be dropped rather than stored -- wait_condition's predicate assumes
    a list once _submitted_answers is not None."""
    workflow_obj = CodingTeamWorkflow()
    workflow_obj._active_resume_token = "tok-1"

    workflow_obj.submit_answers({"resume_token": "tok-1", "answers": "not-a-list"})

    assert workflow_obj._submitted_answers is None


def test_submit_answers_ignores_payload_missing_answers_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A payload with a matching resume_token but no 'answers' key at all must
    be dropped rather than raise KeyError."""
    workflow_obj = CodingTeamWorkflow()
    workflow_obj._active_resume_token = "tok-1"

    workflow_obj.submit_answers({"resume_token": "tok-1"})

    assert workflow_obj._submitted_answers is None


def test_run_request_declares_acknowledged_resume_token() -> None:
    """Regression guard: RunRequest must declare acknowledged_resume_token, or
    Pydantic's default ignore-extra-keys behavior silently drops the value
    CodingTeamWorkflow.run sets on request before run_pipeline_activity's
    RunRequest(**request) call ever sees it."""
    parsed = RunRequest(repo_path="/repo", acknowledged_resume_token="j1:1")

    assert parsed.acknowledged_resume_token == "j1:1"


# --------------------------------------------------------------------------- WorkflowEnvironment (#3988)


@contextlib.asynccontextmanager
async def _workflow_environment_worker(activities=None):
    """Shared ``WorkflowEnvironment`` + ``Worker`` startup/teardown for the
    ``CodingTeamWorkflow`` integration test below. Mirrors
    ``test_code_review_temporal.py``'s helper of the same name (the only other
    place in this repo that drives ``temporalio.testing.WorkflowEnvironment``)
    -- see that helper's docstring for why this skips (rather than fails) when
    the ephemeral test-server binary can't be downloaded.

    ``activities`` defaults to the real, production ``ACTIVITIES`` list; pass
    a substitute (e.g. a fake registered under the same
    ``"coding_team_run_pipeline"`` name) to drive the workflow without
    invoking the real orchestrator/job-store/``CodeEngineProvider`` machinery.
    """
    import concurrent.futures

    from temporalio.testing import WorkflowEnvironment
    from temporalio.worker import Worker

    from software_engineering_team.temporal.coding_team_constants import TASK_QUEUE
    from software_engineering_team.temporal.coding_team_workflow import (
        ACTIVITIES,
        CodingTeamWorkflow,
    )

    try:
        test_env = await WorkflowEnvironment.start_time_skipping()
    except RuntimeError as exc:
        pytest.skip(f"Temporal ephemeral test server unavailable (no egress?): {exc}")

    async with test_env as env:
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as activity_executor:
            worker = Worker(
                env.client,
                task_queue=TASK_QUEUE,
                workflows=[CodingTeamWorkflow],
                activities=activities if activities is not None else ACTIVITIES,
                activity_executor=activity_executor,
            )
            async with worker:
                yield env


@pytest.mark.integration
@pytest.mark.asyncio
async def test_workflow_pauses_then_resumes_to_completion_via_signal() -> None:
    """Drive a full pause -> submit_answers signal -> resume -> completion
    cycle against a real (embedded) Temporal test server -- the acceptance
    criterion #3988 exists for. The rest of this file proves the loop SHAPE
    via monkeypatched fakes; this proves the same cycle survives a real
    Temporal worker/sandbox round-trip.

    Substitutes a fake ``coding_team_run_pipeline`` activity, registered under
    the SAME name the real one uses (``@activity.defn(name=...)``), so
    ``CodingTeamWorkflow.run``'s ``workflow.execute_activity(run_pipeline_activity,
    ...)`` dispatches to it unchanged -- Temporal resolves by registered name,
    not Python object identity. The fake returns ``{"outcome": "paused", ...}``
    on its first call and a terminal dict once
    ``request["acknowledged_resume_token"]`` matches the pause it published,
    so this exercises exactly the loop production hits without the real,
    heavy orchestrator.

    Synchronization: ``submit_answers`` deliberately drops (never buffers) a
    signal arriving before the workflow has processed the paused activity
    result and set ``self._active_resume_token`` (see that method's
    docstring) -- a single, precisely-timed signal send would race that
    window, and there is no query handler to ask "are you paused yet?" (out
    of scope for #3988). Instead this resends the identical signal on a 50ms
    interval until the workflow's result future resolves or an overall
    timeout fires: every resend before the workflow reaches wait_condition is
    silently dropped by design (harmless, retried); the first to land after
    is accepted; every later one is a no-op (the already-submitted guard, or
    the server rejecting a signal to a since-completed workflow, caught and
    ignored); if nothing ever lands (a real regression), the timeout fails
    the test with a clear diagnostic instead of hanging. This requires no
    production change -- it works entirely within the already-merged,
    intentionally non-buffering ``submit_answers`` semantics.
    """
    resume_token = "coding-team-workflow-test:resume-token-1"

    from temporalio import activity
    from temporalio.worker import Replayer

    from software_engineering_team.temporal.coding_team_constants import TASK_QUEUE
    from software_engineering_team.temporal.coding_team_workflow import CodingTeamWorkflow

    @activity.defn(name="coding_team_run_pipeline")
    def _fake_pipeline_activity(request: dict) -> dict:
        if request.get("acknowledged_resume_token") == resume_token:
            return {"job_id": request.get("job_id", "test-job"), "status": "completed"}
        return {
            "outcome": "paused",
            "job_id": request.get("job_id", "test-job"),
            "resume_token": resume_token,
            "pause_kind": "entry",
            "pause_context": None,
            "pending_questions": [{"question_id": "q1", "prompt": "Proceed?"}],
        }

    async def _resend_signal_until_cancelled(
        handle, payload, *, poll_interval: float = 0.05
    ) -> None:
        while True:
            with contextlib.suppress(Exception):
                await handle.signal(CodingTeamWorkflow.submit_answers, payload)
            await asyncio.sleep(poll_interval)

    workflow_id = "coding-team-workflow-pause-resume-test"
    request = {
        "job_id": "test-job-1",
        "repo_path": "/tmp/repo",
        "plan_input": {"objective": "ship it"},
    }

    async with _workflow_environment_worker(activities=[_fake_pipeline_activity]) as env:
        handle = await env.client.start_workflow(
            CodingTeamWorkflow.run,
            request,
            id=workflow_id,
            task_queue=TASK_QUEUE,
        )

        resend_payload = {
            "resume_token": resume_token,
            "answers": [{"question_id": "q1", "answer": "yes"}],
        }
        resender = asyncio.create_task(_resend_signal_until_cancelled(handle, resend_payload))
        try:
            result = await asyncio.wait_for(handle.result(), timeout=30)
        finally:
            resender.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await resender

        history = await handle.fetch_history()

    assert result == {"job_id": "test-job-1", "status": "completed"}

    # Same determinism guard test_code_review_temporal.py's analogous test applies --
    # CodingTeamWorkflow is more determinism-sensitive than that simple one-shot
    # workflow (it mutates/reuses `request` across a signal-driven loop), so this
    # replay check is worth keeping.
    await Replayer(workflows=[CodingTeamWorkflow]).replay_workflow(history)
