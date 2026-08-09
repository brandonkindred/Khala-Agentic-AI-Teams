"""Unit tests for Postgres draft-row helpers (no live DB required)."""

from __future__ import annotations

from agent_team_studio.agent_studio.drafts_pg_store import _row_to_draft


def test_row_to_draft_coerces_non_object_payload() -> None:
    draft = _row_to_draft(
        {
            "draft_id": "d1",
            "name": "n",
            "payload_json": ["not", "an", "object"],
            "created_at": "2026-08-07T12:00:00+00:00",
            "updated_at": "2026-08-07T12:00:00+00:00",
        }
    )
    assert draft.payload == {}

    draft_none = _row_to_draft(
        {
            "draft_id": "d2",
            "name": "n",
            "payload_json": None,
            "created_at": "2026-08-07T12:00:00+00:00",
            "updated_at": "2026-08-07T12:00:00+00:00",
        }
    )
    assert draft_none.payload == {}


def test_row_to_draft_deep_copies_object_payload() -> None:
    raw = {"nested": {"k": 1}}
    draft = _row_to_draft(
        {
            "draft_id": "d1",
            "name": "n",
            "payload_json": raw,
            "created_at": "2026-08-07T12:00:00+00:00",
            "updated_at": "2026-08-07T12:00:00+00:00",
        }
    )
    draft.payload["nested"]["k"] = 99
    assert raw["nested"]["k"] == 1
