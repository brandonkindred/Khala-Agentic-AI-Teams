"""Tests for DummyLLMClient heuristic stubs and Strands Model surface.

Covers ``complete`` / ``complete_json`` / ``complete_text``, ``get_max_context_tokens``,
async ``structured_output`` (including multi-turn routing), ``stream`` with
``system_prompt_content`` (branding Phase 1 anchors, Phase 2 narrative stubs, and
empty-list overrides), and helpers (``_extract_name_from_hint``, strip-filter
frozensets, ``_content_to_text`` / ``_last_user_text`` / ``_aggregated_user_tool_text``).
"""

from __future__ import annotations

import json
import re
from typing import Any, cast

import pytest
from pydantic import BaseModel

from llm_service import DummyLLMClient
from llm_service.clients.dummy import (
    _STRIP_FILLERS,
    _STRIP_SUFFIXES,
    _STRIP_VERBS,
    CODE_REVIEW_MIN_PROMPT_LENGTH,
    _aggregated_user_tool_text,
    _content_to_text,
    _extract_name_from_hint,
    _last_user_text,
    _placeholder_slug,
)


def test_dummy_get_max_context_tokens() -> None:
    c = DummyLLMClient()
    assert c.get_max_context_tokens() == 16384


def test_dummy_complete_returns_str() -> None:
    c = DummyLLMClient()
    s = c.complete("hello", temperature=0.5)
    assert isinstance(s, str)
    assert "Dummy" in s


def test_dummy_complete_json_returns_dict() -> None:
    c = DummyLLMClient()
    j = c.complete_json("hello", temperature=0.1)
    assert isinstance(j, dict)
    assert "output" in j or "status" in j or "summary" in j or "tasks" in j


def test_dummy_complete_json_architecture_stub() -> None:
    c = DummyLLMClient()
    j = c.complete_json(
        "Generate architecture_document with components and overview for the system.",
        temperature=0.0,
    )
    assert "overview" in j
    assert "architecture_document" in j
    assert "components" in j
    assert "diagrams" in j


def test_dummy_complete_text_alias() -> None:
    c = DummyLLMClient()
    s = c.complete_text("hi", objective="test", temperature=0.0)
    assert isinstance(s, str)


def test_dummy_complete_json_accepts_schema_kwarg_as_noop() -> None:
    """schema= is a client-agnostic opt-in — an unsupporting client silently ignores it."""
    c = DummyLLMClient()
    assert c.supports_structured_output() is False
    j = c.complete_json("hello", temperature=0.1, schema={"type": "object"})
    assert isinstance(j, dict)


def test_content_to_text_serializes_json_tool_result_blocks() -> None:
    content = [
        {"json": {"request": "architecture_document with components and overview"}},
    ]
    text = _content_to_text(content)
    assert "architecture_document" in text
    assert "components" in text
    assert "overview" in text


@pytest.mark.asyncio
async def test_structured_output_routes_on_json_tool_result_content() -> None:
    class _ArchStub(BaseModel):
        overview: str
        architecture_document: str
        components: list
        diagrams: dict
        decisions: list

    c = DummyLLMClient()
    prompt = [
        {
            "role": "tool",
            "content": [
                {"json": {"request": "architecture_document with components and overview"}},
            ],
        }
    ]
    events = []
    async for event in c.structured_output(_ArchStub, prompt):
        events.append(event)
    assert isinstance(events[0]["output"], _ArchStub)
    assert "Dummy architecture" in events[0]["output"].overview


def _as_stream_messages(messages: list[dict[str, Any]]) -> Any:
    """Cast plain dict fixtures to the Strands Message type expected by ``stream``."""
    return cast(Any, messages)


def _as_system_content(blocks: list[dict[str, str]]) -> Any:
    """Cast plain dict fixtures to Strands ``SystemContentBlock`` list."""
    return cast(Any, blocks)


