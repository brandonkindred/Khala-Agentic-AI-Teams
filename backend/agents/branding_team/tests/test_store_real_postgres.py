"""Real-Postgres coverage for the branding store SQL.

The rest of the branding suite runs against the in-memory fake, so the
Postgres-specific SQL the store emits — ``jsonb ||`` merges, ``data->>'k'`` /
``data->'k'`` accessors, ``... RETURNING data``, the ``WITH conv AS (UPDATE…)
INSERT`` CTE, and ``LEFT JOIN`` loads — is never validated against a live
database there. These tests opt out of the fake (``real_postgres`` marker) and
exercise that SQL for real. They run in the live-Postgres integration job and
skip when POSTGRES_HOST is unset.
"""

from __future__ import annotations

import os
import uuid

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

pytestmark = [pytest.mark.integration, pytest.mark.real_postgres]


@pytest.fixture(scope="module", autouse=True)
def _branding_schema():
    if not os.environ.get("POSTGRES_HOST"):
        pytest.skip("real_postgres tests require POSTGRES_HOST")
    from shared.postgres import get_conn, register_team_schemas

    register_team_schemas(BRANDING_SCHEMA)
    yield
    # Don't leave test artifacts behind in a shared/CI database.
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "TRUNCATE branding_conv_messages, branding_conversations, "
            "branding_brands, branding_clients, branding_sessions"
        )


def test_brand_jsonb_roundtrip_real_postgres() -> None:
    """Brand CRUD + jsonb merge / version append / lookups work against real Postgres."""
    store = BrandingStore()
    client = store.create_client(f"RealPG {uuid.uuid4().hex[:8]}")
    brand = store.create_brand(
        client.id,
        make_mission(
            company_name="RealCo",
            company_description="A real company description long enough.",
            target_audience="developers",
        ),
    )
    assert brand is not None
    assert brand.version == 0

    # update_brand → `data = data || %s ... RETURNING data`
    updated = store.update_brand(client.id, brand.id, status=BrandStatus.active, name="Renamed")
    assert updated is not None
    assert updated.name == "Renamed"
    assert updated.status == BrandStatus.active

    # append_brand_version → SELECT data->>'version', data->'history' FOR UPDATE,
    # then `data || patch ... RETURNING data`.
    out = TeamOutput(
        status=WorkflowStatus.READY_FOR_ROLLOUT,
        mission_summary="done",
        current_phase=BrandPhase.COMPLETE,
    )
    v1 = store.append_brand_version(client.id, brand.id, out)
    assert v1 is not None and v1.version == 1 and len(v1.history) == 1
    v2 = store.append_brand_version(client.id, brand.id, out)
    assert v2 is not None and v2.version == 2 and len(v2.history) == 2

    reloaded = store.get_brand(client.id, brand.id)
    assert reloaded is not None
    assert reloaded.version == 2
    assert reloaded.current_phase == BrandPhase.COMPLETE

    # brand_exists / get_brand_by_id / get_brand_names (SELECT 1, single-row, ANY)
    assert store.brand_exists(brand.id) is True
    assert store.brand_exists(f"brand_{uuid.uuid4().hex[:12]}") is False
    found = store.get_brand_by_id(brand.id)
    assert found is not None and found[0] == client.id
    assert store.get_brand_names([brand.id]) == {brand.id: "Renamed"}


def test_conversation_sql_real_postgres() -> None:
    """Conversation CTE append / LEFT JOIN load / list aggregate work against real Postgres."""
    store = BrandingConversationStore()
    cid = store.create(
        mission=make_mission(
            company_name="ConvCo",
            company_description="A real company description long enough.",
            target_audience="developers",
        )
    )

    # append_message → data-modifying CTE
    assert store.append_message(cid, "user", "hello") is True
    assert store.append_message(cid, "assistant", "hi there") is True
    assert store.append_message(f"missing_{uuid.uuid4().hex}", "user", "x") is False

    # get_state → single LEFT JOIN load
    state = store.get_state(cid)
    assert state is not None
    assert [(m.role, m.content) for m in state.messages] == [
        ("user", "hello"),
        ("assistant", "hi there"),
    ]
    assert state.brand_id is None

    # list_conversations → LEFT JOIN + GROUP BY COUNT
    summaries = store.list_conversations()
    match = next((s for s in summaries if s.conversation_id == cid), None)
    assert match is not None
    assert match.message_count == 2


def test_attach_conversation_real_postgres() -> None:
    """attach_conversation's SELECT ... FOR UPDATE + cross-table transaction
    (branding_conversations + branding_brands) works against real Postgres."""
    conv_store = BrandingConversationStore()
    brand_store = BrandingStore()
    client = brand_store.create_client(f"RealPGAttach {uuid.uuid4().hex[:8]}")
    brand = brand_store.create_brand(
        client.id,
        make_mission(
            company_name="AttachRealCo",
            company_description="A real company description long enough.",
            target_audience="developers",
        ),
    )
    assert brand is not None
    cid = conv_store.create(
        mission=make_mission(
            company_name="AttachRealCo",
            company_description="A real company description long enough.",
            target_audience="developers",
        )
    )

    updated_mission = make_mission(
        company_name="AttachRealCo v2",
        company_description="A real, updated company description.",
        target_audience="operators",
    )
    result, updated_brand = brand_store.attach_conversation(client.id, brand.id, cid, updated_mission)
    assert result is AttachConversationResult.OK
    assert updated_brand is not None
    assert updated_brand.conversation_id == cid

    assert conv_store.get_conversation_brand_id(cid) == brand.id
    state = conv_store.get_state(cid)
    assert state is not None
    assert state.mission.company_name == "AttachRealCo v2"

    # Already-attached conflict: a second brand cannot claim the same conversation.
    other_brand = brand_store.create_brand(
        client.id,
        make_mission(
            company_name="OtherRealCo",
            company_description="A real company description long enough.",
            target_audience="developers",
        ),
    )
    assert other_brand is not None
    conflict_result, conflict_brand = brand_store.attach_conversation(
        client.id, other_brand.id, cid, updated_mission
    )
    assert conflict_result is AttachConversationResult.ALREADY_ATTACHED
    assert conflict_brand is None

    # Brand-not-found rolls back: a fresh, unattached conversation must not be
    # left pointing at a brand id that doesn't exist.
    orphan_cid = conv_store.create(mission=updated_mission)
    missing_result, missing_brand = brand_store.attach_conversation(
        client.id, "brand_does_not_exist", orphan_cid, updated_mission
    )
    assert missing_result is AttachConversationResult.BRAND_NOT_FOUND
    assert missing_brand is None
    assert conv_store.get_conversation_brand_id(orphan_cid) is None
