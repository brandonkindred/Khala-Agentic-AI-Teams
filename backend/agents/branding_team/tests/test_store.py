"""Tests for branding store (clients and brands).

These tests mock ``shared_postgres.get_conn`` with a tiny dict-backed
fake — see ``_fake_postgres.py``.
"""

from __future__ import annotations

import pytest

from branding_team.models import (
    BrandingMission,
    BrandPhase,
    BrandStatus,
    TeamOutput,
    WorkflowStatus,
)
from branding_team.store import BrandingStore
from branding_team.tests._fake_postgres import install_fake_postgres


@pytest.fixture
def fake_pg(monkeypatch: pytest.MonkeyPatch) -> dict:
    return install_fake_postgres(monkeypatch)


def test_create_client_and_list(fake_pg: dict) -> None:
    store = BrandingStore()
    client = store.create_client("Acme Corp")
    assert client.id.startswith("client_")
    assert client.name == "Acme Corp"
    assert client.created_at
    clients = store.list_clients()
    assert len(clients) == 1
    assert store.get_client(client.id) == client


def test_create_brand_and_list(fake_pg: dict) -> None:
    store = BrandingStore()
    client = store.create_client("Acme")
    mission = BrandingMission(
        company_name="Acme Inc",
        company_description="A great company",
        target_audience="everyone",
    )
    brand = store.create_brand(client.id, mission, name="Acme Brand")
    assert brand is not None
    assert brand.id.startswith("brand_")
    assert brand.client_id == client.id
    assert brand.name == "Acme Brand"
    assert brand.status == BrandStatus.draft
    assert brand.current_phase == BrandPhase.STRATEGIC_CORE
    assert brand.mission.company_name == "Acme Inc"
    brands = store.list_brands_for_client(client.id)
    assert len(brands) == 1
    assert store.get_brand(client.id, brand.id) == brand


def test_get_brand_wrong_client_returns_none(fake_pg: dict) -> None:
    store = BrandingStore()
    c1 = store.create_client("C1")
    c2 = store.create_client("C2")
    mission = BrandingMission(
        company_name="XY",
        company_description="A description that is long enough",
        target_audience="Everyone",
    )
    brand = store.create_brand(c1.id, mission)
    assert brand is not None
    assert store.get_brand(c2.id, brand.id) is None


def test_update_brand(fake_pg: dict) -> None:
    store = BrandingStore()
    client = store.create_client("Acme")
    mission = BrandingMission(
        company_name="Acme Inc",
        company_description="A great company",
        target_audience="everyone",
    )
    brand = store.create_brand(client.id, mission)
    assert brand is not None
    new_mission = mission.model_copy(update={"company_description": "Updated description"})
    updated = store.update_brand(
        client.id, brand.id, mission=new_mission, status=BrandStatus.active
    )
    assert updated is not None
    assert updated.mission.company_description == "Updated description"
    assert updated.status == BrandStatus.active


def test_append_brand_version(fake_pg: dict) -> None:
    store = BrandingStore()
    client = store.create_client("Acme")
    mission = BrandingMission(
        company_name="Acme Inc",
        company_description="A great company",
        target_audience="everyone",
    )
    brand = store.create_brand(client.id, mission)
    assert brand is not None
    assert brand.version == 0
    assert len(brand.history) == 0
    output = TeamOutput(
        status=WorkflowStatus.READY_FOR_ROLLOUT,
        mission_summary="Done",
        current_phase=BrandPhase.COMPLETE,
    )
    updated = store.append_brand_version(client.id, brand.id, output)
    assert updated is not None
    assert updated.version == 1
    assert len(updated.history) == 1
    assert updated.latest_output is not None
    assert updated.latest_output.mission_summary == "Done"
    assert updated.current_phase == BrandPhase.COMPLETE


