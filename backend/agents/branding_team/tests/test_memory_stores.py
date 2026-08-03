"""Unit tests for branding_team memory store doubles (no Postgres)."""

from __future__ import annotations

from branding_team.models import BrandPhase, TeamOutput, WorkflowStatus
from branding_team.tests._memory_stores import MemorySessionStore, MemoryStoreBundle
from branding_team.tests.conftest import make_mission


def _output(summary: str = "draft") -> TeamOutput:
    return TeamOutput(
        status=WorkflowStatus.NEEDS_HUMAN_DECISION,
        mission_summary=summary,
        current_phase=BrandPhase.STRATEGIC_CORE,
    )


def test_memory_session_get_returns_detached_copy() -> None:
    """Mutating a session from ``get`` must not change the store until ``save``.

    Mirrors real ``BrandingSessionStore.get``, which deserializes fresh JSON
    rather than handing out a live row object.
    """
    store = MemorySessionStore(MemoryStoreBundle())
    sid, _ = store.create(mission=make_mission(), latest_output=_output())

    loaded = store.get(sid)
    assert loaded is not None
    loaded.latest_output = _output("mutated-without-save")

    reread = store.get(sid)
    assert reread is not None
    assert reread.latest_output is not None
    assert reread.latest_output.mission_summary == "draft"

    store.save(sid, loaded)
    after_save = store.get(sid)
    assert after_save is not None
    assert after_save.latest_output is not None
    assert after_save.latest_output.mission_summary == "mutated-without-save"
