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
    BrandingMission,
    BrandPhase,
    BrandStatus,
    TeamOutput,
    WorkflowStatus,
)
from branding_team.postgres import SCHEMA as BRANDING_SCHEMA
from branding_team.store import BrandingStore

pytestmark = [pytest.mark.integration, pytest.mark.real_postgres]


@pytest.fixture(scope="module", autouse=True)
def _branding_schema() -> None:
    if not os.environ.get("POSTGRES_HOST"):
        pytest.skip("real_postgres tests require POSTGRES_HOST")
    from shared_postgres import register_team_schemas

    register_team_schemas(BRANDING_SCHEMA)


def _mission(name: str) -> BrandingMission:
    return BrandingMission(
        company_name=name,
        company_description="A real company description long enough.",
        target_audience="developers",
    )


def test_brand_jsonb_roundtrip_real_postgres() -> None:
    store = BrandingStore()
    client = store.create_client(f"RealPG {uuid.uuid4().hex[:8]}")
    brand = store.create_brand(client.id, _mission("RealCo"))
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
    store = BrandingConversationStore()
    cid = store.create(mission=_mission("ConvCo"))

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
