"""Tests for BrandingAssistantAgent (mock LLM)."""

from unittest.mock import MagicMock

import pytest

from branding_team.assistant.agent import BrandingAssistantAgent, _merge_mission_update, _parse_mission_and_suggestions
from branding_team.models import BrandingMission


def test_parse_mission_and_suggestions_returns_reply_and_updates() -> None:
    response = """Great, got it. Acme it is.

```mission
{"company_name": "Acme", "company_description": "We build tools.", "target_audience": "Developers"}
```
```suggestions
["What are your top 3 values?", "Who are your competitors?"]
```"""
    reply, mission_update, suggestions = _parse_mission_and_suggestions(response)
    assert "Acme" in reply or "Great" in reply
    assert mission_update.get("company_name") == "Acme"
    assert mission_update.get("target_audience") == "Developers"
    assert len(suggestions) == 2
    assert "values" in suggestions[0] or "values" in suggestions[1].lower()


def test_parse_mission_and_suggestions_fallback_when_no_blocks() -> None:
    response = "Just a plain reply with no blocks."
    reply, mission_update, suggestions = _parse_mission_and_suggestions(response)
    assert reply == "Just a plain reply with no blocks."
    assert mission_update == {}
    assert len(suggestions) >= 1


def test_merge_mission_update() -> None:
    current = BrandingMission(
        company_name="TBD",
        company_description="To be discussed.",
        target_audience="TBD",
    )
    update = {"company_name": "Acme", "target_audience": "Developers"}
    merged = _merge_mission_update(current, update)
    assert merged.company_name == "Acme"
    assert merged.target_audience == "Developers"
    assert merged.company_description == "To be discussed."


def test_branding_assistant_agent_returns_reply_mission_suggestions() -> None:
    mock_llm = MagicMock()
    mock_llm.complete.return_value = """Thanks! I have Acme and your audience.

```mission
{"company_name": "Acme", "company_description": "Software company", "target_audience": "Engineers"}
```
```suggestions
["Add 3 brand values", "Refine voice"]
```"""
    agent = BrandingAssistantAgent(llm=mock_llm)
    mission = BrandingMission(
        company_name="TBD",
        company_description="To be discussed.",
        target_audience="TBD",
    )
    reply, updated_mission, suggested_questions = agent.respond(
        messages=[],
        current_mission=mission,
        user_message="We're Acme, we make software for engineers.",
    )
    assert reply
    assert updated_mission.company_name == "Acme"
    assert updated_mission.target_audience == "Engineers"
    assert len(suggested_questions) >= 1


def test_branding_assistant_agent_handles_llm_failure() -> None:
    mock_llm = MagicMock()
    mock_llm.complete.side_effect = Exception("LLM unavailable")
    agent = BrandingAssistantAgent(llm=mock_llm)
    mission = BrandingMission(
        company_name="TBD",
        company_description="To be discussed.",
        target_audience="TBD",
    )
    reply, updated_mission, suggested_questions = agent.respond(
        messages=[],
        current_mission=mission,
        user_message="Hello",
    )
    assert "help" in reply.lower() or "brand" in reply.lower()
    assert updated_mission.company_name == "TBD"
    assert len(suggested_questions) >= 1
