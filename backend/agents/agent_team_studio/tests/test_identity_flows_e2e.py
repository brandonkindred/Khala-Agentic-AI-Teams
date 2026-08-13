"""End-to-end identity-epic regression coverage (issue #5904).

Every individual flow — clone, save, roster add-from-registry, mixed roster —
already has deep isolated coverage elsewhere (``agent_studio/tests/test_registration.py``,
``test_service.py``, ``agentic_team_provisioning/tests/test_registry_roster.py``,
``test_mixed_roster.py``). What's missing is a test that chains them: proving a
manifest produced by Studio's *real* clone->edit->save pipeline is the exact thing
the roster's add-from-registry path resolves inside a mixed roster, and that a
later re-save propagates through the roster's join-at-read model. This file adds
only that chain — it does not duplicate the per-flow unit coverage above.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agent_platform.registry.models import AgentManifest
from agent_team_studio.agent_studio.models import AgentDefinition
from agent_team_studio.agent_studio.service import AgentStudioService
from agent_team_studio.agent_studio.testing import seed_manifest
from agent_team_studio.agentic_team_provisioning.assistant.store import AgenticTeamStore
from agent_team_studio.agentic_team_provisioning.manifest_generation import build_agent_manifest
from agent_team_studio.agentic_team_provisioning.models import AgenticTeamAgent
from agent_team_studio.agentic_team_provisioning.tests._fake_postgres import install_fake_postgres

_SEED_MANIFEST = seed_manifest(
    agent_id="blogging.planner",
    team="blogging",
    name="Planner",
    summary="Plans blog outlines",
    tags=["content"],
    tools=["web.search"],
)


class _FakeRegistry:
    """The slice of ``AgentRegistry`` the Studio clone/save and roster/registry
    seams under test use, shared across ``AgentStudioService`` and the
    ``agentic_team_provisioning`` FastAPI app (see the ``registry`` fixture) so a
    manifest saved via Studio is immediately visible to the roster's
    from-registry path. Mirrors the richer double already used in
    ``agentic_team_provisioning/tests/test_registry_roster.py`` and
    ``test_mixed_roster.py`` (a file-local copy, matching that established
    per-file convention, not a shared/importable fixture).
    """

    def __init__(self, manifests: list[AgentManifest] | None = None) -> None:
        self._by_id = {m.id: m for m in (manifests or [])}

    def get(self, agent_id: str, *, conn=None) -> AgentManifest | None:
        del conn
        return self._by_id.get(agent_id)

    def manifests_with_id_prefix(
        self, prefix: str, *, require_store: bool = False
    ) -> list[AgentManifest]:
        del require_store
        return [m for m in self._by_id.values() if m.id.startswith(prefix)]

    def register(
        self,
        manifest: AgentManifest,
        source_path=None,
        *,
        require_persist: bool = False,
        conn=None,
    ) -> None:
        del source_path, require_persist, conn
        self._by_id[manifest.id] = manifest

    def unregister(self, agent_id: str) -> bool:
        return self._by_id.pop(agent_id, None) is not None

    def replace_dynamic_manifests(self, upserts, delete_ids, *, conn=None) -> None:
        del conn
        for agent_id in delete_ids:
            self._by_id.pop(agent_id, None)
        for manifest in upserts:
            self._by_id[manifest.id] = manifest


@pytest.fixture
def registry() -> _FakeRegistry:
    return _FakeRegistry([_SEED_MANIFEST])


@pytest.fixture
def studio_service(registry: _FakeRegistry) -> AgentStudioService:
    return AgentStudioService(registry_getter=lambda: registry)


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, registry: _FakeRegistry) -> TestClient:
    install_fake_postgres(monkeypatch)
    monkeypatch.setattr("agent_platform.registry.get_registry", lambda: registry)
    from agent_team_studio.agentic_team_provisioning.api.main import app

    return TestClient(app)


def _new_team() -> str:
    return AgenticTeamStore().create_team(name="Growth Pod", description="").team_id


def _clone_edit_and_save(
    studio_service: AgentStudioService,
) -> tuple[AgentManifest, AgentDefinition]:
    """Clone the seed manifest, edit it, and save — the shared setup for tests 1-3."""
    draft = studio_service.clone_from_registry(_SEED_MANIFEST.id)
    edited = draft.model_copy(
        update={
            "role": "Plans SEO-aware blog outlines",
            "tags": [*draft.tags, "seo"],
            "tools": [*draft.tools, "draft"],
            "system_prompt": "You are a meticulous, SEO-aware blog planner.",
        }
    )
    manifest, created = studio_service.save_agent(edited)
    assert created is True
    return manifest, edited


def test_clone_edit_save_add_to_registry_roster_end_to_end(
    studio_service: AgentStudioService, client: TestClient
) -> None:
    """Regression for #5904: a manifest produced by Studio's clone->edit->save
    pipeline (not a hand-built test manifest) is exactly what the roster's
    add-from-registry path resolves, and the roster stores only a thin ref."""
    manifest, edited = _clone_edit_and_save(studio_service)

    # The save pipeline's own contract (registration.py:_manifest_states):
    # the edited system_prompt lands on the "executing" state.
    executing_prompt = next(s.system_prompt for s in manifest.states if s.key == "executing")
    assert executing_prompt == edited.system_prompt
    assert manifest.tags == sorted({"studio", *edited.tags})

    team_id = _new_team()
    resp = client.post(f"/teams/{team_id}/agents/from-registry", json={"manifest_id": manifest.id})
    assert resp.status_code == 201
    row = resp.json()
    assert row["source"] == "registry"
    assert row["role"] == edited.role
    assert set(edited.tools) <= set(row["tools"])
    assert "seo" in row["skills"]

    # No second identity survives the round trip: the persisted roster row is a
    # thin ref only (epic AC: "no duplicated persona/skills/prompt fields as a
    # second SoT"), matching the assertion style in test_registry_roster.py.
    stored = AgenticTeamStore().list_team_agents(team_id)[0].model_dump(mode="json")
    assert set(stored.keys()) == {"agent_name", "source", "manifest_id"}
    assert stored["manifest_id"] == manifest.id


def test_clone_edit_save_add_to_mixed_roster_end_to_end(
    studio_service: AgentStudioService, client: TestClient, registry: _FakeRegistry
) -> None:
    """Regression for #5904: a Studio-produced (clone->edit->save) agent composes
    correctly with a purely generated agent in the same roster — the pre-existing
    mixed-roster guarantee (test_mixed_roster.py) now proven against a manifest
    that came from the real Studio pipeline, not a hand-built one."""
    manifest, edited = _clone_edit_and_save(studio_service)
    team_id = _new_team()
    client.post(f"/teams/{team_id}/agents/from-registry", json={"manifest_id": manifest.id})

    writer_manifest = build_agent_manifest(
        team_id, "Writer", summary="Writes copy", skill_tags=["seo"]
    )
    registry.register(writer_manifest)
    AgenticTeamStore().add_or_replace_team_agent(
        team_id,
        AgenticTeamAgent(agent_name="Writer", source="generated", manifest_id=writer_manifest.id),
    )

    roster = {a["agent_name"]: a for a in client.get(f"/teams/{team_id}/agents").json()}
    assert set(roster) == {manifest.name, "Writer"}

    studio_row = roster[manifest.name]
    assert studio_row["source"] == "registry"
    assert studio_row["role"] == edited.role

    generated_row = roster["Writer"]
    assert generated_row["source"] == "generated"
    assert generated_row["role"] == "Writes copy"
    assert "seo" in generated_row["skills"]

    validation = client.get(f"/teams/{team_id}/roster/validation").json()
    assert validation["is_fully_staffed"] is True
    assert validation["gaps"] == []


def test_resave_same_name_updates_roster_linked_manifest_in_place(
    studio_service: AgentStudioService, client: TestClient
) -> None:
    """Regression for #5904: the roster is join-at-read, not a snapshot — editing
    and re-saving a Studio agent already on a roster propagates automatically on
    the next read, with no roster-side write required."""
    manifest, edited = _clone_edit_and_save(studio_service)
    team_id = _new_team()
    client.post(f"/teams/{team_id}/agents/from-registry", json={"manifest_id": manifest.id})

    resaved = edited.model_copy(
        update={
            "role": "Plans blog outlines with a sustainability focus",
            "system_prompt": "You now specialize in sustainability-focused content.",
        }
    )
    updated_manifest, created = studio_service.save_agent(resaved)
    assert created is False
    assert updated_manifest.id == manifest.id

    # No roster mutation happened — the enriched read still reflects the new save.
    roster = {a["agent_name"]: a for a in client.get(f"/teams/{team_id}/agents").json()}
    assert roster[manifest.name]["role"] == resaved.role
