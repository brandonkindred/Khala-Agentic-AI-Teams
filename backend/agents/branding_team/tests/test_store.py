"""Tests for branding store (clients and brands)."""

from branding_team.models import BrandingMission, BrandStatus, TeamOutput, WorkflowStatus
from branding_team.store import BrandingStore


def test_create_client_and_list() -> None:
    store = BrandingStore()
    client = store.create_client("Acme Corp")
    assert client.id.startswith("client_")
    assert client.name == "Acme Corp"
    assert client.created_at
    clients = store.list_clients()
    assert len(clients) == 1
    assert store.get_client(client.id) == client


def test_create_brand_and_list() -> None:
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
    assert brand.mission.company_name == "Acme Inc"
    brands = store.list_brands_for_client(client.id)
    assert len(brands) == 1
    assert store.get_brand(client.id, brand.id) == brand


def test_get_brand_wrong_client_returns_none() -> None:
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


def test_update_brand() -> None:
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
    updated = store.update_brand(client.id, brand.id, mission=new_mission, status=BrandStatus.active)
    assert updated is not None
    assert updated.mission.company_description == "Updated description"
    assert updated.status == BrandStatus.active


def test_append_brand_version() -> None:
    from branding_team.models import BrandCodification, CreativeRefinementPlan, DesignSystemDefinition, WritingGuidelines

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
        codification=BrandCodification(
            positioning_statement="We help everyone",
            brand_promise="Quality",
            brand_personality_traits=[],
            narrative_pillars=[],
        ),
        creative_refinement=CreativeRefinementPlan(),
        writing_guidelines=WritingGuidelines(),
        design_system=DesignSystemDefinition(),
    )
    updated = store.append_brand_version(client.id, brand.id, output)
    assert updated is not None
    assert updated.version == 1
    assert len(updated.history) == 1
    assert updated.latest_output is not None
    assert updated.latest_output.mission_summary == "Done"


def test_create_brand_for_nonexistent_client_returns_none() -> None:
    store = BrandingStore()
    mission = BrandingMission(
        company_name="XY",
        company_description="Long enough description",
        target_audience="Everyone",
    )
    brand = store.create_brand("nonexistent_client_id", mission)
    assert brand is None
