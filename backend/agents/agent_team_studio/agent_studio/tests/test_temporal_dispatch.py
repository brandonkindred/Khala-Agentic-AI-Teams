"""Unit tests for the Agent Studio execute-and-wait dispatch layer.

Covers two things without a Temporal cluster (``execute_workflow_sync`` is patched):

  * each dispatch helper starts the right workflow with the right id/queue/args and
    rebuilds the Pydantic response from the workflow result, and
  * ``_translate_workflow_failure`` maps a ``WorkflowFailureError`` whose cause chain
    carries an ``ApplicationError`` ``type`` marker back to the native
    ``ValueError``/``LookupError`` (and re-raises anything else unchanged).
"""

from __future__ import annotations

from typing import Any

import pytest
from temporalio.client import WorkflowFailureError
from temporalio.exceptions import ActivityError, ApplicationError, WorkflowAlreadyStartedError

import agent_team_studio.agent_studio.temporal.dispatch as dispatch
from agent_team_studio.agent_studio.models import AgentDefinition, ConversationStateResponse
from agent_team_studio.agent_studio.registration import build_studio_agent_manifest
from agent_team_studio.agent_studio.temporal import (
    TASK_QUEUE,
    WORKFLOW_ID_PREFIX_CLONE,
    WORKFLOW_ID_PREFIX_MSG,
    WORKFLOW_ID_PREFIX_SAVE,
    WORKFLOW_ID_PREFIX_START,
    CloneFromRegistryWorkflow,
    SaveAgentWorkflow,
    SendMessageWorkflow,
    StartConversationWorkflow,
)


def _capture(monkeypatch: pytest.MonkeyPatch, result: Any) -> dict:
    """Patch ``execute_workflow_sync`` to record its call and return ``result``."""
    cap: dict = {}

    def _fake(workflow_run, *args, workflow_id, task_queue, **_kw):
        cap.update(
            workflow_run=workflow_run, args=args, workflow_id=workflow_id, task_queue=task_queue
        )
        return result

    monkeypatch.setattr(dispatch, "execute_workflow_sync", _fake)
    return cap


def _raise(monkeypatch: pytest.MonkeyPatch, exc: BaseException) -> None:
    def _fake(*_a, **_kw):
        raise exc

    monkeypatch.setattr(dispatch, "execute_workflow_sync", _fake)


# ── happy paths: correct dispatch + reconstruction ───────────────────────────────


def test_start_conversation_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    resp = ConversationStateResponse(conversation_id="c1", mode="new", definition=AgentDefinition())
    cap = _capture(monkeypatch, resp.model_dump())

    out = dispatch.start_conversation("new", None, "hi")

    assert out == resp
    assert cap["workflow_run"] is StartConversationWorkflow.run
    assert cap["args"] == ("new", None, "hi")
    assert cap["task_queue"] == TASK_QUEUE
    assert cap["workflow_id"].startswith(WORKFLOW_ID_PREFIX_START)


def test_send_message_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    resp = ConversationStateResponse(conversation_id="c2", mode="new", definition=AgentDefinition())
    cap = _capture(monkeypatch, resp.model_dump())

    out = dispatch.send_message("c2", "hello")

    assert out == resp
    assert cap["workflow_run"] is SendMessageWorkflow.run
    assert cap["args"] == ("c2", "hello")
    assert cap["workflow_id"].startswith(f"{WORKFLOW_ID_PREFIX_MSG}c2-")


def test_clone_from_registry_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    draft = AgentDefinition(
        name="Planner.copy", role="r", mode="refine", cloned_from="blogging.planner"
    )
    cap = _capture(monkeypatch, draft.model_dump())

    out = dispatch.clone_from_registry("blogging.planner")

    assert out == draft
    assert cap["workflow_run"] is CloneFromRegistryWorkflow.run
    assert cap["args"] == ("blogging.planner",)
    assert cap["workflow_id"].startswith(f"{WORKFLOW_ID_PREFIX_CLONE}blogging.planner-")


def test_save_agent_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    definition = AgentDefinition(name="Saver", role="Saves things")
    manifest = build_studio_agent_manifest(definition)
    cap = _capture(monkeypatch, {"manifest": manifest.model_dump(), "created": True})

    out_manifest, created = dispatch.save_agent(definition)

    assert out_manifest == manifest
    assert created is True
    assert cap["workflow_run"] is SaveAgentWorkflow.run
    assert cap["args"] == (definition.model_dump(),)
    assert cap["workflow_id"].startswith(WORKFLOW_ID_PREFIX_SAVE)


