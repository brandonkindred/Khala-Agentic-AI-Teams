"""Join-at-read Manifest → RosterPersonaView."""

from __future__ import annotations

import pytest
from agent_registry.models import AgentManifest, CognitionSpec, SourceInfo
from pydantic import ValidationError

from agent_team_studio.agentic_team_provisioning.manifest_generation import (
    build_agent_manifest,
    manifest_agent_id,
)
from agent_team_studio.agentic_team_provisioning.models import AgenticTeamAgent
from agent_team_studio.agentic_team_provisioning.roster_resolve import (
    coerce_roster_agent,
    migrate_roster_row,
    persona_from_manifest,
    resolve_persona,
)


def _manifest(**kwargs) -> AgentManifest:
    base = dict(
        id="demo.planner",
        team="demo",
        name="Planner",
        summary="Plans work",
        tags=["planning"],
        cognition=CognitionSpec(tools=["web_search"]),
        source=SourceInfo(entrypoint="demo.planner:run"),
    )
    base.update(kwargs)
    return AgentManifest.model_validate(base)


def test_persona_from_manifest_maps_summary_tags_tools_team() -> None:
    view = persona_from_manifest(_manifest())
    assert view.role == "Plans work"
    assert view.skills == ["planning"]
    assert view.tools == ["web_search"]
    assert view.expertise == ["demo"]
    assert view.capabilities == []


def test_persona_from_manifest_empty_cognition_tools() -> None:
    view = persona_from_manifest(_manifest(cognition=None, tags=[]))
    assert view.tools == []
    assert view.skills == []
    assert view.expertise == ["demo"]


def test_persona_from_manifest_whitespace_summary_falls_back_to_name() -> None:
    """Blank/whitespace summary must not leave role empty when name is set."""
    view = persona_from_manifest(_manifest(summary="   \t  "))
    assert view.role == "Planner"


def test_coerce_roster_agent_accepts_fat_history_with_null_manifest_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Temporal history may still carry fat rows; coerce must not ValidationError."""
    team_id = "team-1"
    raw = {
        "agent_name": "worker",
        "role": "doer",
        "skills": ["x"],
        "capabilities": [],
        "tools": [],
        "expertise": [],
        "source": "generated",
        "manifest_id": None,
    }
    expected_id = manifest_agent_id(team_id, "worker")

    class _Reg:
        def __init__(self) -> None:
            self._m: dict = {}

        def get(self, agent_id: str):
            return self._m.get(agent_id)

        def register(self, manifest, source_path=None, *, require_persist: bool = False):
            self._m[manifest.id] = manifest

    reg = _Reg()
    monkeypatch.setattr("agent_registry.get_registry", lambda: reg)

    with pytest.raises(ValidationError):
        AgenticTeamAgent.model_validate(raw)

    agent = coerce_roster_agent(team_id, raw)
    assert isinstance(agent, AgenticTeamAgent)
    assert agent.model_dump(mode="json") == {
        "agent_name": "worker",
        "source": "generated",
        "manifest_id": expected_id,
    }


def test_resolve_persona_looks_up_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    m = _manifest()

    class _Reg:
        def get(self, agent_id: str):
            return m if agent_id == m.id else None

    monkeypatch.setattr("agent_registry.get_registry", lambda: _Reg())
    assert resolve_persona("demo.planner").role == "Plans work"


def test_resolve_persona_missing_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Reg:
        def get(self, agent_id: str):
            return None

    monkeypatch.setattr("agent_registry.get_registry", lambda: _Reg())
    with pytest.raises(LookupError):
        resolve_persona("missing.id")


def test_migrate_generated_stamps_manifest_id_and_strips_fat(monkeypatch: pytest.MonkeyPatch) -> None:
    team_id = "team-1"
    raw = {
        "agent_name": "Writer",
        "role": "Writes docs",
        "skills": ["seo"],
        "capabilities": [],
        "tools": [],
        "expertise": [],
        "source": "generated",
        "manifest_id": None,
    }
    expected_id = manifest_agent_id(team_id, "Writer")

    class _Reg:
        def __init__(self) -> None:
            self._m: dict = {}

        def get(self, agent_id: str):
            return self._m.get(agent_id)

        def register(self, manifest, source_path=None, *, require_persist: bool = False):
            self._m[manifest.id] = manifest

    reg = _Reg()
    monkeypatch.setattr("agent_registry.get_registry", lambda: reg)

    agent, changed = migrate_roster_row(team_id, raw)
    assert changed is True
    assert agent.agent_name == "Writer"
    assert agent.source == "generated"
    assert agent.manifest_id == expected_id
    stamped = reg.get(expected_id)
    assert stamped is not None
    assert "seo" in stamped.tags
    assert stamped.summary == "Writes docs"


def test_migrate_with_manifest_id_merges_fat_skills(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fat skills alongside an already-stamped manifest_id must fold into Manifest tags."""
    team_id = "team-1"
    mid = manifest_agent_id(team_id, "Writer")
    prior = build_agent_manifest(team_id, "Writer", summary="old", skill_tags=["prior"])
    raw = {
        "agent_name": "Writer",
        "role": "Writes docs",
        "skills": ["seo"],
        "source": "generated",
        "manifest_id": mid,
    }

    class _Reg:
        def __init__(self) -> None:
            self._m = {mid: prior}

        def get(self, agent_id: str):
            return self._m.get(agent_id)

        def register(self, manifest, source_path=None, *, require_persist: bool = False):
            self._m[manifest.id] = manifest

    reg = _Reg()
    monkeypatch.setattr("agent_registry.get_registry", lambda: reg)

    agent, changed = migrate_roster_row(team_id, raw)
    assert changed is True
    assert agent.manifest_id == mid
    updated = reg.get(mid)
    assert updated is not None
    assert "prior" in updated.tags
    assert "seo" in updated.tags
    assert updated.summary == "Writes docs"


