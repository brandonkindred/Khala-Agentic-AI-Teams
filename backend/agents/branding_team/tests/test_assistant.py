"""Tests for BrandingAssistantAgent (mock LLM).

Imports from ``branding_team.assistant`` chain transitively touch the
real job service.  Marked integration pending follow-up to break that
import-time coupling.
"""

import json
from unittest.mock import MagicMock

import pytest

from branding_team.assistant.agent import (
    BrandingAssistantAgent,
    _merge_mission_update,
    _parse_extraction,
    _strip_accidental_json,
)
from branding_team.models import BrandingMission

pytestmark = [pytest.mark.integration]


def test_parse_extraction_returns_mission_and_suggestions() -> None:
    raw = json.dumps(
        {
            "mission_update": {
                "company_name": "Acme",
                "target_audience": "Developers",
            },
            "suggested_questions": ["What 3 values matter most?", "Who are your competitors?"],
        }
    )
    mission_update, suggestions = _parse_extraction(raw)
    assert mission_update == {"company_name": "Acme", "target_audience": "Developers"}
    assert suggestions == ["What 3 values matter most?", "Who are your competitors?"]


def test_parse_extraction_tolerates_markdown_fences() -> None:
    inner = json.dumps({"mission_update": {"company_name": "Acme"}, "suggested_questions": []})
    mission_update, suggestions = _parse_extraction(f"```json\n{inner}\n```")
    assert mission_update == {"company_name": "Acme"}
    assert suggestions == []


def test_parse_extraction_extracts_object_from_surrounding_prose() -> None:
    raw = 'Here is the JSON: {"mission_update": {"company_name": "Acme"}}'
    mission_update, suggestions = _parse_extraction(raw)
    assert mission_update == {"company_name": "Acme"}
    assert suggestions == []


def test_parse_extraction_returns_empty_on_garbage() -> None:
    mission_update, suggestions = _parse_extraction("nothing here")
    assert mission_update == {}
    assert suggestions == []


def test_strip_accidental_json_suppresses_bare_json_object() -> None:
    """Regression for the screenshot bug — the conversation agent must never leak
    raw mission JSON to the user. _strip_accidental_json blanks it so the caller
    substitutes a graceful fallback prose reply."""
    raw_json = '{"company_name": "Brandon Kindred", "company_description": ""}'
    assert _strip_accidental_json(raw_json) == ""


def test_strip_accidental_json_suppresses_three_field_mission_object() -> None:
    """Regression for the second-round failure: LLM emits the full 3-field object."""
    raw_json = (
        '{"company_name": "Brandon Kindred", "company_description": "", "target_audience": ""}'
    )
    assert _strip_accidental_json(raw_json) == ""


def test_strip_accidental_json_suppresses_json_with_surrounding_whitespace() -> None:
    """The LLM may pad the JSON object with newlines or whitespace."""
    raw = '\n\n  {"company_name": "Brandon Kindred"}  \n\n'
    assert _strip_accidental_json(raw) == ""


def test_strip_accidental_json_preserves_real_prose() -> None:
    prose = "Brandon Kindred — got it. To kick this off, what does your personal brand stand for?"
    assert _strip_accidental_json(prose) == prose


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


def test_branding_assistant_agent_two_stage_returns_natural_reply_and_extracts_mission() -> None:
    """End-to-end: conversation agent returns prose; extractor returns JSON;
    user-facing reply is the prose, mission is populated from the extractor."""
    conversation_llm = MagicMock(
        return_value=(
            "Brandon Kindred — got it. Personal brands live or die on a clear point of view. "
            "What's the work you want to be known for?"
        )
    )
    extraction_llm = MagicMock(
        return_value=json.dumps(
            {
                "mission_update": {"company_name": "Brandon Kindred"},
                "suggested_questions": [
                    "What do you want to be known for?",
                    "Who is your audience?",
                ],
            }
        )
    )
    agent = BrandingAssistantAgent(conversation_llm=conversation_llm, extraction_llm=extraction_llm)
    mission = BrandingMission(
        company_name="TBD", company_description="To be discussed.", target_audience="TBD"
    )
    reply, updated_mission, suggested_questions = agent.respond(
        messages=[("assistant", "Hi! What's your company or product name?")],
        current_mission=mission,
        user_message="Brandon Kindred. it is a personal brand",
    )

    assert "Brandon Kindred" in reply
    assert "{" not in reply and '"company_name"' not in reply
    assert updated_mission.company_name == "Brandon Kindred"
    assert suggested_questions == [
        "What do you want to be known for?",
        "Who is your audience?",
    ]
    conversation_llm.assert_called_once()
    extraction_llm.assert_called_once()


def test_branding_assistant_agent_suppresses_accidental_json_from_conversation_llm() -> None:
    """If the conversation LLM regresses and emits raw JSON, the user must NOT see it.
    A graceful prose fallback is shown, and the extractor still captures the mission."""
    conversation_llm = MagicMock(
        return_value='{"company_name": "Brandon Kindred", "company_description": ""}'
    )
    extraction_llm = MagicMock(
        return_value=json.dumps(
            {
                "mission_update": {"company_name": "Brandon Kindred"},
                "suggested_questions": ["What do you want to be known for?"],
            }
        )
    )
    agent = BrandingAssistantAgent(conversation_llm=conversation_llm, extraction_llm=extraction_llm)
    mission = BrandingMission(
        company_name="TBD", company_description="To be discussed.", target_audience="TBD"
    )
    reply, updated_mission, _ = agent.respond(
        messages=[],
        current_mission=mission,
        user_message="Brandon Kindred. it is a personal brand",
    )
    assert "{" not in reply and '"company_name"' not in reply
    assert reply.strip() != ""
    assert updated_mission.company_name == "Brandon Kindred"


def test_branding_assistant_agent_handles_extraction_failure_gracefully() -> None:
    """Extractor failure must not break the conversation reply or mutate mission."""
    conversation_llm = MagicMock(return_value="Got it — tell me more about your audience.")
    extraction_llm = MagicMock(side_effect=Exception("extractor down"))
    agent = BrandingAssistantAgent(conversation_llm=conversation_llm, extraction_llm=extraction_llm)
    mission = BrandingMission(
        company_name="Acme", company_description="Software company.", target_audience="TBD"
    )
    reply, updated_mission, suggested_questions = agent.respond(
        messages=[],
        current_mission=mission,
        user_message="we sell to dev teams",
    )
    assert "Got it" in reply
    assert updated_mission.company_name == "Acme"
    assert len(suggested_questions) >= 1


def test_branding_assistant_agent_handles_conversation_llm_failure() -> None:
    conversation_llm = MagicMock(side_effect=Exception("LLM unavailable"))
    extraction_llm = MagicMock()
    agent = BrandingAssistantAgent(conversation_llm=conversation_llm, extraction_llm=extraction_llm)
    mission = BrandingMission(
        company_name="TBD", company_description="To be discussed.", target_audience="TBD"
    )
    reply, updated_mission, suggested_questions = agent.respond(
        messages=[], current_mission=mission, user_message="Hello"
    )
    assert "help" in reply.lower() or "brand" in reply.lower()
    assert updated_mission.company_name == "TBD"
    assert len(suggested_questions) >= 1
    extraction_llm.assert_not_called()