@pytest.mark.asyncio
async def test_dummy_stream_empty_system_prompt_content_clears_stale_string() -> None:
    """An explicit empty content list must override a stale branding system_prompt."""
    c = DummyLLMClient()
    messages = [{"role": "user", "content": [{"text": "BrandingMission payload for Dummy Co."}]}]
    chunks: list[str] = []
    async for event in c.stream(
        _as_stream_messages(messages),
        system_prompt=(
            "You must return brand_purpose, mission_statement, and vision_statement "
            "for the brand strategy agent."
        ),
        system_prompt_content=[],
    ):
        delta = (event.get("contentBlockDelta") or {}).get("delta") or {}
        text = delta.get("text")
        if text:
            chunks.append(text)
    assert chunks, "expected a text content delta"
    data = json.loads(chunks[0])
    assert "brand_purpose" not in data
    assert data.get("status") == "ok" or "output" in data


def test_last_user_text_concatenates_all_text_blocks() -> None:
    messages = [
        {
            "role": "user",
            "content": [
                {"text": "prefix context"},
                {"text": "architecture_document with components and overview"},
            ],
        }
    ]
    assert _last_user_text(messages) == (
        "prefix context\narchitecture_document with components and overview"
    )


@pytest.mark.asyncio
async def test_structured_output_uses_later_user_text_blocks_for_routing() -> None:
    """Anchors in a later content block must still select the intended stub."""

    class _ArchStub(BaseModel):
        overview: str
        architecture_document: str
        components: list
        diagrams: dict
        decisions: list

    c = DummyLLMClient()
    prompt = [
        {
            "role": "user",
            "content": [
                {"text": "Please produce the deliverable."},
                {"text": "Need architecture_document with components and overview."},
            ],
        }
    ]
    events = []
    async for event in c.structured_output(_ArchStub, prompt):
        events.append(event)
    assert isinstance(events[0]["output"], _ArchStub)
    assert "Dummy architecture" in events[0]["output"].overview


def test_aggregated_user_tool_text_preserves_earlier_turns() -> None:
    messages = [
        {
            "role": "user",
            "content": [{"text": "Need architecture_document with components and overview."}],
        },
        {"role": "assistant", "content": [{"text": "Sure."}]},
        {"role": "user", "content": [{"text": "return that as structured output"}]},
    ]
    assert "architecture_document" in _aggregated_user_tool_text(messages)
    assert "return that as structured output" in _aggregated_user_tool_text(messages)
    # stream path stays latest-turn-only
    assert _last_user_text(messages) == "return that as structured output"


@pytest.mark.asyncio
async def test_structured_output_uses_earlier_user_turn_for_routing() -> None:
    """Follow-up structured_output requests must still see original routing anchors."""

    class _ArchStub(BaseModel):
        overview: str
        architecture_document: str
        components: list
        diagrams: dict
        decisions: list

    c = DummyLLMClient()
    prompt = [
        {
            "role": "user",
            "content": [{"text": "Need architecture_document with components and overview."}],
        },
        {"role": "assistant", "content": [{"text": "Generated architecture."}]},
        {"role": "user", "content": [{"text": "return that as structured output"}]},
    ]
    events = []
    async for event in c.structured_output(_ArchStub, prompt):
        events.append(event)
    assert isinstance(events[0]["output"], _ArchStub)
    assert "Dummy architecture" in events[0]["output"].overview


def test_extract_name_from_hint_keeps_usable_words() -> None:
    assert _extract_name_from_hint("Implement User Auth Service") == "user-auth"
    assert _extract_name_from_hint("Build PaymentWebhook", separator="_") == "payment_webhook"


def test_extract_name_from_hint_all_stripped_returns_unique_placeholder() -> None:
    a = _extract_name_from_hint("the component")
    b = _extract_name_from_hint("create service module", separator="_")
    c = _extract_name_from_hint("configure service module", separator="_")
    assert a.startswith("item-")
    assert b.startswith("item_")
    assert c.startswith("item_")
    assert a != _extract_name_from_hint("a component")
    assert b != c  # distinct all-stripped hints must not collide on one path
    assert re.fullmatch(r"item-[0-9a-f]+", a)
    assert re.fullmatch(r"item_[0-9a-f]+", b)


