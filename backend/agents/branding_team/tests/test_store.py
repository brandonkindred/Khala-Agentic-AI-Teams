"""Tests for branding store (clients and brands).

These tests mock ``shared_postgres.get_conn`` with a tiny dict-backed
fake — see ``_fake_postgres.py``.
"""

from __future__ import annotations

import os

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

_POSTGRES_HOST = os.environ.get("POSTGRES_HOST", "").strip()


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


def test_get_brand_by_id_resolves_without_client(fake_pg: dict) -> None:
    store = BrandingStore()
    client = store.create_client("Acme")
    mission = BrandingMission(
        company_name="Acme Inc",
        company_description="A great company",
        target_audience="everyone",
    )
    brand = store.create_brand(client.id, mission)
    assert brand is not None
    found = store.get_brand_by_id(brand.id)
    assert found is not None
    assert found.id == brand.id
    assert found.client_id == client.id
    assert store.get_brand_by_id("brand_does_not_exist") is None


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


@pytest.mark.skipif(
    not _POSTGRES_HOST,
    reason="POSTGRES_HOST not set; skipping live Postgres store check",
)
def test_update_and_append_against_live_postgres() -> None:
    """Validate the atomic merge / jsonb_set SQL against real Postgres.

    The unit tests above run against a hand-written fake that mirrors the
    implementation; this exercises the actual ``data || patch`` merge and the
    nested ``jsonb_set`` version-bump/history-append on a live engine so the two
    can never silently diverge. Runs only when ``POSTGRES_HOST`` is set.
    """
    from branding_team.postgres import SCHEMA
    from shared_postgres import get_conn, register_team_schemas

    register_team_schemas(SCHEMA)  # idempotent CREATE TABLE IF NOT EXISTS
    store = BrandingStore()
    client = store.create_client("Live Co")
    mission = BrandingMission(
        company_name="LiveCo",
        company_description="A sufficiently long company description",
        target_audience="everyone",
    )
    brand = store.create_brand(client.id, mission)
    assert brand is not None
    try:
        # update_brand: shallow merge replaces only the named fields.
        updated = store.update_brand(client.id, brand.id, status=BrandStatus.active, name="Renamed")
        assert updated is not None
        assert updated.status == BrandStatus.active
        assert updated.name == "Renamed"
        assert updated.mission.company_name == "LiveCo"  # untouched field preserved

        # get_brand_by_id resolves without the client id.
        assert store.get_brand_by_id(brand.id).id == brand.id

        # append_brand_version: server-side version bump + history append.
        output = TeamOutput(
            status=WorkflowStatus.READY_FOR_ROLLOUT,
            mission_summary="done",
            current_phase=BrandPhase.COMPLETE,
        )
        v1 = store.append_brand_version(client.id, brand.id, output)
        assert v1 is not None and v1.version == 1 and len(v1.history) == 1
        v2 = store.append_brand_version(client.id, brand.id, output)
        assert v2 is not None and v2.version == 2 and len(v2.history) == 2
        assert v2.history[-1].version == 2
        assert v2.current_phase == BrandPhase.COMPLETE
    finally:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM branding_brands WHERE id = %s", (brand.id,))
            cur.execute("DELETE FROM branding_clients WHERE id = %s", (client.id,))
