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


def test_strip_accidental_json_suppresses_nested_mission_with_color_palettes() -> None:
    """The exact reviewer scenario: outer mission JSON contains a nested
    structure (color_palettes → list of objects). The flat-regex guard
    would skip the outer block and only match the inner palette object,
    which has no mission keys, so the leak would slip through. Brace-
    balanced scanning must catch the outer block."""
    raw_json = json.dumps(
        {
            "company_name": "Brandon Kindred",
            "color_palettes": [
                {"name": "warm", "colors": ["#aa3300", "#ffcc99"]},
                {"name": "cool", "colors": ["#003366", "#99ccff"]},
            ],
        }
    )
    assert _strip_accidental_json(raw_json) == ""


def test_strip_accidental_json_suppresses_nested_mission_embedded_in_prose() -> None:
    """Same shape as above, but wrapped in conversational prose."""
    raw = (
        'Here is a draft: {"company_name": "Brandon", '
        '"color_palettes": [{"name": "warm", "colors": ["#a30"]}]} '
        "let me know what you think."
    )
    assert _strip_accidental_json(raw) == ""


def test_strip_accidental_json_suppresses_mission_wrapped_one_level_deep() -> None:
    """LLM occasionally wraps the mission under an outer key (e.g. ``mission``).
    The recursive mission-field check must still catch it."""
    raw = '{"mission": {"company_name": "X", "target_audience": "devs"}}'
    assert _strip_accidental_json(raw) == ""


def test_strip_accidental_json_preserves_prose_with_unrelated_nested_json() -> None:
    """A JSON object embedded in prose that does NOT carry any mission keys
    (even nested) must pass through unchanged — the guard is mission-
    specific and must not over-suppress general prose with code examples."""
    prose = (
        "For example, the analytics payload looks like "
        '{"event": "click", "metadata": {"button": "cta"}} — nothing mission-related there.'
    )
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


def test_branding_assistant_agent_legacy_llm_kwarg_drives_both_stages() -> None:
    """Backward-compat: the legacy ``llm=`` kwarg must route to BOTH stages.

    Regression: a previous iteration only assigned ``llm`` to the conversation
    stage, leaving extraction to construct a real Strands ``Agent`` against
    the live LLM service. Offline / unit-test callers that injected a fake
    via ``llm=`` then hit a real network call on extraction. The fake must
    drive both stages so tests stay hermetic.
    """
    fake_llm = MagicMock()

    def _side_effect(prompt: str) -> str:
        # Conversation stage gets the chatty prompt template; extractor gets
        # the EXTRACTION_USER_TEMPLATE which references "Strategist's reply".
        if "Strategist's reply" in prompt:
            return json.dumps(
                {
                    "mission_update": {"company_name": "Acme"},
                    "suggested_questions": ["What does Acme do?"],
                }
            )
        return "Acme — got it. What's the work you want to be known for?"

    fake_llm.side_effect = _side_effect
    agent = BrandingAssistantAgent(llm=fake_llm)
    mission = BrandingMission(
        company_name="TBD", company_description="To be discussed.", target_audience="TBD"
    )

    reply, updated_mission, suggested_questions = agent.respond(
        messages=[], current_mission=mission, user_message="We're Acme"
    )

    # Both stages used the injected fake — no real Strands Agent constructed.
    assert fake_llm.call_count == 2
    assert "Acme" in reply
    assert updated_mission.company_name == "Acme"
    assert suggested_questions == ["What does Acme do?"]


def test_branding_assistant_agent_explicit_kwargs_override_legacy_llm() -> None:
    """Explicit ``conversation_llm`` / ``extraction_llm`` kwargs take precedence
    over the legacy ``llm=`` shim so callers can still inject distinct backends.
    """
    explicit_conv = MagicMock(return_value="Explicit conversation reply.")
    explicit_extract = MagicMock(return_value=json.dumps({"mission_update": {}}))
    legacy = MagicMock(return_value="should not be called")

    agent = BrandingAssistantAgent(
        conversation_llm=explicit_conv,
        extraction_llm=explicit_extract,
        llm=legacy,
    )
    agent.respond(
        messages=[],
        current_mission=BrandingMission(
            company_name="TBD", company_description="To be discussed.", target_audience="TBD"
        ),
        user_message="Hi",
    )

    explicit_conv.assert_called_once()
    explicit_extract.assert_called_once()
    legacy.assert_not_called()


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
