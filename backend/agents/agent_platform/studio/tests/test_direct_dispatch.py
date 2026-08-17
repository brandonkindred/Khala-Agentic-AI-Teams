"""Unit tests for the Agent Studio in-process authoring dispatch path.

Temporal is never on the CRUD path: ``execute_workflow_sync`` must not run, and
native ``ValueError`` / ``LookupError`` propagate unchanged.
"""

from __future__ import annotations

import contextvars
import threading
import time
from unittest.mock import Mock

import pytest
from temporalio.exceptions import ApplicationError

import agent_platform.studio.temporal.dispatch as dispatch
from agent_platform.studio.models import AgentDefinition, ConversationStateResponse
from agent_platform.studio.registration import build_studio_agent_manifest
from agent_platform.studio.service import AgentStudioService


@pytest.fixture()
def service(monkeypatch: pytest.MonkeyPatch) -> Mock:
    """Mock installed at the lazy-import seam ``_direct_service()`` uses."""
    svc = Mock(spec=AgentStudioService)
    monkeypatch.setattr("agent_platform.studio.runtime.get_studio_service", lambda: svc)
    return svc


# ── start_conversation ──────────────────────────────────────────────────────────


def test_start_conversation_returns_service_response(service: Mock) -> None:
    resp = ConversationStateResponse(conversation_id="c1", mode="new", definition=AgentDefinition())
    service.start_conversation.return_value = resp

    out = dispatch.start_conversation("new", None, "hi")

    assert out is resp
    service.start_conversation.assert_called_once_with("new", None, "hi")


def test_start_conversation_reraises_value_error(service: Mock) -> None:
    service.start_conversation.side_effect = ValueError("bad mode")
    with pytest.raises(ValueError, match="bad mode") as ei:
        dispatch.start_conversation("refine", None, None)
    assert type(ei.value) is ValueError
    assert not isinstance(ei.value, ApplicationError)


def test_start_conversation_reraises_lookup_error(service: Mock) -> None:
    service.start_conversation.side_effect = LookupError("no such source")
    with pytest.raises(LookupError, match="no such source") as ei:
        dispatch.start_conversation("refine", "missing", None)
    assert type(ei.value) is LookupError
    assert not isinstance(ei.value, ApplicationError)


def test_start_conversation_propagates_unknown_error(service: Mock) -> None:
    service.start_conversation.side_effect = RuntimeError("boom")
    with pytest.raises(RuntimeError, match="boom"):
        dispatch.start_conversation("new", None, None)


# ── send_message ─────────────────────────────────────────────────────────────────


def test_send_message_returns_service_response(service: Mock) -> None:
    resp = ConversationStateResponse(conversation_id="c9", mode="new", definition=AgentDefinition())
    service.send_message.return_value = resp

    out = dispatch.send_message("c9", "make a planner")

    assert out is resp
    service.send_message.assert_called_once_with("c9", "make a planner")


def test_send_message_reraises_lookup_error(service: Mock) -> None:
    service.send_message.side_effect = LookupError("unknown conversation")
    with pytest.raises(LookupError, match="unknown conversation") as ei:
        dispatch.send_message("nope", "hi")
    assert type(ei.value) is LookupError
    assert not isinstance(ei.value, ApplicationError)


# ── clone_from_registry ──────────────────────────────────────────────────────────


def test_clone_from_registry_returns_service_definition(service: Mock) -> None:
    draft = AgentDefinition(
        name="Planner.copy", role="r", mode="refine", cloned_from="blogging.planner"
    )
    service.clone_from_registry.return_value = draft

    out = dispatch.clone_from_registry("blogging.planner")

    assert out is draft
    service.clone_from_registry.assert_called_once_with("blogging.planner")


def test_clone_from_registry_reraises_lookup_error(service: Mock) -> None:
    service.clone_from_registry.side_effect = LookupError("unknown agent")
    with pytest.raises(LookupError, match="unknown agent") as ei:
        dispatch.clone_from_registry("missing")
    assert type(ei.value) is LookupError
    assert not isinstance(ei.value, ApplicationError)


# ── save_agent ───────────────────────────────────────────────────────────────────


def test_save_agent_returns_manifest_tuple(service: Mock) -> None:
    definition = AgentDefinition(name="Saver", role="Saves things")
    manifest = build_studio_agent_manifest(definition)
    service.save_agent.return_value = (manifest, True)

    out_manifest, created = dispatch.save_agent(definition)

    assert out_manifest is manifest
    assert created is True
    service.save_agent.assert_called_once_with(definition)


def test_save_agent_reraises_value_error(service: Mock) -> None:
    service.save_agent.side_effect = ValueError("not ready")
    with pytest.raises(ValueError, match="not ready") as ei:
        dispatch.save_agent(AgentDefinition(name="X", role="Y"))
    assert type(ei.value) is ValueError
    assert not isinstance(ei.value, ApplicationError)


# ── subclass parity (direct path must not remap) ─────────────────────────────────