def test_placeholder_slug_never_exceeds_max_length() -> None:
    """Fallback must respect max_length even when truncation strips result to ""."""
    assert _placeholder_slug("some hint", "-", 25) == "item-3082b299"
    slug = _placeholder_slug("x", "i", 1)
    assert slug
    assert len(slug) <= 1


@pytest.mark.asyncio
async def test_structured_output_rejects_non_pydantic_output_model() -> None:
    """output_model without model_validate must raise TypeError, not AssertionError."""
    c = DummyLLMClient()
    prompt = [{"role": "user", "content": [{"text": "hello"}]}]
    with pytest.raises(TypeError, match="model_validate"):
        async for _ in c.structured_output(object, prompt):
            pass


def test_strip_filter_constants_are_frozensets() -> None:
    assert isinstance(_STRIP_VERBS, frozenset)
    assert isinstance(_STRIP_FILLERS, frozenset)
    assert isinstance(_STRIP_SUFFIXES, frozenset)
    with pytest.raises(AttributeError):
        _STRIP_VERBS.add("mutate")  # type: ignore[attr-defined]


class _FallbackStub(BaseModel):
    output: str
    status: str


@pytest.mark.asyncio
async def test_dummy_structured_output_yields_validated_model() -> None:
    c = DummyLLMClient()
    prompt = [{"role": "user", "content": [{"text": "hello"}]}]
    events = []
    async for event in c.structured_output(_FallbackStub, prompt):
        events.append(event)
    assert len(events) == 1
    assert "output" in events[0]
    assert isinstance(events[0]["output"], _FallbackStub)
    assert events[0]["output"].status == "ok"


@pytest.mark.asyncio
async def test_dummy_stream_uses_system_prompt_content_when_system_prompt_absent() -> None:
    """Branding Phase 1: system text via content blocks alone selects purpose/vision stub."""
    c = DummyLLMClient()
    messages = [{"role": "user", "content": [{"text": "BrandingMission payload for Dummy Co."}]}]
    system_prompt_content = [
        {
            "text": (
                "You must return brand_purpose, mission_statement, and vision_statement "
                "for the brand strategy agent."
            )
        }
    ]
    chunks: list[str] = []
    async for event in c.stream(
        _as_stream_messages(messages),
        system_prompt=None,
        system_prompt_content=_as_system_content(system_prompt_content),
    ):
        delta = (event.get("contentBlockDelta") or {}).get("delta") or {}
        text = delta.get("text")
        if text:
            chunks.append(text)
    assert chunks, "expected a text content delta"
    data = json.loads(chunks[0])
    assert "brand_purpose" in data
    assert "vision_statement" in data


@pytest.mark.asyncio
async def test_dummy_stream_prefers_system_prompt_content_over_stale_string() -> None:
    """Branding Phase 1: content blocks win over a non-matching legacy system_prompt."""
    c = DummyLLMClient()
    messages = [{"role": "user", "content": [{"text": "BrandingMission payload for Dummy Co."}]}]
    system_prompt_content = [
        {
            "text": (
                "You must return brand_purpose, mission_statement, and vision_statement "
                "for the brand strategy agent."
            )
        }
    ]
    chunks: list[str] = []
    async for event in c.stream(
        _as_stream_messages(messages),
        system_prompt="You are a generic assistant with no branding output fields.",
        system_prompt_content=_as_system_content(system_prompt_content),
    ):
        delta = (event.get("contentBlockDelta") or {}).get("delta") or {}
        text = delta.get("text")
        if text:
            chunks.append(text)
    assert chunks, "expected a text content delta"
    data = json.loads(chunks[0])
    assert "brand_purpose" in data
    assert "vision_statement" in data


