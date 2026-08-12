"""Unit tests for Agent Studio draft Pydantic models."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from agent_platform.studio.models import AgentStudioDraft, AgentStudioDraftSummary


def test_summary_requires_core_fields() -> None:
    summary = AgentStudioDraftSummary(
        draft_id="d1", name="My draft", updated_at="2026-08-07T12:00:00+00:00"
    )
    assert summary.draft_id == "d1"
    assert summary.name == "My draft"


def test_draft_defaults_payload_to_empty_dict() -> None:
    draft = AgentStudioDraft(
        draft_id="d1",
        name="n",
        created_at="2026-08-07T12:00:00+00:00",
        updated_at="2026-08-07T12:00:00+00:00",
    )
    assert draft.payload == {}


def test_draft_accepts_opaque_payload() -> None:
    draft = AgentStudioDraft(
        draft_id="d1",
        name="n",
        created_at="2026-08-07T12:00:00+00:00",
        updated_at="2026-08-07T12:00:00+00:00",
        payload={"registryAgentId": "a1", "stage1AgentDraft": {"mode": "new"}},
    )
    assert draft.payload["registryAgentId"] == "a1"


def test_summary_rejects_missing_draft_id() -> None:
    with pytest.raises(ValidationError):
        AgentStudioDraftSummary(name="n", updated_at="2026-08-07T12:00:00+00:00")
