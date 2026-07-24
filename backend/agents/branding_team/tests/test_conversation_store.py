"""Unit tests for the branding conversation store (dict-backed fake Postgres).

Focuses on the single-query ``get_state`` / ``get_by_brand_id`` loaders and the
backward-compatible ``get`` view.
"""

from __future__ import annotations

import pytest

from branding_team.assistant.store import BrandingConversationStore
from branding_team.models import BrandPhase, TeamOutput, WorkflowStatus
from branding_team.tests._fake_postgres import install_fake_postgres
from branding_team.tests.conftest import make_mission


@pytest.fixture
def fake_pg(monkeypatch: pytest.MonkeyPatch) -> dict:
    return install_fake_postgres(monkeypatch)


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


def test_get_state_returns_none_for_unknown(fake_pg: dict) -> None:
    """get_state / get return None for a conversation id that does not exist."""
    store = BrandingConversationStore()
    assert store.get_state("missing") is None
    assert store.get("missing") is None


def test_get_state_empty_conversation(fake_pg: dict) -> None:
    """A freshly created conversation loads with no messages and no output."""
    store = BrandingConversationStore()
    cid = store.create(mission=_acme_mission())
    state = store.get_state(cid)
    assert state is not None
    assert state.messages == []
    assert state.brand_id is None
    assert state.latest_output is None
    assert state.mission.company_name == "Acme"


def test_get_state_includes_messages_and_brand_id(fake_pg: dict) -> None:
    """get_state returns messages in order and the attached brand id; the legacy
    3-tuple view stays in sync."""
    store = BrandingConversationStore()
    cid = store.create(brand_id="brand_xyz", mission=_acme_mission())
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


def test_get_state_loads_non_none_latest_output(fake_pg: dict) -> None:
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


def test_append_message_rejects_unknown_conversation_and_role(fake_pg: dict) -> None:
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


def test_get_by_brand_id_single_join(fake_pg: dict) -> None:
    """get_by_brand_id loads the brand's conversation, messages, and mission in
    one query; returns None for an unknown brand."""
    store = BrandingConversationStore()
    cid = store.create(brand_id="brand_join", mission=_acme_mission())
    store.append_message(cid, "user", "one")
    store.append_message(cid, "assistant", "two")

    result = store.get_by_brand_id("brand_join")
    assert result is not None
    rcid, messages, mission, latest_output = result
    assert rcid == cid
    assert [m.content for m in messages] == ["one", "two"]
    assert mission.company_name == "Acme"
    assert latest_output is None

    assert store.get_by_brand_id("brand_absent") is None


def test_get_by_brand_id_loads_non_none_latest_output(fake_pg: dict) -> None:
    """get_by_brand_id surfaces a persisted latest_output, covering the
    non-None branch of the single-query load."""
    store = BrandingConversationStore()
    cid = store.create(brand_id="brand_out", mission=_acme_mission())
    assert store.update_output(cid, _output("live")) is True

    result = store.get_by_brand_id("brand_out")
    assert result is not None
    rcid, _, _, latest_output = result
    assert rcid == cid
    assert latest_output is not None
    assert latest_output.mission_summary == "live"


def test_get_conversation_brand_id_dict_row(fake_pg: dict) -> None:
    """get_conversation_brand_id reads brand_id via dict_row (string key).

    Preconditions:
        Fake Postgres is installed; conversations may or may not exist / have a brand.
    Postconditions:
        Returns the brand id string when set, else None — never raises on row shape.
    """
    store = BrandingConversationStore()
    assert store.get_conversation_brand_id("missing") is None

    unbound = store.create(mission=_acme_mission())
    assert store.get_conversation_brand_id(unbound) is None

    bound = store.create(brand_id="brand_abc", mission=_acme_mission())
    assert store.get_conversation_brand_id(bound) == "brand_abc"


def test_update_mission_and_set_brand(fake_pg: dict) -> None:
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

    assert store.set_brand(cid, "brand_set") is True
    assert store.set_brand("missing", "brand_set") is False
    assert store.get_conversation_brand_id(cid) == "brand_set"

    assert store.set_brand(cid, None) is True
    assert store.get_conversation_brand_id(cid) is None


def test_list_conversations_with_and_without_brand_filter(fake_pg: dict) -> None:
    """list_conversations returns summaries, optionally filtered by brand_id."""
    store = BrandingConversationStore()
    a = store.create(brand_id="brand_a", mission=_acme_mission())
    b = store.create(brand_id="brand_b", mission=_acme_mission())
    store.create(mission=_acme_mission())
    store.append_message(a, "user", "hello")

    all_rows = store.list_conversations()
    assert {r.conversation_id for r in all_rows} >= {a, b}
    by_a = store.list_conversations(brand_id="brand_a")
    assert len(by_a) == 1
    assert by_a[0].conversation_id == a
    assert by_a[0].brand_id == "brand_a"
    assert by_a[0].message_count == 1
    assert store.list_conversations(brand_id="brand_absent") == []


def test_list_conversations_matcher_rejects_loose_fragments(fake_pg: dict) -> None:
    """Loose FROM/ORDER fragments alone must not dispatch as list_conversations."""
    from branding_team._db import get_conn

    decoy = """
        SELECT c.conversation_id
        FROM branding_conversations c
        ORDER BY c.updated_at DESC
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            with pytest.raises(AssertionError, match="unexpected SQL"):
                cur.execute(decoy)
