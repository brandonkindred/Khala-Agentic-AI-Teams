"""Unit tests for the branding conversation store (dict-backed fake Postgres).

Focuses on the single-query ``get_state`` loader and its backward-compatible
``get`` view.
"""

from __future__ import annotations

import pytest

from branding_team.assistant.store import BrandingConversationStore
from branding_team.models import BrandingMission
from branding_team.tests._fake_postgres import install_fake_postgres


@pytest.fixture
def fake_pg(monkeypatch: pytest.MonkeyPatch) -> dict:
    return install_fake_postgres(monkeypatch)


def _mission() -> BrandingMission:
    return BrandingMission(
        company_name="Acme",
        company_description="A great company",
        target_audience="developers",
    )


def test_get_state_returns_none_for_unknown(fake_pg: dict) -> None:
    store = BrandingConversationStore()
    assert store.get_state("missing") is None
    assert store.get("missing") is None


def test_get_state_empty_conversation(fake_pg: dict) -> None:
    store = BrandingConversationStore()
    cid = store.create(mission=_mission())
    state = store.get_state(cid)
    assert state is not None
    assert state.messages == []
    assert state.brand_id is None
    assert state.latest_output is None
    assert state.mission.company_name == "Acme"


def test_get_state_includes_messages_and_brand_id(fake_pg: dict) -> None:
    store = BrandingConversationStore()
    cid = store.create(brand_id="brand_xyz", mission=_mission())
    assert store.append_message(cid, "user", "hello")
    assert store.append_message(cid, "assistant", "hi there")

    state = store.get_state(cid)
    assert state is not None
    assert state.brand_id == "brand_xyz"
    assert [(m.role, m.content) for m in state.messages] == [
        ("user", "hello"),
        ("assistant", "hi there"),
    ]

    # Backward-compatible 3-tuple view stays in sync.
    legacy = store.get(cid)
    assert legacy is not None
    messages, mission, latest_output = legacy
    assert len(messages) == 2
    assert mission.company_name == "Acme"
    assert latest_output is None