def test_code_review_catch_all_matches_long_prompt_with_approved() -> None:
    c = DummyLLMClient()
    # Build a prompt that mentions both "code to review" and "approved", longer
    # than the named threshold so the catch-all still wins over short stubs.
    padding = "x" * (CODE_REVIEW_MIN_PROMPT_LENGTH + 50)
    prompt = f"Please code to review this chunk. approved={padding}"
    assert len(prompt.lower()) > CODE_REVIEW_MIN_PROMPT_LENGTH
    j = c.complete_json(prompt, temperature=0.0)
    assert j["approved"] is True
    assert j["summary"] == "Code review passed (dummy)."
    assert "issues" in j


def test_code_review_catch_all_does_not_match_unrelated_chunk_prompt() -> None:
    """A long prompt about a "chunk" of data (not a code review) must not be
    misclassified as a code-review response by the bare "chunk" heuristic.
    """
    c = DummyLLMClient()
    padding = "x" * (CODE_REVIEW_MIN_PROMPT_LENGTH + 50)
    prompt = f"Please process this chunk of user data and summarize it. {padding}"
    assert len(prompt.lower()) > CODE_REVIEW_MIN_PROMPT_LENGTH
    j = c.complete_json(prompt, temperature=0.0)
    assert j.get("summary") != "Code review passed (dummy)."


def test_code_review_min_prompt_length_constant() -> None:
    assert CODE_REVIEW_MIN_PROMPT_LENGTH == 200


_BRANDING_PHASE2_SYSTEM_PROMPTS = [
    (
        "brand_story and boilerplate_variants for the brand story specialist",
        {"brand_story", "hero_narrative", "boilerplate_variants"},
    ),
    (
        "personality_traits — carry forward brand_story for the archetype specialist",
        {"brand_story", "hero_narrative", "boilerplate_variants", "brand_archetypes"},
    ),
    (
        "tagline_rationale and elevator_pitches for the tagline specialist",
        {
            "brand_story",
            "hero_narrative",
            "boilerplate_variants",
            "brand_archetypes",
            "tagline",
            "tagline_rationale",
            "elevator_pitches",
        },
    ),
    (
        "messaging_framework and audience_message_maps for the messaging specialist",
        {
            "brand_story",
            "hero_narrative",
            "boilerplate_variants",
            "brand_archetypes",
            "tagline",
            "tagline_rationale",
            "elevator_pitches",
            "messaging_framework",
            "audience_message_maps",
        },
    ),
    (
        "jobs_to_be_done and media_habits for the persona specialist",
        {
            "brand_story",
            "hero_narrative",
            "boilerplate_variants",
            "brand_archetypes",
            "tagline",
            "tagline_rationale",
            "elevator_pitches",
            "messaging_framework",
            "audience_message_maps",
            "persona_profiles",
        },
    ),
    (
        "writing_guidelines and editorial_quality_bar for the voice specialist",
        {
            "brand_story",
            "hero_narrative",
            "boilerplate_variants",
            "brand_archetypes",
            "tagline",
            "tagline_rationale",
            "elevator_pitches",
            "messaging_framework",
            "audience_message_maps",
            "persona_profiles",
            "writing_guidelines",
        },
    ),
]


@pytest.mark.parametrize("system_prompt,expected_keys", _BRANDING_PHASE2_SYSTEM_PROMPTS)
def test_branding_phase2_branches_return_cumulative_keys(
    system_prompt: str, expected_keys: set[str]
) -> None:
    """Each Phase 2 branding specialist must carry forward exactly the keys
    its predecessors introduced, plus its own — pinning the incremental
    ``_branding_phase2_narrative_*`` helper composition against silent
    branch desync."""
    c = DummyLLMClient()
    j = c.complete_json("dummy prompt", system_prompt=system_prompt, temperature=0.0)
    assert set(j.keys()) == expected_keys