# ── error translation ────────────────────────────────────────────────────────────


def test_dispatch_translates_value_error_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    _raise(monkeypatch, WorkflowFailureError(cause=ApplicationError("bad", type="ValueError")))
    with pytest.raises(ValueError, match="bad"):
        dispatch.start_conversation("refine", None, None)


def test_dispatch_translates_lookup_error_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    _raise(monkeypatch, WorkflowFailureError(cause=ApplicationError("missing", type="LookupError")))
    with pytest.raises(LookupError, match="missing"):
        dispatch.clone_from_registry("missing")


def test_dispatch_reraises_unmarked_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    _raise(monkeypatch, WorkflowFailureError(cause=ApplicationError("weird", type="RuntimeError")))
    with pytest.raises(WorkflowFailureError):
        dispatch.send_message("c", "hi")


def test_dispatch_surfaces_duplicate_workflow_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """A duplicate live workflow id (``WorkflowAlreadyStartedError``) surfaces as a clear
    ``RuntimeError`` naming the id, not the raw Temporal error the route would not map."""
    _raise(monkeypatch, WorkflowAlreadyStartedError("wid", "AgentStudioStartConversationWorkflow"))
    with pytest.raises(RuntimeError, match="duplicate live workflow id"):
        dispatch.start_conversation("new", None, None)


def test_dispatch_surfaces_workflow_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """A workflow timeout (``concurrent.futures.TimeoutError``) surfaces as a clear
    ``RuntimeError`` rather than an opaque unhandled error."""
    import concurrent.futures

    _raise(monkeypatch, concurrent.futures.TimeoutError())
    with pytest.raises(RuntimeError, match="did not complete within"):
        dispatch.start_conversation("new", None, None)


# ── _translate_workflow_failure directly ─────────────────────────────────────────


def test_translate_finds_nested_marker() -> None:
    """A marker nested one level deeper (under an ActivityError-like wrapper) is found."""
    inner = ApplicationError("nested bad", type="ValueError")
    outer = RuntimeError("activity failed")
    outer.__cause__ = inner
    wf_fail = WorkflowFailureError(cause=outer)
    with pytest.raises(ValueError, match="nested bad"):
        dispatch._translate_workflow_failure(wf_fail)


def test_translate_finds_real_activity_error_chain() -> None:
    """The real production chain — ``WorkflowFailureError`` → ``ActivityError`` →
    ``ApplicationError`` (two levels, which Temporal actually builds) — translates back
    to the native exception, not just the hand-crafted one-level chain above."""
    app = ApplicationError("boom", type="ValueError")
    act = ActivityError(
        message="activity failed",
        scheduled_event_id=1,
        started_event_id=2,
        identity="x",
        activity_type="agent_studio_start_conversation",
        activity_id="1",
        retry_state=None,
    )
    act.__cause__ = app  # Temporal exposes the wrapped failure as .cause / .__cause__
    wf_fail = WorkflowFailureError(cause=act)
    with pytest.raises(ValueError, match="boom"):
        dispatch._translate_workflow_failure(wf_fail)


def test_translate_finds_top_level_marker() -> None:
    """The common single-activity path: the marker ``ApplicationError`` is the
    ``WorkflowFailureError``'s immediate cause (one step from the top)."""
    wf_fail = WorkflowFailureError(cause=ApplicationError("bad", type="ValueError"))
    with pytest.raises(ValueError, match="bad"):
        dispatch._translate_workflow_failure(wf_fail)


def test_translate_returns_none_without_marker() -> None:
    """No contract marker → returns without raising (caller re-raises the original)."""
    wf_fail = WorkflowFailureError(cause=ApplicationError("infra", type="Boom"))
    assert dispatch._translate_workflow_failure(wf_fail) is None


def test_translate_is_cycle_safe() -> None:
    """A cyclic cause chain terminates instead of looping forever."""
    a = RuntimeError("a")
    b = RuntimeError("b")
    a.__cause__ = b
    b.__cause__ = a
    wf_fail = WorkflowFailureError(cause=a)
    assert dispatch._translate_workflow_failure(wf_fail) is None


def test_translate_and_dispatch_handle_none_cause(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ``WorkflowFailureError`` with no cause chain (``cause=None``) returns ``None``
    from the walk (no raise) and is re-raised as-is by the dispatch layer."""
    assert dispatch._translate_workflow_failure(WorkflowFailureError(cause=None)) is None

    _raise(monkeypatch, WorkflowFailureError(cause=None))
    with pytest.raises(WorkflowFailureError):
        dispatch.send_message("c", "hi")
