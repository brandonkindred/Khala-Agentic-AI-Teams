"""Tests for the §5.3 registry→roster bridge.

Covers ``POST /teams/{id}/agents/from-registry`` (project a registered
``AgentManifest`` into the roster) and ``DELETE /teams/{id}/agents/{name}``,
plus the projection's effect on roster validation. The agent registry is faked
so no manifests are loaded from disk.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agent_registry.models import AgentManifest, CognitionSpec, SourceInfo
from agent_team_studio.agentic_team_provisioning.assistant.store import AgenticTeamStore
from agent_team_studio.agentic_team_provisioning.models import AgenticTeamAgent
from agent_team_studio.agentic_team_provisioning.tests._fake_postgres import install_fake_postgres

_SOURCE = SourceInfo(entrypoint="pkg.mod:Agent")

# A fully-specified manifest (tags + cognition tools) and a bare one (no summary,
# no cognition) to exercise both projection branches.
_PLANNER = AgentManifest(
    id="blogging.planner",
    team="blogging",
    name="blogging.planner",
    summary="Plans SEO-aware blog outlines",
    tags=["studio", "seo"],
    cognition=CognitionSpec(tools=["web.search", "draft"]),
    source=_SOURCE,
)
_BARE = AgentManifest(
    id="misc.bare",
    team="misc",
    name="misc.bare",
    summary="",
    tags=["studio"],
    cognition=None,
    source=_SOURCE,
)


class _FakeRegistry:
    """The slice of ``AgentRegistry`` the from-registry / delete routes use."""

    def __init__(self, manifests: list[AgentManifest]) -> None:
        self._by_id = {m.id: m for m in manifests}

    def get(self, agent_id: str) -> AgentManifest | None:
        return self._by_id.get(agent_id)

    def manifests_with_id_prefix(
        self, prefix: str, *, require_store: bool = False
    ) -> list[AgentManifest]:
        del require_store  # fake has no dynamic store
        # Mirror AgentRegistry.manifests_with_id_prefix so register_team_manifests'
        # stale-cleanup scan runs against the fake (without this method the call
        # would raise AttributeError and fail the register/unregister path).
        return [m for m in self._by_id.values() if m.id.startswith(prefix)]

    def register(
        self, manifest: AgentManifest, source_path=None, *, require_persist: bool = False
    ) -> None:
        del source_path, require_persist  # fake has no dynamic store to persist
        self._by_id[manifest.id] = manifest

    def unregister(self, agent_id: str) -> bool:
        return self._by_id.pop(agent_id, None) is not None

    def replace_dynamic_manifests(self, upserts, delete_ids, *, conn=None) -> None:
        del conn  # fake has no dynamic store
        for agent_id in delete_ids:
            self._by_id.pop(agent_id, None)
        for manifest in upserts:
            self._by_id[manifest.id] = manifest


@pytest.fixture
def registry() -> _FakeRegistry:
    """One fake registry instance shared by the route and the test (so register/
    unregister side effects are observable)."""
    return _FakeRegistry([_PLANNER, _BARE])


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, registry: _FakeRegistry) -> TestClient:
    install_fake_postgres(monkeypatch)
    # The route resolves the registry via ``from agent_registry import get_registry``;
    # patch the package attribute so the call picks up the shared fake.
    monkeypatch.setattr("agent_registry.get_registry", lambda: registry)
    from agent_team_studio.agentic_team_provisioning.api.main import app

    return TestClient(app)


def _new_team() -> str:
    return AgenticTeamStore().create_team(name="Growth Pod", description="").team_id


def test_from_registry_projects_and_persists(client: TestClient) -> None:
    """201 with a thin roster ref — no manifest fields copied onto the row.

    Persona resolution from the manifest (role/skills/tools/expertise) happens
    live via ``manifest_generation.resolve_roster_persona`` for consumers that
    need it (validation, prompt construction) — see ``test_manifest_generation.py``
    — not at write time.
    """
    team_id = _new_team()

    resp = client.post(
        f"/teams/{team_id}/agents/from-registry", json={"manifest_id": "blogging.planner"}
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["agent_name"] == "blogging.planner"
    assert body["role"] == ""
    assert body["skills"] == []
    assert body["tools"] == []
    assert body["expertise"] == []
    assert body["source"] == "registry"
    assert body["manifest_id"] == "blogging.planner"

    # Persisted on the roster, still thin.
    roster = client.get(f"/teams/{team_id}/agents").json()
    assert [a["agent_name"] for a in roster] == ["blogging.planner"]
    assert roster[0]["source"] == "registry"
    assert roster[0]["role"] == ""


def test_from_registry_bare_manifest_is_thin(client: TestClient) -> None:
    """A manifest with no summary/cognition still projects to a thin ref."""
    team_id = _new_team()
    resp = client.post(f"/teams/{team_id}/agents/from-registry", json={"manifest_id": "misc.bare"})
    assert resp.status_code == 201
    body = resp.json()
    assert body["role"] == ""
    assert body["tools"] == []
    assert body["skills"] == []
    assert body["expertise"] == []


def test_from_registry_no_cognition_tools_passes_validation(client: TestClient) -> None:
    """Regression: a tagged manifest with NO cognition tools (the common catalog
    shape) must still pass roster validation — skills (tags) + expertise (team)
    give two populated fields, so it isn't flagged ``sparse_profile``."""
    team_id = _new_team()
    client.post(f"/teams/{team_id}/agents/from-registry", json={"manifest_id": "misc.bare"})

    validation = client.get(f"/teams/{team_id}/roster/validation").json()
    assert validation["is_fully_staffed"] is True
    assert validation["gaps"] == []


