"""Tests for rosters that mix ``source="registry"`` and ``source="generated"``
agents in the same team.

Registry-only and generated-only rosters are covered thoroughly elsewhere
(``test_registry_roster.py``, ``test_team_agents.py``, ``test_roster_validation.py``).
This file pins the cross-cutting invariant that a mixed roster keeps working
correctly through the store round-trip, the registry-preserving merge, API
read-enrichment, and roster validation — and that the ``AgentManifest`` ->
roster-row projection (``_roster_agent_from_manifest``) composes cleanly with
a generated entry in the same roster.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agent_registry.models import AgentManifest, CognitionSpec, SourceInfo
from agent_team_studio.agentic_team_provisioning.assistant.store import AgenticTeamStore
from agent_team_studio.agentic_team_provisioning.manifest_generation import (
    build_agent_manifest,
    manifest_agent_id,
)
from agent_team_studio.agentic_team_provisioning.models import (
    AgenticTeam,
    AgenticTeamAgent,
    ProcessDefinition,
    ProcessOutput,
    ProcessStatus,
    ProcessStep,
    ProcessStepAgent,
    ProcessTrigger,
    TriggerType,
)
from agent_team_studio.agentic_team_provisioning.roster_validation import validate_roster
from agent_team_studio.agentic_team_provisioning.tests._fake_postgres import install_fake_postgres

_SOURCE = SourceInfo(entrypoint="pkg.mod:Agent")

_PLANNER = AgentManifest(
    id="blogging.planner",
    team="blogging",
    name="blogging.planner",
    summary="Plans SEO-aware blog outlines",
    tags=["studio", "seo"],
    cognition=CognitionSpec(tools=["web.search", "draft"]),
    source=_SOURCE,
)
_EDITOR = AgentManifest(
    id="blogging.editor",
    team="blogging",
    name="blogging.editor",
    summary="Edits blog drafts for tone",
    tags=["studio", "editing"],
    cognition=CognitionSpec(tools=["style.check"]),
    source=_SOURCE,
)


class _FakeRegistry:
    """The slice of ``AgentRegistry`` the roster/registry seams under test use."""

    def __init__(self, manifests: list[AgentManifest]) -> None:
        self._by_id = {m.id: m for m in manifests}

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
    return _FakeRegistry([_PLANNER, _EDITOR])


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, registry: _FakeRegistry) -> TestClient:
    install_fake_postgres(monkeypatch)
    monkeypatch.setattr("agent_registry.get_registry", lambda: registry)
    from agent_team_studio.agentic_team_provisioning.api.main import app

    return TestClient(app)


def _new_team() -> str:
    return AgenticTeamStore().create_team(name="Growth Pod", description="").team_id


def _thin_gen(team_id: str, agent_name: str) -> AgenticTeamAgent:
    return AgenticTeamAgent(
        agent_name=agent_name,
        source="generated",
        manifest_id=manifest_agent_id(team_id, agent_name),
    )


def _registry_ref(manifest: AgentManifest) -> AgenticTeamAgent:
    """Project a manifest into a thin roster row the same way
    ``_roster_agent_from_manifest`` does (id/name only — persona resolves on read)."""
    return AgenticTeamAgent(agent_name=manifest.name, source="registry", manifest_id=manifest.id)


def test_save_team_agents_round_trips_mixed_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    """A full-roster save with both sources persists and reloads every row intact,
    ordered by ``agent_name`` (``list_team_agents``'s documented postcondition)."""
    install_fake_postgres(monkeypatch)
    store = AgenticTeamStore()
    team_id = store.create_team(name="Pod", description="").team_id

    registry_agents = [_registry_ref(_PLANNER), _registry_ref(_EDITOR)]
    generated_agents = [_thin_gen(team_id, "Writer"), _thin_gen(team_id, "Scout")]
    store.save_team_agents(team_id, registry_agents + generated_agents)

    loaded = store.list_team_agents(team_id)
    assert [a.agent_name for a in loaded] == sorted(
        a.agent_name for a in registry_agents + generated_agents
    )
    by_name = {a.agent_name: a for a in loaded}
    assert by_name["blogging.planner"].source == "registry"
    assert by_name["blogging.planner"].manifest_id == "blogging.planner"
    assert by_name["blogging.editor"].source == "registry"
    assert by_name["Writer"].source == "generated"
    assert by_name["Writer"].manifest_id == manifest_agent_id(team_id, "Writer")
    assert by_name["Scout"].source == "generated"


def test_merge_generated_agents_preserves_multiple_registry_agents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``merge_generated_agents`` keeps every pre-existing registry row (not just a
    single one), layers on non-colliding generated agents, and drops a generated
    agent that collides by name with a preserved registry agent (registry wins)."""
    install_fake_postgres(monkeypatch)
    store = AgenticTeamStore()
    team_id = store.create_team(name="Pod", description="").team_id

    store.add_or_replace_team_agent(team_id, _registry_ref(_PLANNER))
    store.add_or_replace_team_agent(team_id, _registry_ref(_EDITOR))

    colliding = AgenticTeamAgent(
        agent_name="blogging.editor",  # collides with a preserved registry agent
        source="generated",
        manifest_id=manifest_agent_id(team_id, "blogging.editor"),
    )
    merged = store.merge_generated_agents(
        team_id,
        [_thin_gen(team_id, "Writer"), _thin_gen(team_id, "Scout"), colliding],
    )

    by_name = {a.agent_name: a for a in merged}
    assert set(by_name) == {"blogging.planner", "blogging.editor", "Writer", "Scout"}
    assert by_name["blogging.planner"].source == "registry"
    assert by_name["blogging.editor"].source == "registry"  # collision dropped generated dup
    assert by_name["blogging.editor"].manifest_id == "blogging.editor"  # untouched, not overwritten
    assert by_name["Writer"].source == "generated"
    assert by_name["Scout"].source == "generated"

    # Persisted, not just returned.
    persisted = {a.agent_name: a.source for a in store.list_team_agents(team_id)}
    assert persisted == {
        "blogging.planner": "registry",
        "blogging.editor": "registry",
        "Writer": "generated",
        "Scout": "generated",
    }


def test_get_team_agents_enriches_mixed_roster(client: TestClient) -> None:
    """A roster with one registry-projected agent and one generated agent enriches
    both correctly on read: each row's persona resolves from its own linked
    manifest, proving ``enrich_roster_agent`` handles a mixed list, not just
    same-source lists."""
    team_id = _new_team()

    # Registry side: exercises the AgentManifest -> roster-row projection
    # (``_roster_agent_from_manifest``) via the from-registry endpoint.
    resp = client.post(
        f"/teams/{team_id}/agents/from-registry", json={"manifest_id": "blogging.planner"}
    )
    assert resp.status_code == 201

    # Generated side: its own manifest is registered and the thin ref added directly,
    # mirroring how the LLM-save path stamps and registers a generated agent's manifest.
    from agent_registry import get_registry

    writer_manifest = build_agent_manifest(
        team_id, "Writer", summary="Writes copy", skill_tags=["seo"]
    )
    get_registry().register(writer_manifest)
    AgenticTeamStore().add_or_replace_team_agent(team_id, _thin_gen(team_id, "Writer"))

    roster = {a["agent_name"]: a for a in client.get(f"/teams/{team_id}/agents").json()}
    assert set(roster) == {"blogging.planner", "Writer"}

    registry_row = roster["blogging.planner"]
    assert registry_row["source"] == "registry"
    assert registry_row["role"] == "Plans SEO-aware blog outlines"
    assert registry_row["skills"] == ["studio", "seo"]
    assert registry_row["tools"] == ["web.search", "draft"]

    generated_row = roster["Writer"]
    assert generated_row["source"] == "generated"
    assert generated_row["role"] == "Writes copy"
    assert "seo" in generated_row["skills"]


def test_validate_roster_mixed_sources_fully_staffed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Roster validation is source-agnostic: a roster with one registry agent and
    one generated agent, each backed by a full manifest and used by a process
    step, reports fully staffed with no gaps."""
    team_id = "mixed-team"
    reg = _FakeRegistry([_PLANNER])
    generated_manifest = build_agent_manifest(
        team_id, "Writer", summary="Writes copy", skill_tags=["seo"]
    )
    reg.register(generated_manifest)
    monkeypatch.setattr("agent_registry.get_registry", lambda: reg)

    registry_agent = AgenticTeamAgent(
        agent_name="blogging.planner", source="registry", manifest_id="blogging.planner"
    )
    generated_agent = AgenticTeamAgent(
        agent_name="Writer", source="generated", manifest_id=generated_manifest.id
    )
    team = AgenticTeam(
        team_id=team_id,
        name="Mixed",
        agents=[registry_agent, generated_agent],
        processes=[
            ProcessDefinition(
                process_id="p1",
                name="P1",
                trigger=ProcessTrigger(trigger_type=TriggerType.MESSAGE, description="go"),
                steps=[
                    ProcessStep(
                        step_id="s1",
                        name="Step 1",
                        agents=[
                            ProcessStepAgent(agent_name="blogging.planner", role="plans"),
                            ProcessStepAgent(agent_name="Writer", role="writes"),
                        ],
                    )
                ],
                output=ProcessOutput(description="done", destination="out"),
                status=ProcessStatus.DRAFT,
            )
        ],
    )

    result = validate_roster(team)
    assert result.is_fully_staffed is True
    assert result.gaps == []
    assert result.agent_count == 2