def test_append_brand_version_persists_current_phase(fake_pg: dict) -> None:
    """Verify that current_phase on the brand record is updated from the output."""
    store = BrandingStore()
    client = store.create_client("PhaseTest")
    mission = BrandingMission(
        company_name="PhaseTestCo",
        company_description="Company for phase persistence test",
        target_audience="testers",
    )
    brand = store.create_brand(client.id, mission)
    assert brand is not None
    assert brand.current_phase == BrandPhase.STRATEGIC_CORE

    output = TeamOutput(
        status=WorkflowStatus.READY_FOR_ROLLOUT,
        mission_summary="Governance done",
        current_phase=BrandPhase.GOVERNANCE,
    )
    store.append_brand_version(client.id, brand.id, output)

    reloaded = store.get_brand(client.id, brand.id)
    assert reloaded is not None
    assert reloaded.current_phase == BrandPhase.GOVERNANCE

    output2 = output.model_copy(
        update={"current_phase": BrandPhase.COMPLETE, "mission_summary": "All done"}
    )
    store.append_brand_version(client.id, brand.id, output2)
    reloaded2 = store.get_brand(client.id, brand.id)
    assert reloaded2 is not None
    assert reloaded2.current_phase == BrandPhase.COMPLETE


def test_create_brand_for_nonexistent_client_returns_none(fake_pg: dict) -> None:
    store = BrandingStore()
    mission = BrandingMission(
        company_name="XY",
        company_description="Long enough description",
        target_audience="Everyone",
    )
    brand = store.create_brand("nonexistent_client_id", mission)
    assert brand is None


def _mission() -> BrandingMission:
    return BrandingMission(
        company_name="Acme Inc",
        company_description="A great company",
        target_audience="everyone",
    )


def test_brand_exists(fake_pg: dict) -> None:
    store = BrandingStore()
    client = store.create_client("Acme")
    brand = store.create_brand(client.id, _mission())
    assert brand is not None
    assert store.brand_exists(brand.id) is True
    assert store.brand_exists("brand_does_not_exist") is False


def test_get_brand_by_id_resolves_client(fake_pg: dict) -> None:
    store = BrandingStore()
    client = store.create_client("Acme")
    brand = store.create_brand(client.id, _mission())
    assert brand is not None
    found = store.get_brand_by_id(brand.id)
    assert found is not None
    resolved_client_id, resolved_brand = found
    assert resolved_client_id == client.id
    assert resolved_brand.id == brand.id
    assert store.get_brand_by_id("brand_missing") is None


def test_get_brand_names_returns_only_requested(fake_pg: dict) -> None:
    store = BrandingStore()
    client = store.create_client("Acme")
    b1 = store.create_brand(client.id, _mission(), name="First")
    b2 = store.create_brand(client.id, _mission(), name="Second")
    assert b1 is not None and b2 is not None

    names = store.get_brand_names([b1.id, "brand_missing"])
    assert names == {b1.id: "First"}
    assert b2.id not in names

    # Empty / falsy input issues no query and returns an empty map.
    assert store.get_brand_names([]) == {}
    assert store.get_brand_names([""]) == {}


def test_list_clients_pagination(fake_pg: dict) -> None:
    store = BrandingStore()
    created = [store.create_client(f"Client {i}") for i in range(5)]
    assert len(store.list_clients()) == 5
    first_two = store.list_clients(limit=2, offset=0)
    assert len(first_two) == 2
    next_two = store.list_clients(limit=2, offset=2)
    assert len(next_two) == 2
    # Pages do not overlap and stay within the created set.
    ids = {c.id for c in first_two} | {c.id for c in next_two}
    assert len(ids) == 4
    assert ids <= {c.id for c in created}


def test_list_brands_for_client_pagination(fake_pg: dict) -> None:
    store = BrandingStore()
    client = store.create_client("Acme")
    for _ in range(3):
        assert store.create_brand(client.id, _mission()) is not None
    assert len(store.list_brands_for_client(client.id)) == 3
    page = store.list_brands_for_client(client.id, limit=1, offset=1)
    assert len(page) == 1
