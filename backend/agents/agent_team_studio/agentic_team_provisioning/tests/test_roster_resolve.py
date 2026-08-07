"""Join-at-read Manifest → RosterPersonaView."""

from __future__ import annotations

import pytest
from agent_registry.models import AgentManifest, CognitionSpec, SourceInfo

from agent_team_studio.agentic_team_provisioning.roster_resolve import (
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


def test_resolve_persona_looks_up_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    m = _manifest()

    class _Reg:
        def get(self, agent_id: str):
            return m if agent_id == m.id else None

    monkeypatch.setattr(
        "agent_team_studio.agentic_team_provisioning.roster_resolve.get_registry",
        lambda: _Reg(),
    )
    assert resolve_persona("demo.planner").role == "Plans work"


def test_resolve_persona_missing_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Reg:
        def get(self, agent_id: str):
            return None

    monkeypatch.setattr(
        "agent_team_studio.agentic_team_provisioning.roster_resolve.get_registry",
        lambda: _Reg(),
    )
    with pytest.raises(LookupError):
        resolve_persona("missing.id")
