"""Unit tests for the Agent Studio direct (non-Temporal) authoring dispatch path.

Mirrors ``test_temporal_activity.py`` on the ``dispatch.*`` helpers: Temporal is forced
off, the process-wide ``AgentStudioService`` is a Mock, and ``execute_workflow_sync``
must never run. The direct path returns service objects unchanged and re-raises native
``ValueError`` / ``LookupError`` (never ``ApplicationError``).
"""

from __future__ import annotations

from unittest.mock import Mock

import pytest
from temporalio.exceptions import ApplicationError

import agent_platform.studio.temporal.dispatch as dispatch
from agent_platform.studio.models import AgentDefinition, ConversationStateResponse
from agent_platform.studio.registration import build_studio_agent_manifest
from agent_platform.studio.service import AgentStudioService


@pytest.fixture(autouse=True)
def _force_direct_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the in-process branch regardless of ``TEMPORAL_ADDRESS``."""
    monkeypatch.setattr(dispatch, "_temporal_enabled", lambda: False)


@pytest.fixture(autouse=True)
def _forbid_temporal_execute(monkeypatch: pytest.MonkeyPatch) -> None:
    """Direct path must never start a workflow."""

    def _boom(*_a, **_k):
        raise AssertionError("direct path must not call execute_workflow_sync")

    monkeypatch.setattr(dispatch, "execute_workflow_sync", _boom)


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
