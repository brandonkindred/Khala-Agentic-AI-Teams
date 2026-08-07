"""Factory tests for get_draft_store()."""

from __future__ import annotations

import pytest

from agent_team_studio.agent_studio.drafts_store import AgentStudioDraftStore


def test_get_draft_store_returns_stable_singleton() -> None:
    from agent_team_studio.agent_studio.drafts_runtime import get_draft_store

    store = get_draft_store()
    assert get_draft_store() is store


def test_build_draft_store_in_memory_when_postgres_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import shared.postgres
    from agent_team_studio.agent_studio import drafts_runtime

    monkeypatch.setattr(shared.postgres, "is_postgres_enabled", lambda: False)
    store = drafts_runtime._build_draft_store()
    assert isinstance(store, AgentStudioDraftStore)


def test_build_draft_store_uses_postgres_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agent_team_studio.agent_studio.drafts_pg_store as pg
    import shared.postgres
    from agent_team_studio.agent_studio import drafts_runtime

    class _StubStore:
        """Stand-in so the selection does not construct a real Postgres store."""

    monkeypatch.setattr(shared.postgres, "is_postgres_enabled", lambda: True)
    monkeypatch.setattr(pg, "PostgresAgentStudioDraftStore", _StubStore)

    store = drafts_runtime._build_draft_store()
    assert isinstance(store, _StubStore)
