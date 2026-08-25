"""Tests for branding store (clients and brands).

Runs against live Postgres via ``shared.postgres.testing.real_postgres_schema``
with ``scope="function"`` (truncate before and after each test when not under
pytest-xdist). Skips when ``POSTGRES_HOST`` is unset.

These tests assert global table counts and are intended for the branding CI
job / plain pytest (no ``-n``). Under xdist, truncate is skipped by the
shared fixture — use unique row identifiers instead of this module.
"""

from __future__ import annotations

import pytest

from branding_team.assistant.store import BrandingConversationStore
from branding_team.models import (
    BrandPhase,
    BrandStatus,
    TeamOutput,
    WorkflowStatus,
)
from branding_team.postgres import SCHEMA as BRANDING_SCHEMA
from branding_team.store import AttachConversationResult, BrandingStore
from branding_team.tests.conftest import make_mission
from shared.postgres.testing import real_postgres_schema

pytestmark = [pytest.mark.integration, pytest.mark.real_postgres]

_branding_schema = real_postgres_schema(BRANDING_SCHEMA, scope="function", autouse=True)


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
    mission = make_mission(
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


def test_create_brand_records_profile_association(monkeypatch: pytest.MonkeyPatch) -> None:
    """create_brand links the new brand to the default user profile (best-effort)."""
    import branding_team.store as store_mod
    from user_profile import ArtifactType

    calls: list = []
    monkeypatch.setattr(store_mod, "record_association_safe", lambda *a, **k: calls.append((a, k)))

    store = BrandingStore()
    client = store.create_client("Acme")
    mission = make_mission(
        company_name="Acme Inc",
        company_description="A great company",
        target_audience="everyone",
    )
    brand = store.create_brand(client.id, mission, name="Acme Brand")

    assert calls == [((ArtifactType.BRAND, "branding", brand.id), {"label": brand.name})]


def test_get_brand_wrong_client_returns_none() -> None:
    store = BrandingStore()
    c1 = store.create_client("C1")
    c2 = store.create_client("C2")
    mission = make_mission(
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
    mission = make_mission(
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


def test_update_brand_mission_clears_latest_output_and_resets_phase() -> None:
    """A mission edit invalidates prior output, per update_brand's documented contract."""
    store = BrandingStore()
    client = store.create_client("Acme")
    mission = make_mission(
        company_name="Acme Inc",
        company_description="A great company",
        target_audience="everyone",
    )
    brand = store.create_brand(client.id, mission)
    assert brand is not None
    output = TeamOutput(
        status=WorkflowStatus.READY_FOR_ROLLOUT,
        mission_summary="Done",
        current_phase=BrandPhase.COMPLETE,
    )
    with_output = store.append_brand_version(client.id, brand.id, output)
    assert with_output is not None
    assert with_output.latest_output is not None
    assert with_output.current_phase == BrandPhase.COMPLETE

    new_mission = mission.model_copy(update={"company_description": "Updated description"})
    updated = store.update_brand(client.id, brand.id, mission=new_mission)

    assert updated is not None
    assert updated.latest_output is None
    assert updated.current_phase == BrandPhase.STRATEGIC_CORE


def test_update_brand_without_mission_preserves_latest_output() -> None:
    """Patching unrelated fields must not trigger the mission-edit invalidation."""
    store = BrandingStore()
    client = store.create_client("Acme")
    mission = make_mission(
        company_name="Acme Inc",
        company_description="A great company",
        target_audience="everyone",
    )
    brand = store.create_brand(client.id, mission)
    assert brand is not None
    output = TeamOutput(
        status=WorkflowStatus.READY_FOR_ROLLOUT,
        mission_summary="Done",
        current_phase=BrandPhase.COMPLETE,
    )
    store.append_brand_version(client.id, brand.id, output)

    updated = store.update_brand(client.id, brand.id, status=BrandStatus.active)

    assert updated is not None
    assert updated.status == BrandStatus.active
    assert updated.latest_output is not None
    assert updated.current_phase == BrandPhase.COMPLETE


def test_append_brand_version() -> None:
    store = BrandingStore()
    client = store.create_client("Acme")
    mission = make_mission(
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


def test_append_brand_version_persists_current_phase() -> None:
    """Verify that current_phase on the brand record is updated from the output."""
    store = BrandingStore()
    client = store.create_client("PhaseTest")
    mission = make_mission(
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


def test_create_brand_for_nonexistent_client_returns_none() -> None:
    store = BrandingStore()
    mission = make_mission(
        company_name="XY",
        company_description="Long enough description",
        target_audience="Everyone",
    )
    brand = store.create_brand("nonexistent_client_id", mission)
    assert brand is None


def test_delete_brand_removes_brand_and_association(monkeypatch: pytest.MonkeyPatch) -> None:
    import branding_team.store as store_mod
    from user_profile import ArtifactType

    calls: list = []
    monkeypatch.setattr(store_mod, "remove_association_safe", lambda *a, **k: calls.append((a, k)))

    store = BrandingStore()
    client = store.create_client("Acme")
    mission = make_mission(
        company_name="Acme Inc",
        company_description="A great company",
        target_audience="everyone",
    )
    brand = store.create_brand(client.id, mission, name="Acme Brand")
    assert brand is not None

    assert store.delete_brand(client.id, brand.id) is True
    assert store.get_brand(client.id, brand.id) is None
    assert store.list_brands_for_client(client.id) == []
    assert calls == [((ArtifactType.BRAND, brand.id), {})]


def test_delete_brand_unknown_id_is_noop() -> None:
    store = BrandingStore()
    client = store.create_client("Acme")
    assert store.delete_brand(client.id, "brand_does_not_exist") is False


def test_delete_brand_wrong_client_is_noop() -> None:
    """delete_brand is scoped to client_id — another client can't delete it."""
    store = BrandingStore()
    owner = store.create_client("Owner")
    other = store.create_client("Other")
    mission = make_mission(
        company_name="Acme Inc",
        company_description="A great company",
        target_audience="everyone",
    )
    brand = store.create_brand(owner.id, mission)
    assert brand is not None

    assert store.delete_brand(other.id, brand.id) is False
    assert store.get_brand(owner.id, brand.id) is not None


def test_brand_exists() -> None:
    """brand_exists is True for an existing brand and False for an unknown id."""
    store = BrandingStore()
    client = store.create_client("Acme")
    brand = store.create_brand(
        client.id,
        make_mission(
            company_name="Acme Inc",
            company_description="A great company",
            target_audience="everyone",
        ),
    )
    assert brand is not None
    assert store.brand_exists(brand.id) is True
    assert store.brand_exists("brand_does_not_exist") is False


def test_get_brand_by_id_resolves_client() -> None:
    """get_brand_by_id returns the owning client id + brand, or None if absent."""
    store = BrandingStore()
    client = store.create_client("Acme")
    brand = store.create_brand(
        client.id,
        make_mission(
            company_name="Acme Inc",
            company_description="A great company",
            target_audience="everyone",
        ),
    )
    assert brand is not None
    found = store.get_brand_by_id(brand.id)
    assert found is not None
    resolved_client_id, resolved_brand = found
    assert resolved_client_id == client.id
    assert resolved_brand.id == brand.id
    assert store.get_brand_by_id("brand_missing") is None


def test_get_brand_names_returns_only_requested() -> None:
    """get_brand_names maps only the requested existing ids; empty input is a no-op."""
    store = BrandingStore()
    client = store.create_client("Acme")
    b1 = store.create_brand(
        client.id,
        make_mission(
            company_name="Acme Inc",
            company_description="A great company",
            target_audience="everyone",
        ),
        name="First",
    )
    b2 = store.create_brand(
        client.id,
        make_mission(
            company_name="Acme Inc",
            company_description="A great company",
            target_audience="everyone",
        ),
        name="Second",
    )
    assert b1 is not None and b2 is not None

    names = store.get_brand_names([b1.id, "brand_missing"])
    assert names == {b1.id: "First"}
    assert b2.id not in names

    # Empty / falsy input issues no query and returns an empty map.
    assert store.get_brand_names([]) == {}
    assert store.get_brand_names([""]) == {}


def test_list_clients_pagination() -> None:
    """list_clients limit/offset returns non-overlapping pages within the set."""
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


def test_pagination_rejects_invalid_args() -> None:
    """Both list methods reject non-positive limits and negative offsets."""
    store = BrandingStore()
    for bad in (dict(limit=0), dict(limit=-1), dict(offset=-1)):
        with pytest.raises(ValueError):
            store.list_clients(**bad)
        with pytest.raises(ValueError):
            store.list_brands_for_client("client_x", **bad)


def test_list_brands_for_client_pagination() -> None:
    """list_brands_for_client honors limit/offset for a client's brands."""
    store = BrandingStore()
    client = store.create_client("Acme")
    for _ in range(3):
        assert (
            store.create_brand(
                client.id,
                make_mission(
                    company_name="Acme Inc",
                    company_description="A great company",
                    target_audience="everyone",
                ),
            )
            is not None
        )
    assert len(store.list_brands_for_client(client.id)) == 3
    page = store.list_brands_for_client(client.id, limit=1, offset=1)
    assert len(page) == 1


def test_attach_conversation_success() -> None:
    """attach_conversation atomically links an unattached conversation and the
    brand's conversation_id, updating both rows in one call."""
    store = BrandingStore()
    conv_store = BrandingConversationStore()
    client = store.create_client("Acme")
    brand = store.create_brand(
        client.id,
        make_mission(
            company_name="Acme Inc",
            company_description="A great company",
            target_audience="everyone",
        ),
    )
    assert brand is not None
    cid = conv_store.create(mission=make_mission(company_name="Acme Inc"))

    updated_mission = make_mission(
        company_name="Acme Rebrand",
        company_description="Updated description",
        target_audience="developers",
    )
    result, updated_brand = store.attach_conversation(client.id, brand.id, cid, updated_mission)
    assert result is AttachConversationResult.OK
    assert updated_brand is not None
    assert updated_brand.conversation_id == cid

    assert conv_store.get_conversation_brand_id(cid) == brand.id
    state = conv_store.get_state(cid)
    assert state is not None
    assert state.mission.company_name == "Acme Rebrand"


def test_attach_conversation_unknown_conversation() -> None:
    """attach_conversation reports CONVERSATION_NOT_FOUND without touching the brand."""
    store = BrandingStore()
    client = store.create_client("Acme")
    brand = store.create_brand(client.id, make_mission(company_name="Acme Inc"))
    assert brand is not None

    result, updated_brand = store.attach_conversation(
        client.id, brand.id, "missing-conv", make_mission(company_name="Acme Inc")
    )
    assert result is AttachConversationResult.CONVERSATION_NOT_FOUND
    assert updated_brand is None
    assert store.get_brand(client.id, brand.id).conversation_id is None


def test_attach_conversation_already_attached() -> None:
    """attach_conversation reports ALREADY_ATTACHED and leaves both rows unchanged
    when the conversation is already linked to a different brand."""
    store = BrandingStore()
    conv_store = BrandingConversationStore()
    client = store.create_client("Acme")
    other_brand = store.create_brand(client.id, make_mission(company_name="Other Co"))
    target_brand = store.create_brand(client.id, make_mission(company_name="Target Co"))
    assert other_brand is not None and target_brand is not None
    cid = conv_store.create(brand_id=other_brand.id, mission=make_mission(company_name="Other Co"))

    result, updated_brand = store.attach_conversation(
        client.id, target_brand.id, cid, make_mission(company_name="Other Co")
    )
    assert result is AttachConversationResult.ALREADY_ATTACHED
    assert updated_brand is None
    assert conv_store.get_conversation_brand_id(cid) == other_brand.id
    assert store.get_brand(client.id, target_brand.id).conversation_id is None


def test_attach_conversation_reattaching_same_brand_is_ok() -> None:
    """Re-attaching a conversation to the brand it's already on is allowed
    (not a conflict). The conversation's own mission is updated to the new
    value, but the brand's ``mission`` field is left unchanged — only
    ``patch_brand`` refreshes a brand's mission."""
    store = BrandingStore()
    conv_store = BrandingConversationStore()
    client = store.create_client("Acme")
    brand = store.create_brand(client.id, make_mission(company_name="Acme Inc"))
    assert brand is not None
    cid = conv_store.create(brand_id=brand.id, mission=make_mission(company_name="Acme Inc"))

    result, updated_brand = store.attach_conversation(
        client.id, brand.id, cid, make_mission(company_name="Acme Inc v2")
    )
    assert result is AttachConversationResult.OK
    assert updated_brand is not None
    assert updated_brand.conversation_id == cid
    assert updated_brand.mission.company_name == "Acme Inc"


def test_attach_conversation_without_mission_preserves_existing_mission() -> None:
    """Calling attach_conversation with mission=None (the attach_conversation_to_brand
    endpoint's path) must not clobber the conversation's current mission with a
    caller's pre-lock snapshot — it leaves mission_json exactly as this same
    locked transaction just read it."""
    store = BrandingStore()
    conv_store = BrandingConversationStore()
    client = store.create_client("Acme")
    brand = store.create_brand(client.id, make_mission(company_name="Acme Inc"))
    assert brand is not None
    cid = conv_store.create(mission=make_mission(company_name="Acme Live Mission"))

    result, updated_brand = store.attach_conversation(client.id, brand.id, cid)
    assert result is AttachConversationResult.OK
    assert updated_brand is not None
    assert updated_brand.conversation_id == cid

    state = conv_store.get_state(cid)
    assert state is not None
    assert state.mission.company_name == "Acme Live Mission"


def test_attach_conversation_unknown_brand() -> None:
    """attach_conversation reports BRAND_NOT_FOUND when the brand row doesn't
    exist for the given client, and rolls back the conversation write so the
    conversation is not left pointing at a missing brand id.
    """
    store = BrandingStore()
    conv_store = BrandingConversationStore()
    client = store.create_client("Acme")
    cid = conv_store.create(mission=make_mission(company_name="Acme Inc"))

    result, updated_brand = store.attach_conversation(
        client.id, "brand_missing", cid, make_mission(company_name="Acme Inc")
    )
    assert result is AttachConversationResult.BRAND_NOT_FOUND
    assert updated_brand is None
    assert conv_store.get_conversation_brand_id(cid) is None


# ---------------------------------------------------------------------------
# Documented preconditions are enforced, not just documented
# ---------------------------------------------------------------------------


def test_get_client_rejects_empty_client_id() -> None:
    store = BrandingStore()
    with pytest.raises(ValueError, match="client_id"):
        store.get_client("")


def test_create_client_rejects_empty_name() -> None:
    store = BrandingStore()
    with pytest.raises(ValueError, match="name"):
        store.create_client("")


def test_get_brand_rejects_empty_ids() -> None:
    store = BrandingStore()
    with pytest.raises(ValueError, match="client_id"):
        store.get_brand("", "brand_x")
    with pytest.raises(ValueError, match="brand_id"):
        store.get_brand("client_x", "")


def test_create_brand_rejects_empty_client_id_and_bad_mission_type() -> None:
    store = BrandingStore()
    mission = make_mission(company_name="Acme Inc")
    with pytest.raises(ValueError, match="client_id"):
        store.create_brand("", mission)
    with pytest.raises(ValueError, match="mission"):
        store.create_brand("client_x", {"company_name": "Acme"})


def test_update_brand_rejects_empty_ids_and_bad_types() -> None:
    store = BrandingStore()
    client = store.create_client("Acme")
    brand = store.create_brand(client.id, make_mission(company_name="Acme Inc"))
    assert brand is not None
    with pytest.raises(ValueError, match="client_id"):
        store.update_brand("", brand.id)
    with pytest.raises(ValueError, match="brand_id"):
        store.update_brand(client.id, "")
    with pytest.raises(ValueError, match="mission"):
        store.update_brand(client.id, brand.id, mission={"company_name": "Acme"})
    with pytest.raises(ValueError, match="status"):
        store.update_brand(client.id, brand.id, status="draft")


def test_attach_conversation_rejects_empty_ids_and_bad_mission_type() -> None:
    store = BrandingStore()
    client = store.create_client("Acme")
    brand = store.create_brand(client.id, make_mission(company_name="Acme Inc"))
    assert brand is not None
    mission = make_mission(company_name="Acme Inc")
    with pytest.raises(ValueError, match="client_id"):
        store.attach_conversation("", brand.id, "conv_x", mission)
    with pytest.raises(ValueError, match="brand_id"):
        store.attach_conversation(client.id, "", "conv_x", mission)
    with pytest.raises(ValueError, match="conversation_id"):
        store.attach_conversation(client.id, brand.id, "", mission)
    with pytest.raises(ValueError, match="mission"):
        store.attach_conversation(client.id, brand.id, "conv_x", {"company_name": "Acme"})
