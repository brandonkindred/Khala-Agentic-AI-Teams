"""Distinct real-Postgres check for BrandingConversationStore SQL.

``test_store.py`` now runs ``BrandingStore`` (and attach_conversation) against
live Postgres via ``real_postgres_schema``. This module keeps the conversation
append / LEFT JOIN load / list-aggregate coverage that ``test_store.py`` does
not exercise — retained until the conversation-store migration in the
follow-up that retires ``_fake_postgres.py``.
"""

from __future__ import annotations

import uuid

import pytest

from branding_team.assistant.store import BrandingConversationStore
from branding_team.postgres import SCHEMA as BRANDING_SCHEMA
from branding_team.tests.conftest import make_mission
from shared.postgres.testing import real_postgres_schema

pytestmark = [pytest.mark.integration, pytest.mark.real_postgres]

_branding_schema = real_postgres_schema(BRANDING_SCHEMA)


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
