"""Tests for the process-wide Agent Studio service singleton."""

from __future__ import annotations

import pytest

from agent_platform.studio.service import AgentStudioService
from agent_platform.studio.store import AgentStudioConversationStore


def test_get_studio_service_returns_stable_singleton() -> None:
    from agent_platform.studio.runtime import get_studio_service

    svc = get_studio_service()
    assert isinstance(svc, AgentStudioService)
    assert get_studio_service() is svc


def test_build_service_in_memory_when_postgres_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    import shared.postgres
    from agent_platform.studio import runtime

    monkeypatch.setattr(shared.postgres, "is_postgres_enabled", lambda: False)
    svc = runtime._build_service()
    assert isinstance(svc, AgentStudioService)
    assert isinstance(svc._store, AgentStudioConversationStore)


def test_build_service_uses_postgres_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    import agent_platform.studio.pg_store as pg
    import shared.postgres
    from agent_platform.studio import runtime

    class _StubStore:
        """Stand-in so the selection does not construct a real Postgres store."""

    monkeypatch.setattr(shared.postgres, "is_postgres_enabled", lambda: True)
    monkeypatch.setattr(pg, "PostgresAgentStudioConversationStore", _StubStore)

    svc = runtime._build_service()
    assert isinstance(svc._store, _StubStore)
