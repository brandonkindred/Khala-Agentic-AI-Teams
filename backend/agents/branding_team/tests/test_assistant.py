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
    _strip_accidental_json,
)
from branding_team.assistant.models import MissionUpdate
from branding_team.tests.conftest import make_mission

pytestmark = [pytest.mark.integration]


def _extraction_result(**mission_update_kwargs) -> MagicMock:
    """Build a fake extraction-agent call result exposing ``.structured_output``.

    Mirrors the contract ``build_agent(..., structured_output=MissionUpdate)``
    gives callers: the result of calling the agent carries a validated
    ``MissionUpdate`` instance on ``.structured_output``.
    """
    return MagicMock(structured_output=MissionUpdate(**mission_update_kwargs))


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


def test_strip_accidental_json_suppresses_mission_after_stray_open_brace() -> None:
    """A stray ``{`` in prose before the real mission JSON must NOT
    short-circuit the scanner. Regression for the reviewer's case: the
    earlier implementation aborted the entire scan on the first unmatched
    brace and let the later mission JSON leak through to the user."""
    raw = (
        "See { example pseudo-code, then the real payload: "
        '{"company_name": "Acme", "target_audience": "devs"} done.'
    )
    assert _strip_accidental_json(raw) == ""


def test_strip_accidental_json_handles_multiple_stray_open_braces() -> None:
    """Several stray ``{`` characters before mission JSON must not stop the scan."""
    raw = 'Plenty { of { unbalanced { junk before {"company_name": "Acme"} the payload'
    assert _strip_accidental_json(raw) == ""


def test_strip_accidental_json_preserves_prose_with_only_stray_open_braces() -> None:
    """Stray ``{`` characters with NO subsequent mission JSON must pass
    through unchanged — the scanner must terminate without false positives."""
    prose = "Here are some braces: { { { but no real JSON object follows."
    assert _strip_accidental_json(prose) == prose


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
    current = make_mission(
        company_name="TBD",
        company_description="To be discussed.",
        target_audience="TBD",
    )
    update = MissionUpdate(company_name="Acme", target_audience="Developers")
    merged = _merge_mission_update(current, update)
    assert merged.company_name == "Acme"
    assert merged.target_audience == "Developers"
    assert merged.company_description == "To be discussed."


def test_merge_mission_update_replaces_and_dedupes_list_fields() -> None:
    current = make_mission(values=["old"])
    update = MissionUpdate(values=["clarity", "trust", "clarity"])
    merged = _merge_mission_update(current, update)
    assert merged.values == ["clarity", "trust"]


def test_merge_mission_update_sets_color_palettes_and_selected_index() -> None:
    from branding_team.models import ColorPalette

    current = make_mission()
    update = MissionUpdate(
        color_palettes=[
            ColorPalette(name="Warm", description="cozy", colors=["#a30"], sentiment="warm"),
            ColorPalette(name="Cool", description="crisp", colors=["#03a"], sentiment="cool"),
        ],
        selected_palette_index=1,
    )
    merged = _merge_mission_update(current, update)
    assert [p.name for p in merged.color_palettes] == ["Warm", "Cool"]
    assert merged.selected_palette_index == 1


