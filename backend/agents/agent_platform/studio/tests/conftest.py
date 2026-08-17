"""Shared fixtures for the Agent Studio authoring test suite."""

from __future__ import annotations

from unittest.mock import Mock

import pytest

import agent_platform.studio.temporal.dispatch as dispatch
from agent_platform.studio.service import AgentStudioService


@pytest.fixture(autouse=True)
def _forbid_temporal_execute(monkeypatch: pytest.MonkeyPatch) -> None:
    """Authoring CRUD must never start a Temporal workflow, regardless of Temporal state.

    Shared across the dispatch test suite (``test_direct_dispatch.py``,
    ``test_temporal_enabled.py``, ``test_temporal_worker_absent.py``) so the
    guard is defined once rather than re-implemented per file.
    """

    def _boom(*_a, **_k):
        raise AssertionError("authoring CRUD must not call execute_workflow_sync")

    monkeypatch.setattr("shared.temporal.execute_workflow_sync", _boom, raising=False)
    if hasattr(dispatch, "execute_workflow_sync"):
        monkeypatch.setattr(dispatch, "execute_workflow_sync", _boom)


@pytest.fixture()
def service(monkeypatch: pytest.MonkeyPatch) -> Mock:
    """A mocked ``AgentStudioService`` installed at the runtime seam.

    Postconditions:
        * ``agent_platform.studio.runtime.get_studio_service`` returns this
          mock for the duration of the requesting test.

    Shared across the dispatch test suite so the seam-mock is defined once
    rather than re-implemented per file.
    """
    svc = Mock(spec=AgentStudioService)
    monkeypatch.setattr("agent_platform.studio.runtime.get_studio_service", lambda: svc)
    return svc