def test_branding_phase2_branch_results_do_not_share_mutable_state() -> None:
    """Each ``complete_json`` call must hand back independent objects so
    mutating one response's nested lists/dicts can't leak into another call's
    response."""
    c = DummyLLMClient()
    system_prompt = _BRANDING_PHASE2_SYSTEM_PROMPTS[0][0]
    first = c.complete_json("dummy prompt", system_prompt=system_prompt, temperature=0.0)
    second = c.complete_json("dummy prompt", system_prompt=system_prompt, temperature=0.0)
    first["boilerplate_variants"].append("mutated")
    assert "mutated" not in second["boilerplate_variants"]


def test_senior_backend_branch_generates_valid_python_for_quote_laden_hint() -> None:
    """A task_hint with quotes/triple-quotes must not corrupt the generated source."""
    c = DummyLLMClient()
    hint = '''Build user's "profile" module """ oops'''
    prompt = f"You are a senior backend software engineer. Task: {hint}"
    j = c.complete_json(prompt, temperature=0.0)
    compile(j["code"], "<code>", "exec")
    compile(j["tests"], "<tests>", "exec")
    for path, content in j["files"].items():
        compile(content, path, "exec")


def test_security_branch_not_shadowed_by_code_review_catch_all() -> None:
    """Security prompts include "Code to review" as an input-section header (see
    security_agent/prompts.py), which also matches the generic code-review
    catch-all. The security branch must win so callers get "vulnerabilities",
    not an empty generic "issues" review.
    """
    c = DummyLLMClient()
    prompt = (
        "You are a Cybersecurity Expert. Review the code for security vulnerabilities.\n"
        "**Input:**\n- Code to review\n- Language\n"
    )
    j = c.complete_json(prompt, temperature=0.0)
    assert "vulnerabilities" in j
    assert j["vulnerabilities"] == []
    assert j["summary"] == "No security issues found (dummy)"
    assert "issues" not in j


def test_accessibility_branch_not_shadowed_by_code_review_catch_all() -> None:
    """Accessibility prompts include "Code to review" as an input-section header
    (see accessibility_agent/prompts.py), which also matches the generic
    code-review catch-all. The accessibility branch must win so callers get
    the WCAG-shaped stub, not the generic code-review stub.
    """
    c = DummyLLMClient()
    prompt = (
        "You are an expert Accessibility Engineer specializing in WCAG 2.2 compliance.\n"
        "**Input:**\n- Code to review (JSX/TSX, HTML templates)\n"
        "Return a list of accessibility issues.\n"
    )
    j = c.complete_json(prompt, temperature=0.0)
    assert j["issues"] == []
    assert j["summary"] == "No WCAG 2.2 accessibility issues found (dummy)"
    assert "vulnerabilities" not in j


def test_voice_principles_branch_nests_editorial_quality_bar_in_writing_guidelines() -> None:
    """The VoicePrinciplesDrafter branch must return ``editorial_quality_bar``
    nested inside ``writing_guidelines`` (matching ``WritingGuidelinesBody``),
    not as a sibling top-level key.
    """
    c = DummyLLMClient()
    system_prompt = (
        "You are a Voice Principles Drafter. Using all prior narrative fields from Inputs "
        "from previous nodes and the mission's desired_voice, carry the prior fields forward "
        "unchanged and produce writing_guidelines:\n"
        "1. voice_principles — 3-4 principles (e.g. 'Use a confident, human voice')\n"
        "2. style_dos — 3-4 writing best practices\n"
        "3. style_donts — 3-4 things to avoid\n"
        "4. editorial_quality_bar — 3-4 quality standards every piece must meet\n\n"
        "This is the final step in narrative development."
    )
    j = c.complete_json("go", system_prompt=system_prompt, temperature=0.0)
    assert isinstance(j["writing_guidelines"], dict)
    guidelines = j["writing_guidelines"]
    for field in ("voice_principles", "style_dos", "style_donts", "editorial_quality_bar"):
        assert len(guidelines[field]) == 3
    assert "editorial_quality_bar" not in j