def test_from_registry_empty_tags_agent_added_but_flagged_sparse(
    client: TestClient, registry: _FakeRegistry
) -> None:
    """A manifest with NO tags and no cognition resolves, for validation, to a
    persona with only ``expertise`` populated (skills/capabilities/tools all
    empty) — three missing categories, which ``roster_validation`` correctly
    flags ``sparse_profile``. The add still succeeds (201) with a thin row;
    validation honestly reports the team is not fully staffed once it hydrates
    the row from the manifest. (Studio-saved manifests always carry the
    'studio' tag, so this degenerate shape only arises for hand-authored /
    non-Studio catalog manifests — and a tag-less, tool-less, capability-less
    agent genuinely *is* under-specified.)"""
    registry.register(
        AgentManifest(
            id="empty.tags",
            team="empty",
            name="empty.tags",
            summary="No tags",
            tags=[],
            cognition=None,
            source=_SOURCE,
        )
    )
    team_id = _new_team()
    resp = client.post(f"/teams/{team_id}/agents/from-registry", json={"manifest_id": "empty.tags"})
    assert resp.status_code == 201
    body = resp.json()
    assert body["skills"] == []  # thin row: not populated at write time
    assert body["expertise"] == []  # thin row: not populated at write time

    validation = client.get(f"/teams/{team_id}/roster/validation").json()
    assert validation["is_fully_staffed"] is False  # sparse_profile: 3 categories missing
    assert any(g["category"] == "sparse_profile" for g in validation["gaps"])


def test_mixed_registry_and_generated_roster_validates_without_false_gaps(
    client: TestClient,
) -> None:
    """A roster mixing a thin registry-sourced agent and a fat generated agent
    validates correctly: the registry agent's persona is hydrated from its
    manifest (not read off its thin, empty roster row), and the generated
    agent's own fields are used directly — neither is falsely flagged."""
    team_id = _new_team()
    client.post(f"/teams/{team_id}/agents/from-registry", json={"manifest_id": "blogging.planner"})
    AgenticTeamStore().add_or_replace_team_agent(
        team_id,
        AgenticTeamAgent(
            agent_name="Editor",
            role="Edits drafts",
            skills=["editing"],
            tools=["Slack API"],
            expertise=["publishing"],
        ),
    )

    roster = client.get(f"/teams/{team_id}/agents").json()
    assert {a["agent_name"] for a in roster} == {"blogging.planner", "Editor"}
    assert next(a for a in roster if a["agent_name"] == "blogging.planner")["role"] == ""
    assert next(a for a in roster if a["agent_name"] == "Editor")["role"] == "Edits drafts"

    validation = client.get(f"/teams/{team_id}/roster/validation").json()
    assert validation["agent_count"] == 2
    assert validation["is_fully_staffed"] is True
    assert validation["gaps"] == []


def test_from_registry_is_idempotent_by_name(client: TestClient) -> None:
    """Re-adding the same manifest updates in place rather than duplicating."""
    team_id = _new_team()
    client.post(f"/teams/{team_id}/agents/from-registry", json={"manifest_id": "blogging.planner"})
    client.post(f"/teams/{team_id}/agents/from-registry", json={"manifest_id": "blogging.planner"})

    roster = client.get(f"/teams/{team_id}/agents").json()
    assert len(roster) == 1


def test_from_registry_invalid_body_422(client: TestClient) -> None:
    """The API contract is enforced: a missing or empty manifest_id is a 422."""
    team_id = _new_team()
    assert client.post(f"/teams/{team_id}/agents/from-registry", json={}).status_code == 422
    assert (
        client.post(f"/teams/{team_id}/agents/from-registry", json={"manifest_id": ""}).status_code
        == 422
    )


def test_from_registry_unknown_manifest_404(client: TestClient) -> None:
    """An unknown manifest id is a 404."""
    team_id = _new_team()
    resp = client.post(f"/teams/{team_id}/agents/from-registry", json={"manifest_id": "nope"})
    assert resp.status_code == 404


