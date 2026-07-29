"""Tests for BrandingSessionStore against live Postgres."""

from __future__ import annotations

import pytest

from branding_team.api.state import BrandingSessionStore
from branding_team.models import BrandPhase, TeamOutput, WorkflowStatus
from branding_team.postgres import SCHEMA as BRANDING_SCHEMA
from branding_team.tests.conftest import make_mission
from shared.postgres.testing import real_postgres_schema

pytestmark = [pytest.mark.integration, pytest.mark.real_postgres]

_branding_schema = real_postgres_schema(BRANDING_SCHEMA, scope="function", autouse=True)


def _output() -> TeamOutput:
    return TeamOutput(
        status=WorkflowStatus.NEEDS_HUMAN_DECISION,
        mission_summary="draft",
        current_phase=BrandPhase.STRATEGIC_CORE,
    )


def test_create_and_get_round_trip() -> None:
    store = BrandingSessionStore()
    mission = make_mission()
    sid, session = store.create(mission=mission, latest_output=_output())
    loaded = store.get(sid)
    assert loaded is not None
    assert loaded.mission.company_name == mission.company_name
    assert loaded.latest_output is not None


def test_get_unknown_returns_none() -> None:
    assert BrandingSessionStore().get("missing-session") is None


def test_save_persists_mutations() -> None:
    store = BrandingSessionStore()
    sid, session = store.create(mission=make_mission(), latest_output=_output())
    session.latest_output = _output()
    session.latest_output.mission_summary = "updated"
    store.save(sid, session)
    loaded = store.get(sid)
    assert loaded is not None
    assert loaded.latest_output is not None
    assert loaded.latest_output.mission_summary == "updated"


def test_save_unknown_is_noop() -> None:
    store = BrandingSessionStore()
    _, session = store.create(mission=make_mission(), latest_output=_output())
    store.save("no-such-session", session)  # must not raise
    assert store.get("no-such-session") is None