def test_merge_mission_update_ignores_out_of_range_selected_index() -> None:
    current = make_mission()
    update = MissionUpdate(selected_palette_index=3)
    merged = _merge_mission_update(current, update)
    assert merged.selected_palette_index is None


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
        return_value=_extraction_result(
            company_name="Brandon Kindred",
            suggested_questions=[
                "What do you want to be known for?",
                "Who is your audience?",
            ],
        )
    )
    agent = BrandingAssistantAgent(conversation_llm=conversation_llm, extraction_llm=extraction_llm)
    mission = make_mission(
        company_name="TBD", company_description="To be discussed.", target_audience="TBD"
    )
    reply, updated_mission, suggested_questions, degraded = agent.respond(
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
    assert degraded is False
    conversation_llm.assert_called_once()
    extraction_llm.assert_called_once()


def test_branding_assistant_agent_suppresses_accidental_json_from_conversation_llm() -> None:
    """If the conversation LLM regresses and emits raw JSON, the user must NOT see it.
    A graceful prose fallback is shown, and the extractor still captures the mission."""
    conversation_llm = MagicMock(
        return_value='{"company_name": "Brandon Kindred", "company_description": ""}'
    )
    extraction_llm = MagicMock(
        return_value=_extraction_result(
            company_name="Brandon Kindred",
            suggested_questions=["What do you want to be known for?"],
        )
    )
    agent = BrandingAssistantAgent(conversation_llm=conversation_llm, extraction_llm=extraction_llm)
    mission = make_mission(
        company_name="TBD", company_description="To be discussed.", target_audience="TBD"
    )
    reply, updated_mission, _, degraded = agent.respond(
        messages=[],
        current_mission=mission,
        user_message="Brandon Kindred. it is a personal brand",
    )
    assert "{" not in reply and '"company_name"' not in reply
    assert reply.strip() != ""
    assert updated_mission.company_name == "Brandon Kindred"
    assert degraded is False


def test_branding_assistant_agent_handles_extraction_failure_gracefully() -> None:
    """Extractor failure must not break the conversation reply or mutate mission."""
    conversation_llm = MagicMock(return_value="Got it — tell me more about your audience.")
    extraction_llm = MagicMock(side_effect=Exception("extractor down"))
    agent = BrandingAssistantAgent(conversation_llm=conversation_llm, extraction_llm=extraction_llm)
    mission = make_mission(
        company_name="Acme", company_description="Software company.", target_audience="TBD"
    )
    reply, updated_mission, suggested_questions, degraded = agent.respond(
        messages=[],
        current_mission=mission,
        user_message="we sell to dev teams",
    )
    assert "Got it" in reply
    assert updated_mission.company_name == "Acme"
    assert len(suggested_questions) >= 1
    assert degraded is True


def test_branding_assistant_agent_handles_malformed_extraction_result() -> None:
    """The extractor call returns a result with no valid ``.structured_output``
    (not exception-raising). This must surface as ``degraded=True`` through the
    full ``respond()`` path rather than an indistinguishable no-op mission
    update."""
    conversation_llm = MagicMock(return_value="Got it — tell me more about your audience.")
    extraction_llm = MagicMock(return_value=MagicMock(structured_output="not-a-mission-update"))
    agent = BrandingAssistantAgent(conversation_llm=conversation_llm, extraction_llm=extraction_llm)
    mission = make_mission(
        company_name="Acme", company_description="Software company.", target_audience="TBD"
    )
    reply, updated_mission, suggested_questions, degraded = agent.respond(
        messages=[],
        current_mission=mission,
        user_message="we sell to dev teams",
    )
    assert "Got it" in reply
    assert updated_mission.company_name == "Acme"
    assert len(suggested_questions) >= 1
    assert degraded is True


def test_branding_assistant_agent_legacy_llm_kwarg_drives_both_stages() -> None:
    """Backward-compat: the legacy ``llm=`` kwarg must route to BOTH stages.

    Regression: a previous iteration only assigned ``llm`` to the conversation
    stage, leaving extraction to construct a real Strands ``Agent`` against
    the live LLM service. Offline / unit-test callers that injected a fake
    via ``llm=`` then hit a real network call on extraction. The fake must
    drive both stages so tests stay hermetic.
    """
    fake_llm = MagicMock()

    def _side_effect(prompt: str):
        # Conversation stage gets the chatty prompt template; extractor gets
        # the EXTRACTION_USER_TEMPLATE which references "Strategist's reply".
        if "Strategist's reply" in prompt:
            return _extraction_result(
                company_name="Acme",
                suggested_questions=["What does Acme do?"],
            )
        return "Acme — got it. What's the work you want to be known for?"

    fake_llm.side_effect = _side_effect
    agent = BrandingAssistantAgent(llm=fake_llm)
    mission = make_mission(
        company_name="TBD", company_description="To be discussed.", target_audience="TBD"
    )

    reply, updated_mission, suggested_questions, degraded = agent.respond(
        messages=[], current_mission=mission, user_message="We're Acme"
    )

    # Both stages used the injected fake — no real Strands Agent constructed.
    assert fake_llm.call_count == 2
    assert "Acme" in reply
    assert degraded is False
    assert updated_mission.company_name == "Acme"
    assert suggested_questions == ["What does Acme do?"]


def test_branding_assistant_agent_explicit_kwargs_override_legacy_llm() -> None:
    """Explicit ``conversation_llm`` / ``extraction_llm`` kwargs take precedence
    over the legacy ``llm=`` shim so callers can still inject distinct backends.
    """
    explicit_conv = MagicMock(return_value="Explicit conversation reply.")
    explicit_extract = MagicMock(return_value=_extraction_result())
    legacy = MagicMock(return_value="should not be called")

    agent = BrandingAssistantAgent(
        conversation_llm=explicit_conv,
        extraction_llm=explicit_extract,
        llm=legacy,
    )
    agent.respond(
        messages=[],
        current_mission=make_mission(
            company_name="TBD", company_description="To be discussed.", target_audience="TBD"
        ),
        user_message="Hi",
    )

    explicit_conv.assert_called_once()
    explicit_extract.assert_called_once()
    legacy.assert_not_called()


def test_branding_assistant_agent_default_conversation_agent_uses_build_agent() -> None:
    """Default construction routes the conversation stage through ``build_agent``.

    Runs under ``LLM_PROVIDER=dummy`` (no real LLM, no Postgres): construction
    resolves a dummy Strands model and never invokes it. Only the conversation
    stage is left to default-construct; the extraction stage is injected so
    this test stays hermetic on that side.
    """
    from strands import Agent
    from strands.handlers import null_callback_handler

    from branding_team.assistant.prompts import SYSTEM_PROMPT

    extraction_llm = MagicMock()
    agent = BrandingAssistantAgent(extraction_llm=extraction_llm)

    conversation_agent = agent._conversation_agent
    assert isinstance(conversation_agent, Agent)
    assert conversation_agent.name == "conversation"
    assert conversation_agent.system_prompt == SYSTEM_PROMPT
    assert conversation_agent.callback_handler is null_callback_handler


def test_branding_assistant_agent_default_extraction_agent_uses_build_agent() -> None:
    """Default construction routes the extraction stage through ``build_agent``.

    Runs under ``LLM_PROVIDER=dummy`` (no real LLM, no Postgres): construction
    resolves a dummy Strands model and never invokes it. Only the extraction
    stage is left to default-construct; the conversation stage is injected so
    this test stays hermetic on that side.
    """
    from strands import Agent
    from strands.handlers import null_callback_handler

    from branding_team.assistant.prompts import EXTRACTION_SYSTEM_PROMPT

    conversation_llm = MagicMock()
    agent = BrandingAssistantAgent(conversation_llm=conversation_llm)

    extraction_agent = agent._extraction_agent
    assert isinstance(extraction_agent, Agent)
    assert extraction_agent.name == "extraction"
    assert extraction_agent.system_prompt == EXTRACTION_SYSTEM_PROMPT
    assert extraction_agent.callback_handler is null_callback_handler


def test_branding_assistant_agent_handles_conversation_llm_failure() -> None:
    conversation_llm = MagicMock(side_effect=Exception("LLM unavailable"))
    extraction_llm = MagicMock()
    agent = BrandingAssistantAgent(conversation_llm=conversation_llm, extraction_llm=extraction_llm)
    mission = make_mission(
        company_name="TBD", company_description="To be discussed.", target_audience="TBD"
    )
    reply, updated_mission, suggested_questions, degraded = agent.respond(
        messages=[], current_mission=mission, user_message="Hello"
    )
    assert "help" in reply.lower() or "brand" in reply.lower()
    assert updated_mission.company_name == "TBD"
    assert len(suggested_questions) >= 1
    assert degraded is True
    extraction_llm.assert_not_called()