def test_from_registry_registry_lookup_failure_503(
    client: TestClient, registry: _FakeRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A registry lookup failure (e.g. the registry backend is unavailable) is a
    503, not an opaque 500 — and the roster is left unchanged."""

    def _boom(agent_id: str):
        raise RuntimeError("registry backend down")

    monkeypatch.setattr(registry, "get", _boom)
    team_id = _new_team()
    resp = client.post(
        f"/teams/{team_id}/agents/from-registry", json={"manifest_id": "blogging.planner"}
    )
    assert resp.status_code == 503
    assert "registry" in resp.json()["detail"].lower()
    assert client.get(f"/teams/{team_id}/agents").json() == []


def test_from_registry_validation_error_422(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pydantic ValidationError raised while projecting the manifest is a 422,
    the same documented contract as the explicit ValueError guards in
    ``_roster_agent_from_manifest`` — not an opaque 500."""
    from pydantic import BaseModel, ValidationError

    class _Probe(BaseModel):
        x: int

    try:
        _Probe(x="not-an-int")
        raise AssertionError("expected _Probe construction to fail")
    except ValidationError as captured:
        validation_error = captured

    from agent_team_studio.agentic_team_provisioning.api import main

    def _raise_validation_error(manifest):
        raise validation_error

    monkeypatch.setattr(main, "_roster_agent_from_manifest", _raise_validation_error)

    team_id = _new_team()
    resp = client.post(
        f"/teams/{team_id}/agents/from-registry", json={"manifest_id": "blogging.planner"}
    )
    assert resp.status_code == 422
    assert "manifest" in resp.json()["detail"].lower()
    assert client.get(f"/teams/{team_id}/agents").json() == []


def test_from_registry_unknown_team_404(client: TestClient) -> None:
    """Adding to an unknown team is a 404."""
    resp = client.post(
        "/teams/missing/agents/from-registry", json={"manifest_id": "blogging.planner"}
    )
    assert resp.status_code == 404


def test_from_registry_malformed_manifest_422(client: TestClient, registry: _FakeRegistry) -> None:
    """A resolvable but too-malformed-to-project manifest is a 422, not a 500.

    ``AgentManifest.name`` is required but not length-constrained, so a blank name
    passes Pydantic yet can't yield a usable roster agent. The route catches the
    projection's ``ValueError`` and surfaces it as a client error; the roster is
    left unchanged."""
    registry.register(
        AgentManifest(
            id="blank.name",
            team="misc",
            name="",  # passes Pydantic (no min_length) but unprojectable
            summary="Blank name",
            source=_SOURCE,
        )
    )
    team_id = _new_team()
    resp = client.post(f"/teams/{team_id}/agents/from-registry", json={"manifest_id": "blank.name"})
    assert resp.status_code == 422
    assert "manifest" in resp.json()["detail"].lower()
    # Roster untouched.
    assert client.get(f"/teams/{team_id}/agents").json() == []


def test_delete_removes_agent(client: TestClient) -> None:
    """Deleting a rostered agent returns 204 and empties the roster."""
    team_id = _new_team()
    client.post(f"/teams/{team_id}/agents/from-registry", json={"manifest_id": "blogging.planner"})

    resp = client.delete(f"/teams/{team_id}/agents/blogging.planner")
    assert resp.status_code == 204
    assert client.get(f"/teams/{team_id}/agents").json() == []


def test_delete_unknown_agent_404(client: TestClient) -> None:
    """Deleting an agent that isn't on the roster is a 404."""
    team_id = _new_team()
    resp = client.delete(f"/teams/{team_id}/agents/ghost")
    assert resp.status_code == 404


def test_delete_unknown_team_404(client: TestClient) -> None:
    """Deleting from an unknown team is a 404."""
    resp = client.delete("/teams/missing/agents/whoever")
    assert resp.status_code == 404


def test_delete_only_removes_the_named_agent(client: TestClient) -> None:
    """Single-agent delete must not disturb the rest of the roster."""
    team_id = _new_team()
    client.post(f"/teams/{team_id}/agents/from-registry", json={"manifest_id": "blogging.planner"})
    client.post(f"/teams/{team_id}/agents/from-registry", json={"manifest_id": "misc.bare"})

    client.delete(f"/teams/{team_id}/agents/blogging.planner")
    roster = client.get(f"/teams/{team_id}/agents").json()
    assert [a["agent_name"] for a in roster] == ["misc.bare"]


def test_delete_generated_agent_unregisters_its_manifest(
    client: TestClient, registry: _FakeRegistry
) -> None:
    """Deleting a generated roster agent also unregisters its in-process manifest,
    so catalog/invoke consumers stop resolving it (mirrors the full-save cleanup)."""
    from agent_team_studio.agentic_team_provisioning.manifest_generation import build_agent_manifest

    team_id = _new_team()
    gen = AgenticTeamAgent(
        agent_name="Writer Agent", role="Writes", skills=["seo"], source="generated"
    )
    AgenticTeamStore().save_team_agents(team_id, [gen])
    manifest = build_agent_manifest(team_id, gen)
    registry.register(manifest)  # simulate the LLM save path's install
    assert registry.get(manifest.id) is not None

    resp = client.delete(f"/teams/{team_id}/agents/Writer Agent")
    assert resp.status_code == 204
    assert registry.get(manifest.id) is None  # unregistered


def test_from_registry_replacing_generated_unregisters_old_manifest(
    client: TestClient, registry: _FakeRegistry
) -> None:
    """Adding a registry agent whose name matches an existing *generated* agent
    drops that generated agent's stale manifest from the live registry."""
    from agent_team_studio.agentic_team_provisioning.manifest_generation import build_agent_manifest

    team_id = _new_team()
    # A generated agent already on the roster + installed in the registry, named to
    # collide with the registry manifest we'll add (_PLANNER.name).
    gen = AgenticTeamAgent(
        agent_name="blogging.planner", role="old", skills=["x"], source="generated"
    )
    AgenticTeamStore().save_team_agents(team_id, [gen])
    old_manifest = build_agent_manifest(team_id, gen)
    registry.register(old_manifest)
    assert registry.get(old_manifest.id) is not None

    resp = client.post(
        f"/teams/{team_id}/agents/from-registry", json={"manifest_id": "blogging.planner"}
    )
    assert resp.status_code == 201
    assert resp.json()["source"] == "registry"  # roster row replaced
    assert registry.get(old_manifest.id) is None  # stale generated manifest dropped


def test_from_registry_rejects_own_generated_manifest_409(
    client: TestClient, registry: _FakeRegistry
) -> None:
    """Re-adding *this team's own* generated manifest is rejected (409), not applied.

    A generated roster row has ``manifest_id=None`` but is registered under
    ``manifest_agent_id(team_id, name)``; the manifest-id-only "already on roster"
    guards on the callers miss it. Without this guard the from-registry projection
    would create a registry-source row carrying that same id, and the on_replaced
    cleanup would unregister the very manifest it points at — leaving a roster entry
    whose manifest no longer resolves. The endpoint must refuse, leaving the roster
    and the registered manifest untouched.
    """
    from agent_team_studio.agentic_team_provisioning.manifest_generation import build_agent_manifest

    team_id = _new_team()
    gen = AgenticTeamAgent(
        agent_name="Planner", role="Plans things", skills=["x"], source="generated"
    )
    AgenticTeamStore().save_team_agents(team_id, [gen])
    gen_manifest = build_agent_manifest(team_id, gen)
    registry.register(gen_manifest)

    resp = client.post(
        f"/teams/{team_id}/agents/from-registry", json={"manifest_id": gen_manifest.id}
    )
    assert resp.status_code == 409

    # Roster row is still the untouched generated agent, and its manifest still resolves.
    roster = client.get(f"/teams/{team_id}/agents").json()
    assert len(roster) == 1
    assert roster[0]["agent_name"] == "Planner"
    assert roster[0]["source"] == "generated"
    assert roster[0]["manifest_id"] is None
    assert registry.get(gen_manifest.id) is not None


def test_from_registry_rejects_another_teams_generated_manifest_409(
    client: TestClient, registry: _FakeRegistry
) -> None:
    """A *different* team's generated manifest is also rejected (409).

    Generated manifests are ephemeral and roster-owned; adding one to another team
    would leave that row dangling the moment the owning team drops the agent (its
    ``register_team_manifests`` unregisters the manifest). The guard classifies on the
    ``"generated"`` tag, not this team's id prefix, so cross-team generated adds are
    refused too — leaving the target roster unchanged.
    """
    from agent_team_studio.agentic_team_provisioning.manifest_generation import build_agent_manifest

    other_team_id = _new_team()
    other_gen = AgenticTeamAgent(
        agent_name="Scout", role="Researches", skills=["x"], source="generated"
    )
    other_manifest = build_agent_manifest(other_team_id, other_gen)
    registry.register(other_manifest)

    team_id = _new_team()  # a distinct team with a different id prefix
    resp = client.post(
        f"/teams/{team_id}/agents/from-registry", json={"manifest_id": other_manifest.id}
    )
    assert resp.status_code == 409
    assert client.get(f"/teams/{team_id}/agents").json() == []
    assert registry.get(other_manifest.id) is not None


def test_llm_save_preserves_registry_agents(client: TestClient, registry: _FakeRegistry) -> None:
    """A chat-driven roster save must not drop a user-added registry agent, and a
    generated agent can't overwrite one by name (registry wins the collision).

    The merge is exercised by calling ``_save_agents_from_llm`` directly: it is the
    seam the conversation handler funnels every assistant ``agents`` block through,
    and driving it via the full chat API would require mocking the LLM agent — far
    heavier and less targeted than pinning this one invariant here.
    """
    from agent_team_studio.agentic_team_provisioning.api.main import _save_agents_from_llm

    team_id = _new_team()
    client.post(f"/teams/{team_id}/agents/from-registry", json={"manifest_id": "blogging.planner"})

    # The LLM round-trip only knows generated agents; one collides by name.
    _save_agents_from_llm(
        team_id,
        [
            {"agent_name": "Writer", "role": "Writes"},
            {"agent_name": "blogging.planner", "role": "x"},
        ],
    )

    roster = {a["agent_name"]: a["source"] for a in client.get(f"/teams/{team_id}/agents").json()}
    assert roster["blogging.planner"] == "registry"  # preserved, not overwritten
    assert roster["Writer"] == "generated"  # the new generated agent was added


def test_register_team_manifests_skips_registry_agents(
    client: TestClient, registry: _FakeRegistry
) -> None:
    """register_team_manifests must not install a generated wrapper for a
    registry-source agent (which would duplicate it on every restart), while it DOES
    register the generated wrapper and unregister this team's stale generated entries.
    The fake registry must implement ``manifests_with_id_prefix`` — without it
    register_team_manifests raises AttributeError."""
    from agent_team_studio.agentic_team_provisioning.manifest_generation import (
        build_agent_manifest,
        register_team_manifests,
    )

    team_id = _new_team()
    # A stale generated wrapper from a prior roster (agent no longer present) must be
    # unregistered by the prefix-scoped stale-cleanup.
    stale = build_agent_manifest(
        team_id, AgenticTeamAgent(agent_name="OldGen", role="o", skills=["x"], source="generated")
    )
    registry.register(stale)
    assert registry.get(stale.id) is not None

    gen = AgenticTeamAgent(agent_name="Writer", role="w", skills=["x"], source="generated")
    reg = AgenticTeamAgent(
        agent_name="blogging.planner",
        role="p",
        skills=["seo"],
        source="registry",
        manifest_id="blogging.planner",
    )

    result = register_team_manifests(team_id, [gen, reg])
    assert result.registered is True
    assert len(result.manifests) == 1  # only the generated agent is wrapped
    # The generated wrapper is actually installed (path exercised, not swallowed).
    assert registry.get(result.manifests[0].id) is result.manifests[0]
    # The stale generated wrapper was unregistered by the prefix scan.
    assert registry.get(stale.id) is None
    # The original registry manifest is untouched; no generated wrapper was added.
    assert registry.get("blogging.planner") is _PLANNER


def test_delete_registry_agent_keeps_global_manifest(
    client: TestClient, registry: _FakeRegistry
) -> None:
    """A registry-source agent exists in the registry independently of the team;
    removing it from the roster must NOT unregister it globally."""
    team_id = _new_team()
    client.post(f"/teams/{team_id}/agents/from-registry", json={"manifest_id": "blogging.planner"})

    client.delete(f"/teams/{team_id}/agents/blogging.planner")
    assert registry.get("blogging.planner") is not None  # still globally registered


def test_delete_agent_name_with_slash(client: TestClient) -> None:
    """Roster names containing '/' (e.g. 'Backend — API/OpenAPI Specialist') must be
    deletable — the :path converter matches the slash instead of 404-ing."""
    team_id = _new_team()
    name = "Backend — API/OpenAPI Specialist"
    AgenticTeamStore().save_team_agents(
        team_id, [AgenticTeamAgent(agent_name=name, role="Specs", skills=["openapi"])]
    )

    resp = client.delete(f"/teams/{team_id}/agents/{name}")
    assert resp.status_code == 204
    assert client.get(f"/teams/{team_id}/agents").json() == []


def _raise(*_args, **_kwargs):
    raise KeyError("registry exploded")


def test_delete_unregister_failure_still_returns_204(
    client: TestClient, registry: _FakeRegistry
) -> None:
    """A best-effort registry failure during cleanup must not 500 a succeeded delete."""
    from agent_team_studio.agentic_team_provisioning.manifest_generation import build_agent_manifest

    team_id = _new_team()
    gen = AgenticTeamAgent(agent_name="Writer", role="w", skills=["x"], source="generated")
    AgenticTeamStore().save_team_agents(team_id, [gen])
    registry.register(build_agent_manifest(team_id, gen))
    registry.unregister = _raise  # cleanup blows up

    resp = client.delete(f"/teams/{team_id}/agents/Writer")
    assert resp.status_code == 204  # primary op still succeeds
    assert client.get(f"/teams/{team_id}/agents").json() == []


def test_from_registry_replace_unregister_failure_still_returns_201(
    client: TestClient, registry: _FakeRegistry
) -> None:
    """Same best-effort guarantee on the from-registry replace-unregister path."""
    team_id = _new_team()
    gen = AgenticTeamAgent(
        agent_name="blogging.planner", role="old", skills=["x"], source="generated"
    )
    AgenticTeamStore().save_team_agents(team_id, [gen])
    registry.unregister = _raise  # cleanup blows up

    resp = client.post(
        f"/teams/{team_id}/agents/from-registry", json={"manifest_id": "blogging.planner"}
    )
    assert resp.status_code == 201  # primary op still succeeds
    assert resp.json()["source"] == "registry"


def test_update_agent_edits_only_supplied_fields(client: TestClient) -> None:
    """PUT edits the supplied fields and leaves the rest of the (thin) row untouched."""
    team_id = _new_team()
    client.post(f"/teams/{team_id}/agents/from-registry", json={"manifest_id": "blogging.planner"})

    resp = client.put(
        f"/teams/{team_id}/agents/blogging.planner",
        json={"role": "Custom role for this team", "skills": ["custom-skill"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["role"] == "Custom role for this team"
    assert body["skills"] == ["custom-skill"]
    # Untouched fields stay at their thin default — not projected from the manifest.
    assert body["tools"] == []
    assert body["expertise"] == []
    # source/manifest_id are fixed — never changed by this route.
    assert body["source"] == "registry"
    assert body["manifest_id"] == "blogging.planner"

    # Persisted.
    roster = client.get(f"/teams/{team_id}/agents").json()
    assert roster[0]["role"] == "Custom role for this team"


def test_update_agent_edits_generated_agent(client: TestClient) -> None:
    """A generated agent's fields (its only definition) are fully editable."""
    team_id = _new_team()
    AgenticTeamStore().save_team_agents(
        team_id, [AgenticTeamAgent(agent_name="Writer", role="Writes", skills=["seo"])]
    )

    resp = client.put(f"/teams/{team_id}/agents/Writer", json={"tools": ["Slack API"]})
    assert resp.status_code == 200
    body = resp.json()
    assert body["tools"] == ["Slack API"]
    assert body["role"] == "Writes"  # unset field kept
    assert body["source"] == "generated"


def test_update_agent_unknown_agent_404(client: TestClient) -> None:
    """Editing an agent not on the roster is a 404 (roster unchanged)."""
    team_id = _new_team()
    resp = client.put(f"/teams/{team_id}/agents/ghost", json={"role": "x"})
    assert resp.status_code == 404


def test_update_agent_unknown_team_404(client: TestClient) -> None:
    """Editing an agent on an unknown team is a 404."""
    resp = client.put("/teams/missing/agents/whoever", json={"role": "x"})
    assert resp.status_code == 404


def test_update_agent_empty_body_is_a_noop(client: TestClient) -> None:
    """An empty request body changes nothing (every field is optional/unset)."""
    team_id = _new_team()
    client.post(f"/teams/{team_id}/agents/from-registry", json={"manifest_id": "blogging.planner"})

    resp = client.put(f"/teams/{team_id}/agents/blogging.planner", json={})
    assert resp.status_code == 200
    body = resp.json()
    assert body["role"] == ""  # still thin — no override was ever supplied
    assert body["skills"] == []


def test_update_agent_explicit_null_role_rejected_422(client: TestClient) -> None:
    """An explicit ``{"role": null}`` is rejected with 422 (not persisted).

    Regression: ``role`` is required on ``AgenticTeamAgent`` but optional on
    ``UpdateAgentRequest``; without re-validation, ``model_copy`` would write
    ``role=None`` and every subsequent roster read would fail ``model_validate``.
    The re-validated merge rejects it up front and leaves the roster unchanged.
    """
    team_id = _new_team()
    client.post(f"/teams/{team_id}/agents/from-registry", json={"manifest_id": "blogging.planner"})

    resp = client.put(f"/teams/{team_id}/agents/blogging.planner", json={"role": None})
    assert resp.status_code == 422

    # The roster is unchanged and still readable (no corrupt row was written).
    roster = client.get(f"/teams/{team_id}/agents").json()
    assert roster[0]["role"] == ""


def test_update_generated_agent_reregisters_manifest_with_new_summary(
    client: TestClient, registry: _FakeRegistry
) -> None:
    """Editing a generated agent's role refreshes its live registry manifest in place
    (its ``summary`` = the new role), instead of leaving the stale pre-edit summary."""
    from agent_team_studio.agentic_team_provisioning.manifest_generation import build_agent_manifest

    team_id = _new_team()
    gen = AgenticTeamAgent(
        agent_name="Writer", role="Writes drafts", skills=["seo"], source="generated"
    )
    AgenticTeamStore().save_team_agents(team_id, [gen])
    manifest = build_agent_manifest(team_id, gen)
    registry.register(manifest)  # simulate the LLM save path's install
    assert registry.get(manifest.id).summary == "Writes drafts"

    resp = client.put(f"/teams/{team_id}/agents/Writer", json={"role": "Edits and publishes"})
    assert resp.status_code == 200
    # Same id (name unchanged), refreshed summary — catalog/invoke now reflect the edit.
    assert registry.get(manifest.id).summary == "Edits and publishes"


def test_update_registry_agent_does_not_register_a_generated_wrapper(
    client: TestClient, registry: _FakeRegistry
) -> None:
    """Editing a registry-source agent must not install a generated wrapper manifest
    for it (its manifest stays owned by the catalog)."""
    from agent_team_studio.agentic_team_provisioning.manifest_generation import manifest_agent_id

    team_id = _new_team()
    client.post(f"/teams/{team_id}/agents/from-registry", json={"manifest_id": "blogging.planner"})

    resp = client.put(
        f"/teams/{team_id}/agents/blogging.planner", json={"role": "Team-specific role"}
    )
    assert resp.status_code == 200
    assert resp.json()["source"] == "registry"
    # No generated wrapper was registered for the registry agent's team-namespaced id.
    assert registry.get(manifest_agent_id(team_id, "blogging.planner")) is None
    # The original catalog manifest is untouched.
    assert registry.get("blogging.planner") is _PLANNER


def test_update_team_agent_merges_over_the_lock_read_row(monkeypatch: pytest.MonkeyPatch) -> None:
    """``update_team_agent`` runs ``apply_updates`` on the row read **under the lock**,
    not a caller snapshot — so a patch that only touches ``role`` preserves fields set
    by an intervening write (the race Codex flagged). We simulate the intervening write
    by mutating the stored row, then confirm the merge sees the fresh value.
    """
    install_fake_postgres(monkeypatch)
    store = AgenticTeamStore()
    team_id = store.create_team(name="Pod", description="").team_id
    store.save_team_agents(
        team_id, [AgenticTeamAgent(agent_name="Writer", role="old", skills=["seo"])]
    )

    seen: dict[str, list[str]] = {}

    def _apply(current: AgenticTeamAgent) -> AgenticTeamAgent:
        # The callback receives the CURRENT stored row (skills intact), and only
        # patches role — skills must survive.
        seen["skills"] = list(current.skills)
        return current.model_copy(update={"role": "new"})

    updated = store.update_team_agent(team_id, "Writer", _apply)
    assert updated is not None
    assert seen["skills"] == ["seo"]  # merge saw the fresh row, not an empty patch
    assert updated.role == "new"
    assert updated.skills == ["seo"]  # intervening-write field preserved
    # Persisted.
    roster = {a.agent_name: a for a in store.list_team_agents(team_id)}
    assert roster["Writer"].role == "new"
    assert roster["Writer"].skills == ["seo"]


def test_update_team_agent_unknown_agent_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unknown agent yields ``None`` (roster unchanged); ``apply_updates`` is never
    called."""
    install_fake_postgres(monkeypatch)
    store = AgenticTeamStore()
    team_id = store.create_team(name="Pod", description="").team_id
    called = False

    def _apply(current: AgenticTeamAgent) -> AgenticTeamAgent:
        nonlocal called
        called = True
        return current

    assert store.update_team_agent(team_id, "ghost", _apply) is None
    assert called is False


def test_update_team_agent_apply_error_rolls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """A raising ``apply_updates`` propagates and leaves the row unchanged."""
    install_fake_postgres(monkeypatch)
    store = AgenticTeamStore()
    team_id = store.create_team(name="Pod", description="").team_id
    store.save_team_agents(team_id, [AgenticTeamAgent(agent_name="Writer", role="keep")])

    def _boom(current: AgenticTeamAgent) -> AgenticTeamAgent:
        raise ValueError("bad patch")

    with pytest.raises(ValueError):
        store.update_team_agent(team_id, "Writer", _boom)
    assert store.list_team_agents(team_id)[0].role == "keep"  # unchanged


@pytest.mark.parametrize("bad_name", ["", "   "])
def test_roster_agent_from_manifest_rejects_blank_name(bad_name: str) -> None:
    """DbC precondition: a manifest with a blank name fails fast rather than being
    projected into the roster (constructed via model_construct to bypass Pydantic)."""
    from agent_team_studio.agentic_team_provisioning.api.main import _roster_agent_from_manifest

    manifest = AgentManifest.model_construct(
        id="x.y", team="t", name=bad_name, summary="", tags=[], cognition=None
    )
    with pytest.raises(ValueError):
        _roster_agent_from_manifest(manifest)


def test_roster_agent_from_manifest_rejects_missing_id() -> None:
    """DbC precondition: a manifest with no id fails fast."""
    from agent_team_studio.agentic_team_provisioning.api.main import _roster_agent_from_manifest

    manifest = AgentManifest.model_construct(
        id="", team="t", name="ok", summary="", tags=[], cognition=None
    )
    with pytest.raises(ValueError):
        _roster_agent_from_manifest(manifest)


def test_roster_agent_from_manifest_projects_ref_only() -> None:
    """The projection copies no persona field off the manifest — only the thin
    agent_name/source/manifest_id identity (issue #5891)."""
    from agent_team_studio.agentic_team_provisioning.api.main import _roster_agent_from_manifest

    agent = _roster_agent_from_manifest(_PLANNER)

    assert agent.agent_name == "blogging.planner"
    assert agent.source == "registry"
    assert agent.manifest_id == "blogging.planner"
    assert agent.role == ""
    assert agent.skills == []
    assert agent.capabilities == []
    assert agent.tools == []
    assert agent.expertise == []


def test_full_save_preserves_created_at_and_prunes(monkeypatch: pytest.MonkeyPatch) -> None:
    """A full-roster save upserts survivors (keeping their original ``created_at``)
    and deletes only the agents no longer present."""
    db = install_fake_postgres(monkeypatch)
    store = AgenticTeamStore()
    team_id = store.create_team(name="Pod", description="").team_id

    keep = AgenticTeamAgent(agent_name="Keep", role="k", skills=["x"])
    drop = AgenticTeamAgent(agent_name="Drop", role="d", skills=["x"])
    store.save_team_agents(team_id, [keep, drop])
    original_created = db["team_agents"][(team_id, "Keep")]["created_at"]

    # Re-save: Keep survives, Drop is removed, Add is new.
    add = AgenticTeamAgent(agent_name="Add", role="a", skills=["x"])
    store.save_team_agents(team_id, [keep, add])

    names = {name for (_, name) in db["team_agents"] if _ == team_id}
    assert names == {"Keep", "Add"}  # Drop pruned, Add inserted
    # Keep's creation time is carried forward (upsert, not delete+reinsert).
    assert db["team_agents"][(team_id, "Keep")]["created_at"] == original_created

    # Saving an empty roster clears every row (the no-names branch).
    store.save_team_agents(team_id, [])
    assert store.list_team_agents(team_id) == []


def test_merge_generated_agents_unknown_team_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    """For an unknown team the roster is untouched and [] is returned, but on_merged is
    still invoked once with [] so the caller can reconcile a vanished team's external
    state (e.g. unregister its stale manifests)."""
    install_fake_postgres(monkeypatch)
    store = AgenticTeamStore()
    seen: list[list[str]] = []
    result = store.merge_generated_agents(
        "missing",
        [AgenticTeamAgent(agent_name="X", role="x", skills=["y"])],
        on_merged=lambda ms, _conn: seen.append([m.agent_name for m in ms]),
    )
    assert result == []
    assert store.list_team_agents("missing") == []
    assert seen == [[]]  # on_merged called once with the empty roster


def test_manifests_endpoint_returns_original_for_registry_agent(client: TestClient) -> None:
    """A registry-source roster agent advertises its *original* resolvable manifest id,
    while a generated agent still gets the synthetic stamped wrapper."""
    team_id = _new_team()
    client.post(f"/teams/{team_id}/agents/from-registry", json={"manifest_id": "blogging.planner"})
    # A generated agent alongside it (saved directly; chat path isn't exercised here).
    gen = AgenticTeamAgent(agent_name="Writer", role="Writes", skills=["seo"], source="generated")
    AgenticTeamStore().add_or_replace_team_agent(team_id, gen)

    manifests = {
        m["name"]: m for m in client.get(f"/teams/{team_id}/agents/manifests").json()["manifests"]
    }

    # Registry agent → original registry id (resolvable via /api/agents/{id}/invoke).
    assert manifests["blogging.planner"]["id"] == "blogging.planner"
    # Generated agent → synthetic team-namespaced id (the stamped wrapper).
    assert manifests["Writer"]["id"].startswith("agentic_team_provisioning.")


def test_manifests_endpoint_omits_unresolvable_registry_agent(client: TestClient) -> None:
    """A registry-source agent whose manifest_id doesn't resolve in this process is
    omitted (not advertised with a synthetic generated id that invoke would 404 on),
    while a resolvable registry sibling is still returned."""
    team_id = _new_team()
    store = AgenticTeamStore()
    store.add_or_replace_team_agent(
        team_id,
        AgenticTeamAgent(
            agent_name="Orphan",
            role="r",
            skills=["x"],
            source="registry",
            manifest_id="not.in.registry",
        ),
    )
    # A resolvable registry sibling.
    client.post(f"/teams/{team_id}/agents/from-registry", json={"manifest_id": "blogging.planner"})

    names = {
        m["name"] for m in client.get(f"/teams/{team_id}/agents/manifests").json()["manifests"]
    }
    assert names == {"blogging.planner"}  # Orphan omitted, resolvable one kept


def test_merge_generated_agents_invokes_on_merged(client: TestClient) -> None:
    """``merge_generated_agents`` calls ``on_merged`` once, under the lock, with the
    merged roster — the hook the chat-save path uses to register under the lock."""
    team_id = _new_team()
    seen: list[list[str]] = []

    store = AgenticTeamStore()
    merged = store.merge_generated_agents(
        team_id,
        [AgenticTeamAgent(agent_name="Writer", role="w", skills=["x"])],
        on_merged=lambda ms, _conn: seen.append([m.agent_name for m in ms]),
    )
    assert [m.agent_name for m in merged] == ["Writer"]
    assert seen == [["Writer"]]  # called exactly once with the merged list


def test_merge_generated_agents_passes_conn_and_propagates_on_merged_failure(
    client: TestClient,
) -> None:
    """Chat-save wiring: on_merged receives the roster conn and a raise escapes.

    Production ``get_conn`` rolls the open transaction back on that escape so the
    roster write is undone together with any registry statements that joined
    ``conn``. The dict-backed fake mutates eagerly (no txn rollback), so this
    test asserts the fail-closed *control path* rather than fake DB contents.
    """
    team_id = _new_team()
    store = AgenticTeamStore()
    store.save_team_agents(
        team_id,
        [AgenticTeamAgent(agent_name="Prior", role="p", skills=["x"], source="generated")],
    )

    seen_conn: list[object] = []

    def _boom(merged, conn):
        seen_conn.append(conn)
        raise RuntimeError("registry replace failed")

    with pytest.raises(RuntimeError, match="registry replace failed"):
        store.merge_generated_agents(
            team_id,
            [AgenticTeamAgent(agent_name="New", role="n", skills=["y"])],
            on_merged=_boom,
        )

    assert len(seen_conn) == 1
    assert seen_conn[0] is not None