def test_start_conversation_preserves_valueerror_subclass(service: Mock) -> None:
    """A ``ValueError`` subclass stays that subclass — not an ``ApplicationError``."""

    class _CustomBadInput(ValueError):
        pass

    service.start_conversation.side_effect = _CustomBadInput("bad")
    with pytest.raises(_CustomBadInput, match="bad") as ei:
        dispatch.start_conversation("new", None, None)
    assert type(ei.value) is _CustomBadInput
    assert not isinstance(ei.value, ApplicationError)


def test_send_message_preserves_keyerror(service: Mock) -> None:
    """A ``KeyError`` stays a ``KeyError`` (still a ``LookupError``), not remapped."""
    service.send_message.side_effect = KeyError("missing")
    with pytest.raises(KeyError) as ei:
        dispatch.send_message("c", "hi")
    assert type(ei.value) is KeyError
    assert isinstance(ei.value, LookupError)
    assert not isinstance(ei.value, ApplicationError)


# ── dispatch timeout (replaces the former Temporal activity/execute caps) ────────


def test_authoring_timeout_matches_former_activity_cap() -> None:
    """In-process CRUD must keep the 180s cap the Temporal activity used."""
    assert dispatch.AUTHORING_TIMEOUT_S == 180.0


def test_send_message_timeout_raises_runtime_error(
    monkeypatch: pytest.MonkeyPatch, service: Mock
) -> None:
    """A stalled LLM turn must not occupy the caller until the provider timeout."""
    monkeypatch.setattr(dispatch, "AUTHORING_TIMEOUT_S", 0.05)
    service.send_message.side_effect = lambda *_a, **_k: time.sleep(1.0)

    with pytest.raises(RuntimeError, match="dispatch timeout"):
        dispatch.send_message("c9", "make a planner")


def test_start_conversation_timeout_raises_runtime_error(
    monkeypatch: pytest.MonkeyPatch, service: Mock
) -> None:
    monkeypatch.setattr(dispatch, "AUTHORING_TIMEOUT_S", 0.05)
    service.start_conversation.side_effect = lambda *_a, **_k: time.sleep(1.0)

    with pytest.raises(RuntimeError, match="dispatch timeout"):
        dispatch.start_conversation("new", None, "hi")


# ── _DaemonAuthoringPool (bounded threads + context propagation) ─────────────────


def test_pool_bounds_worker_threads_under_burst() -> None:
    """A burst of submits must not spawn a thread per call — concurrency stays capped."""
    pool = dispatch._DaemonAuthoringPool()
    try:
        idents: set[int] = set()
        lock = threading.Lock()

        def _record() -> None:
            with lock:
                idents.add(threading.get_ident())

        futures = [pool.submit(_record) for _ in range(50)]
        for fut in futures:
            fut.result(timeout=5)

        # Distinct worker thread ids can never exceed the fixed pool size, no
        # matter how many tasks were submitted (thread-per-task would give ~50).
        assert 0 < len(idents) <= dispatch._AUTHORING_POOL_WORKERS
    finally:
        pool.shutdown()


def test_pool_propagates_caller_contextvars() -> None:
    """A task runs inside the submitting thread's context snapshot (attribution/trace)."""
    var: contextvars.ContextVar[str] = contextvars.ContextVar("studio_test_var", default="unset")
    pool = dispatch._DaemonAuthoringPool()
    try:
        token = var.set("bound-in-caller")
        try:
            fut = pool.submit(var.get)
            assert fut.result(timeout=5) == "bound-in-caller"
        finally:
            var.reset(token)
    finally:
        pool.shutdown()


def test_pool_submit_after_shutdown_returns_runtime_error() -> None:
    pool = dispatch._DaemonAuthoringPool()
    pool.shutdown()
    fut = pool.submit(lambda: "unreachable")
    with pytest.raises(RuntimeError, match="shut down"):
        fut.result(timeout=5)


def test_pool_shutdown_is_idempotent() -> None:
    pool = dispatch._DaemonAuthoringPool()
    pool.shutdown()
    pool.shutdown()
    assert pool.is_live() is False


def test_shutdown_authoring_executor_is_idempotent() -> None:
    dispatch.shutdown_authoring_executor()
    dispatch.shutdown_authoring_executor()


def test_authoring_crud_works_after_executor_shutdown(service: Mock) -> None:
    resp = ConversationStateResponse(conversation_id="c1", mode="new", definition=AgentDefinition())
    service.start_conversation.return_value = resp
    dispatch.shutdown_authoring_executor()

    out = dispatch.start_conversation("new", None, None)

    assert out is resp


# ── studio_temporal_enabled ─────────────────────────────────────────────────────


def test_studio_temporal_enabled_delegates_to_shared_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("shared.temporal.client.is_temporal_enabled", lambda: True)
    assert dispatch.studio_temporal_enabled() is True


def test_studio_temporal_enabled_false_when_not_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shared.temporal.client.is_temporal_enabled", lambda: False)
    assert dispatch.studio_temporal_enabled() is False