def test_migrate_persist_failure_raises_before_thin_return(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """require_persist failure must abort migrate so callers keep the fat row."""
    team_id = "team-1"
    raw = {
        "agent_name": "Writer",
        "role": "Writes docs",
        "skills": ["seo"],
        "source": "generated",
        "manifest_id": None,
    }

    class _Reg:
        def get(self, agent_id: str):
            return None

        def register(self, manifest, source_path=None, *, require_persist: bool = False):
            assert require_persist is True
            raise RuntimeError("boom:upsert")

    monkeypatch.setattr("agent_registry.get_registry", lambda: _Reg())

    with pytest.raises(RuntimeError, match="boom:upsert"):
        migrate_roster_row(team_id, raw)


def test_migrate_registry_without_manifest_id_raises() -> None:
    with pytest.raises(ValueError, match="manifest_id"):
        migrate_roster_row(
            "team-1",
            {"agent_name": "X", "source": "registry", "manifest_id": None, "role": "r"},
        )


def test_migrate_already_thin_unchanged() -> None:
    raw = {
        "agent_name": "Writer",
        "source": "generated",
        "manifest_id": "agentic_team_provisioning.abc.writer-1",
    }
    agent, changed = migrate_roster_row("team-1", raw)
    assert changed is False
    assert agent.manifest_id == raw["manifest_id"]


def test_migrate_with_manifest_id_and_fat_keys_changed(monkeypatch: pytest.MonkeyPatch) -> None:
    team_id = "team-1"
    manifest_id = manifest_agent_id(team_id, "Writer")
    prior = build_agent_manifest(team_id, "Writer", summary="old")
    raw = {
        "agent_name": "Writer",
        "source": "generated",
        "manifest_id": manifest_id,
        "role": "Writes docs",
        "skills": ["seo"],
    }

    class _Reg:
        def __init__(self) -> None:
            self._m = {manifest_id: prior}

        def get(self, agent_id: str):
            return self._m.get(agent_id)

        def register(self, manifest, source_path=None, *, require_persist: bool = False):
            self._m[manifest.id] = manifest

    reg = _Reg()
    monkeypatch.setattr("agent_registry.get_registry", lambda: reg)

    agent, changed = migrate_roster_row(team_id, raw)
    assert changed is True
    assert agent.agent_name == "Writer"
    assert agent.source == "generated"
    assert agent.manifest_id == manifest_id
    assert "seo" in reg.get(manifest_id).tags
    assert reg.get(manifest_id).summary == "Writes docs"
