"""Tests for the persona CRUD + /testable-teams endpoints.

Drive the route handlers directly rather than through TestClient, in
the same style as ``test_jobs_endpoints.py`` — keeps Postgres, the
adapter registry, and the unified config out of test setup.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from agent_team_studio.user_agent_founder.store import StoredPersona


def _make_persona(persona_id: str = "p-1", *, is_builtin: bool = False) -> StoredPersona:
    return StoredPersona(
        persona_id=persona_id,
        name=f"name-{persona_id}",
        description="desc",
        icon="person",
        system_prompt="you are persona",
        spec_generation_prompt="write spec",
        is_builtin=is_builtin,
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
    )


@pytest.fixture
def fake_persona_store(monkeypatch):
    from agent_team_studio.user_agent_founder.api import main as api_main

    store = MagicMock()
    monkeypatch.setattr(api_main, "get_persona_store", lambda: store)
    return store


# ---------------------------------------------------------------------------
# GET /personas
# ---------------------------------------------------------------------------


def test_list_personas_returns_db_rows(fake_persona_store):
    from agent_team_studio.user_agent_founder.api.main import list_personas

    fake_persona_store.list_personas.return_value = [
        _make_persona("startup-founder", is_builtin=True),
        _make_persona("custom-1"),
    ]

    resp = list_personas()

    assert [p.id for p in resp.personas] == ["startup-founder", "custom-1"]
    assert resp.personas[0].is_builtin is True
    assert resp.personas[1].is_builtin is False


# ---------------------------------------------------------------------------
# POST /personas
# ---------------------------------------------------------------------------


def test_create_persona_returns_201_with_payload(fake_persona_store):
    from agent_team_studio.user_agent_founder.api.main import CreatePersonaRequest, create_persona

    fake_persona_store.create_persona.return_value = _make_persona("new-1")

    out = create_persona(
        CreatePersonaRequest(
            name="QA",
            description="aggressive QA",
            icon="bug_report",
            system_prompt="be picky",
            spec_generation_prompt="spec it",
        )
    )

    assert out.id == "new-1"
    fake_persona_store.create_persona.assert_called_once_with(
        name="QA",
        description="aggressive QA",
        icon="bug_report",
        system_prompt="be picky",
        spec_generation_prompt="spec it",
    )


# ---------------------------------------------------------------------------
# GET /personas/{id}
# ---------------------------------------------------------------------------


def test_get_persona_returns_persona(fake_persona_store):
    from agent_team_studio.user_agent_founder.api.main import get_persona

    fake_persona_store.get_persona.return_value = _make_persona("p-1")

    out = get_persona("p-1")
    assert out.id == "p-1"
    fake_persona_store.get_persona.assert_called_once_with("p-1")


def test_get_persona_404_when_missing(fake_persona_store):
    from agent_team_studio.user_agent_founder.api.main import get_persona

    fake_persona_store.get_persona.return_value = None
    with pytest.raises(HTTPException) as exc:
        get_persona("ghost")
    assert exc.value.status_code == 404


# ---------------------------------------------------------------------------
# PUT /personas/{id}  (the "edit" capability)
# ---------------------------------------------------------------------------


def test_update_persona_writes_partial_fields(fake_persona_store):
    from agent_team_studio.user_agent_founder.api.main import UpdatePersonaRequest, update_persona

    fake_persona_store.update_persona.return_value = _make_persona("p-1")

    out = update_persona("p-1", UpdatePersonaRequest(description="new desc"))

    assert out.id == "p-1"
    fake_persona_store.update_persona.assert_called_once_with("p-1", description="new desc")


def test_update_persona_404_when_missing(fake_persona_store):
    from agent_team_studio.user_agent_founder.api.main import UpdatePersonaRequest, update_persona

    fake_persona_store.update_persona.return_value = None
    with pytest.raises(HTTPException) as exc:
        update_persona("ghost", UpdatePersonaRequest(name="x"))
    assert exc.value.status_code == 404


def test_update_persona_succeeds_on_builtin(fake_persona_store):
    """Per user choice: builtins are editable like any other persona."""
    from agent_team_studio.user_agent_founder.api.main import UpdatePersonaRequest, update_persona

    fake_persona_store.update_persona.return_value = _make_persona(
        "startup-founder", is_builtin=True
    )

    out = update_persona("startup-founder", UpdatePersonaRequest(description="customized"))
    assert out.id == "startup-founder"
    assert out.is_builtin is True


def test_update_persona_with_no_fields_returns_existing_row(fake_persona_store):
    from agent_team_studio.user_agent_founder.api.main import UpdatePersonaRequest, update_persona

    fake_persona_store.get_persona.return_value = _make_persona("p-1")

    out = update_persona("p-1", UpdatePersonaRequest())

    assert out.id == "p-1"
    fake_persona_store.update_persona.assert_not_called()
    fake_persona_store.get_persona.assert_called_once_with("p-1")


def test_update_persona_with_no_fields_404_when_missing(fake_persona_store):
    from agent_team_studio.user_agent_founder.api.main import UpdatePersonaRequest, update_persona

    fake_persona_store.get_persona.return_value = None
    with pytest.raises(HTTPException) as exc:
        update_persona("ghost", UpdatePersonaRequest())
    assert exc.value.status_code == 404


# ---------------------------------------------------------------------------
# DELETE /personas/{id}
# ---------------------------------------------------------------------------


def test_delete_persona_204(fake_persona_store):
    from agent_team_studio.user_agent_founder.api.main import delete_persona

    fake_persona_store.delete_persona.return_value = True
    resp = delete_persona("p-1")
    assert resp.status_code == 204
    fake_persona_store.delete_persona.assert_called_once_with("p-1")


def test_delete_persona_404_when_missing(fake_persona_store):
    from agent_team_studio.user_agent_founder.api.main import delete_persona

    fake_persona_store.delete_persona.return_value = False
    with pytest.raises(HTTPException) as exc:
        delete_persona("ghost")
    assert exc.value.status_code == 404


def test_delete_persona_succeeds_on_builtin(fake_persona_store):
    """Per user choice: builtins are deletable; the seed will recreate on restart."""
    from agent_team_studio.user_agent_founder.api.main import delete_persona

    fake_persona_store.delete_persona.return_value = True
    resp = delete_persona("startup-founder")
    assert resp.status_code == 204


# ---------------------------------------------------------------------------
# GET /testable-teams
# ---------------------------------------------------------------------------


def test_testable_teams_lists_adapter_registry_keys():
    from agent_team_studio.user_agent_founder.api.main import list_testable_teams

    resp = list_testable_teams()
    keys = [t.team_key for t in resp.teams]
    assert "software_engineering" in keys
    # Display name is humanised (either via TEAM_CONFIGS or the title-case fallback).
    se = next(t for t in resp.teams if t.team_key == "software_engineering")
    assert se.display_name and se.display_name[0].isupper()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def test_slugify_persona_name_drops_non_alnum_and_truncates():
    from agent_team_studio.user_agent_founder.api.main import _slugify_persona_name

    assert _slugify_persona_name("Startup Founder") == "startup-founder"
    assert _slugify_persona_name("QA Bot 9000!!!") == "qa-bot-9000"
    assert _slugify_persona_name("") == "persona"
    assert _slugify_persona_name("A" * 200, max_len=8) == "a" * 8
