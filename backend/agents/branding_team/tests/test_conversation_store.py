"""Tests for BrandingConversationStore against live Postgres.

Uses unique brand_id values per test so runs stay safe under pytest-xdist
when ``real_postgres_schema`` skips truncation (worker processes share a
persistent DB and fixed brand ids would collide on
``idx_branding_conv_brand_unique``).
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from branding_team.assistant.store import BrandingConversationStore
from branding_team.models import BrandPhase, TeamOutput, WorkflowStatus
from branding_team.postgres import SCHEMA as BRANDING_SCHEMA
from branding_team.tests.conftest import make_mission
from shared.postgres.testing import real_postgres_schema

pytestmark = [pytest.mark.integration, pytest.mark.real_postgres]

_branding_schema = real_postgres_schema(BRANDING_SCHEMA, scope="function", autouse=True)


def _brand_id(label: str = "brand") -> str:
    """Mint a brand id unique across workers / repeated integration runs."""
    return f"{label}_{uuid4().hex}"


def _output(summary: str = "done") -> TeamOutput:
    return TeamOutput(
        status=WorkflowStatus.READY_FOR_ROLLOUT,
        mission_summary=summary,
        current_phase=BrandPhase.COMPLETE,
    )


def _acme_mission():
    return make_mission(
        company_name="Acme",
        company_description="A great company",
        target_audience="developers",
    )


def test_get_state_returns_none_for_unknown() -> None:
    """get_state / get return None for a conversation id that does not exist."""
    store = BrandingConversationStore()
    assert store.get_state("missing") is None
    assert store.get("missing") is None


def test_get_state_empty_conversation() -> None:
    """A freshly created conversation loads with no messages and no output."""
    store = BrandingConversationStore()
    cid = store.create(mission=_acme_mission())
    state = store.get_state(cid)
    assert state is not None
    assert state.messages == []
    assert state.brand_id is None
    assert state.latest_output is None
    assert state.mission.company_name == "Acme"


def test_get_state_includes_messages_and_brand_id() -> None:
    """get_state returns messages in order and the attached brand id; the legacy
    3-tuple view stays in sync."""
    store = BrandingConversationStore()
    brand_id = _brand_id("xyz")
    cid = store.create(brand_id=brand_id, mission=_acme_mission())
    assert store.append_message(cid, "user", "hello")
    assert store.append_message(cid, "assistant", "hi there")

    state = store.get_state(cid)
    assert state is not None
    assert state.brand_id == brand_id
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


def test_get_state_loads_non_none_latest_output() -> None:
    """get_state (and the legacy view) load a persisted latest_output in the
    single query — not just the None case."""
    store = BrandingConversationStore()
    cid = store.create(mission=_acme_mission())
    assert store.update_output(cid, _output("rollout ready")) is True

    state = store.get_state(cid)
    assert state is not None
    assert state.latest_output is not None
    assert state.latest_output.mission_summary == "rollout ready"
    assert state.latest_output.current_phase == BrandPhase.COMPLETE

    _, _, legacy_output = store.get(cid)
    assert legacy_output is not None
    assert legacy_output.mission_summary == "rollout ready"


def test_append_message_rejects_unknown_conversation_and_role() -> None:
    """append_message returns False for an unknown conversation or invalid role,
    and True (persisting) for a valid one."""
    store = BrandingConversationStore()
    cid = store.create(mission=_acme_mission())
    # Unknown conversation -> False, nothing inserted.
    assert store.append_message("missing", "user", "hi") is False
    # Invalid role -> False.
    assert store.append_message(cid, "system", "nope") is False
    # Valid -> True.
    assert store.append_message(cid, "user", "hello") is True
    state = store.get_state(cid)
    assert state is not None
    assert [(m.role, m.content) for m in state.messages] == [("user", "hello")]


def test_get_by_brand_id_single_join() -> None:
    """get_by_brand_id loads the brand's conversation, messages, and mission in
    one query; returns None for an unknown brand."""
    store = BrandingConversationStore()
    brand_id = _brand_id("join")
    cid = store.create(brand_id=brand_id, mission=_acme_mission())
    store.append_message(cid, "user", "one")
    store.append_message(cid, "assistant", "two")

    result = store.get_by_brand_id(brand_id)
    assert result is not None
    rcid, messages, mission, latest_output = result
    assert rcid == cid
    assert [m.content for m in messages] == ["one", "two"]
    assert mission.company_name == "Acme"
    assert latest_output is None

    assert store.get_by_brand_id(_brand_id("absent")) is None


def test_get_by_brand_id_ignores_stale_conversation_for_same_brand() -> None:
    """When more than one conversation row exists for a brand, get_by_brand_id
    must return only the most-recently-updated conversation's own messages —
    never a merge of messages from multiple conversations."""
    from datetime import datetime, timezone

    from psycopg.types.json import Json

    from shared.postgres.client import get_conn

    store = BrandingConversationStore()
    brand_id = _brand_id("dup")
    stale_cid = store.create(brand_id=brand_id, mission=_acme_mission())
    store.append_message(stale_cid, "user", "stale one")
    store.append_message(stale_cid, "assistant", "stale two")

    fresh_cid = str(uuid4())
    ts_old = datetime(2020, 1, 1, tzinfo=timezone.utc)
    ts_new = datetime(2026, 1, 1, tzinfo=timezone.utc)
    mission_json = Json(_acme_mission().model_dump(mode="json"))

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE branding_conversations SET updated_at = %s WHERE conversation_id = %s",
                (ts_old, stale_cid),
            )
            cur.execute("DROP INDEX IF EXISTS idx_branding_conv_brand_unique")
            cur.execute(
                "INSERT INTO branding_conversations "
                "(conversation_id, brand_id, mission_json, latest_output_json, created_at, updated_at) "
                "VALUES (%s, %s, %s, NULL, %s, %s)",
                (fresh_cid, brand_id, mission_json, ts_new, ts_new),
            )
        conn.commit()

    try:
        store.append_message(fresh_cid, "user", "fresh one")

        result = store.get_by_brand_id(brand_id)
        assert result is not None
        rcid, messages, _, _ = result
        assert rcid == fresh_cid
        assert [m.content for m in messages] == ["fresh one"]
    finally:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE branding_conversations SET brand_id = NULL WHERE conversation_id = %s",
                    (stale_cid,),
                )
                cur.execute(
                    """CREATE UNIQUE INDEX IF NOT EXISTS idx_branding_conv_brand_unique
                       ON branding_conversations(brand_id) WHERE brand_id IS NOT NULL"""
                )
            conn.commit()


def test_get_by_brand_id_loads_non_none_latest_output() -> None:
    """get_by_brand_id surfaces a persisted latest_output, covering the
    non-None branch of the single-query load."""
    store = BrandingConversationStore()
    brand_id = _brand_id("out")
    cid = store.create(brand_id=brand_id, mission=_acme_mission())
    assert store.update_output(cid, _output("live")) is True

    result = store.get_by_brand_id(brand_id)
    assert result is not None
    rcid, _, _, latest_output = result
    assert rcid == cid
    assert latest_output is not None
    assert latest_output.mission_summary == "live"


def test_get_conversation_brand_id_dict_row() -> None:
    """get_conversation_brand_id reads brand_id via dict_row (string key).

    Preconditions:
        Conversations may or may not exist / have a brand.
    Postconditions:
        Returns the brand id string when set, else None — never raises on row shape.
    """
    store = BrandingConversationStore()
    assert store.get_conversation_brand_id("missing") is None

    unbound = store.create(mission=_acme_mission())
    assert store.get_conversation_brand_id(unbound) is None

    brand_id = _brand_id("abc")
    bound = store.create(brand_id=brand_id, mission=_acme_mission())
    assert store.get_conversation_brand_id(bound) == brand_id


def test_update_mission_and_set_brand() -> None:
    """update_mission and set_brand persist and return False for unknown ids."""
    store = BrandingConversationStore()
    cid = store.create(mission=_acme_mission())

    updated = make_mission(
        company_name="Beta",
        company_description="Updated description",
        target_audience="operators",
    )
    assert store.update_mission(cid, updated) is True
    assert store.update_mission("missing", updated) is False

    state = store.get_state(cid)
    assert state is not None
    assert state.mission.company_name == "Beta"

    brand_id = _brand_id("set")
    assert store.set_brand(cid, brand_id) is True
    assert store.set_brand("missing", brand_id) is False
    assert store.get_conversation_brand_id(cid) == brand_id

    assert store.set_brand(cid, None) is True
    assert store.get_conversation_brand_id(cid) is None


def test_attach_and_update_mission() -> None:
    """attach_and_update_mission sets brand_id and mission in one call, and
    returns False for an unknown conversation without raising."""
    store = BrandingConversationStore()
    cid = store.create(mission=_acme_mission())

    updated = make_mission(
        company_name="Beta",
        company_description="Updated description",
        target_audience="operators",
    )
    brand_id = _brand_id("atomic")
    assert store.attach_and_update_mission(cid, brand_id, updated) is True
    assert store.get_conversation_brand_id(cid) == brand_id
    state = store.get_state(cid)
    assert state is not None
    assert state.mission.company_name == "Beta"

    assert store.attach_and_update_mission("missing", brand_id, updated) is False


def test_list_conversations_with_and_without_brand_filter() -> None:
    """list_conversations returns summaries, optionally filtered by brand_id."""
    store = BrandingConversationStore()
    brand_a = _brand_id("a")
    brand_b = _brand_id("b")
    a = store.create(brand_id=brand_a, mission=_acme_mission())
    b = store.create(brand_id=brand_b, mission=_acme_mission())
    store.create(mission=_acme_mission())
    store.append_message(a, "user", "hello")

    all_rows = store.list_conversations()
    assert {r.conversation_id for r in all_rows} >= {a, b}
    by_a = store.list_conversations(brand_id=brand_a)
    assert len(by_a) == 1
    assert by_a[0].conversation_id == a
    assert by_a[0].brand_id == brand_a
    assert by_a[0].message_count == 1
    assert store.list_conversations(brand_id=_brand_id("absent")) == []
