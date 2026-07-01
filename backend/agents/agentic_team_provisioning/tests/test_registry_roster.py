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
from agentic_team_provisioning.assistant.store import AgenticTeamStore
from agentic_team_provisioning.models import AgenticTeamAgent
from agentic_team_provisioning.tests._fake_postgres import install_fake_postgres

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

    def manifests_with_id_prefix(self, prefix: str) -> list[AgentManifest]:
        # Mirror AgentRegistry.manifests_with_id_prefix so register_team_manifests'
        # stale-cleanup scan runs against the fake instead of AttributeError-ing
        # into register_team_manifests' best-effort try/except (which would silently
        # skip the register/unregister path in every test using this fake).
        return [m for m in self._by_id.values() if m.id.startswith(prefix)]

    def register(self, manifest: AgentManifest, source_path=None) -> None:
        self._by_id[manifest.id] = manifest

    def unregister(self, agent_id: str) -> bool:
        return self._by_id.pop(agent_id, None) is not None


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
    from agentic_team_provisioning.api.main import app

    return TestClient(app)


def _new_team() -> str:
    return AgenticTeamStore().create_team(name="Growth Pod", description="").team_id


def test_from_registry_projects_and_persists(client: TestClient) -> None:
    """201 with the projected fields, and the agent is persisted on the roster."""
    team_id = _new_team()

    resp = client.post(
        f"/teams/{team_id}/agents/from-registry", json={"manifest_id": "blogging.planner"}
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["agent_name"] == "blogging.planner"
    assert body["role"] == "Plans SEO-aware blog outlines"
    assert body["skills"] == ["studio", "seo"]  # from manifest tags
    assert body["tools"] == ["web.search", "draft"]  # from cognition.tools
    assert body["expertise"] == ["blogging"]  # from the home team
    assert body["source"] == "registry"
    assert body["manifest_id"] == "blogging.planner"

    # Persisted on the roster.
    roster = client.get(f"/teams/{team_id}/agents").json()
    assert [a["agent_name"] for a in roster] == ["blogging.planner"]
    assert roster[0]["source"] == "registry"


def test_from_registry_bare_manifest_falls_back(client: TestClient) -> None:
    """No summary → role falls back to name; no cognition → empty tools."""
    team_id = _new_team()
    resp = client.post(f"/teams/{team_id}/agents/from-registry", json={"manifest_id": "misc.bare"})
    assert resp.status_code == 201
    body = resp.json()
    assert body["role"] == "misc.bare"
    assert body["tools"] == []
    assert body["skills"] == ["studio"]
    assert body["expertise"] == ["misc"]


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
    """A manifest with NO tags and no cognition projects to a roster agent with only
    ``expertise`` populated (skills/capabilities/tools all empty) — three missing
    categories, which ``roster_validation`` correctly flags ``sparse_profile``. The
    add still succeeds (201); validation honestly reports the team is not fully
    staffed. (Studio-saved manifests always carry the 'studio' tag, so this
    degenerate shape only arises for hand-authored / non-Studio catalog manifests —
    and a tag-less, tool-less, capability-less agent genuinely *is* under-specified.)"""
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
    assert body["skills"] == []  # empty tags → empty skills
    assert body["expertise"] == ["empty"]  # only expertise populated

    validation = client.get(f"/teams/{team_id}/roster/validation").json()
    assert validation["is_fully_staffed"] is False  # sparse_profile: 3 categories missing
    assert any(g["category"] == "sparse_profile" for g in validation["gaps"])


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
    from agentic_team_provisioning.manifest_generation import build_agent_manifest

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
    from agentic_team_provisioning.manifest_generation import build_agent_manifest

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


def test_llm_save_preserves_registry_agents(client: TestClient, registry: _FakeRegistry) -> None:
    """A chat-driven roster save must not drop a user-added registry agent, and a
    generated agent can't overwrite one by name (registry wins the collision).

    The merge is exercised by calling ``_save_agents_from_llm`` directly: it is the
    seam the conversation handler funnels every assistant ``agents`` block through,
    and driving it via the full chat API would require mocking the LLM agent — far
    heavier and less targeted than pinning this one invariant here.
    """
    from agentic_team_provisioning.api.main import _save_agents_from_llm

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
    register the generated wrapper and unregister this team's stale generated entries
    — the register/unregister path that silently no-ops if the fake registry lacks
    ``manifests_with_id_prefix`` (register_team_manifests swallows the AttributeError)."""
    from agentic_team_provisioning.manifest_generation import (
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

    manifests = register_team_manifests(team_id, [gen, reg])
    assert len(manifests) == 1  # only the generated agent is wrapped
    # The generated wrapper is actually installed (path exercised, not swallowed).
    assert registry.get(manifests[0].id) is manifests[0]
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
    from agentic_team_provisioning.manifest_generation import build_agent_manifest

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
    """PUT edits the supplied fields and leaves the rest of the row untouched."""
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
    # Untouched fields keep their projected values.
    assert body["tools"] == ["web.search", "draft"]
    assert body["expertise"] == ["blogging"]
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
    assert body["role"] == "Plans SEO-aware blog outlines"
    assert body["skills"] == ["studio", "seo"]


@pytest.mark.parametrize("bad_name", ["", "   "])
def test_roster_agent_from_manifest_rejects_blank_name(bad_name: str) -> None:
    """DbC precondition: a manifest with a blank name fails fast rather than being
    projected into the roster (constructed via model_construct to bypass Pydantic)."""
    from agentic_team_provisioning.api.main import _roster_agent_from_manifest

    manifest = AgentManifest.model_construct(
        id="x.y", team="t", name=bad_name, summary="", tags=[], cognition=None
    )
    with pytest.raises(ValueError):
        _roster_agent_from_manifest(manifest)


def test_roster_agent_from_manifest_rejects_missing_id() -> None:
    """DbC precondition: a manifest with no id fails fast."""
    from agentic_team_provisioning.api.main import _roster_agent_from_manifest

    manifest = AgentManifest.model_construct(
        id="", team="t", name="ok", summary="", tags=[], cognition=None
    )
    with pytest.raises(ValueError):
        _roster_agent_from_manifest(manifest)


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
        on_merged=lambda ms: seen.append([m.agent_name for m in ms]),
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
        on_merged=lambda ms: seen.append([m.agent_name for m in ms]),
    )
    assert [m.agent_name for m in merged] == ["Writer"]
    assert seen == [["Writer"]]  # called exactly once with the merged list
