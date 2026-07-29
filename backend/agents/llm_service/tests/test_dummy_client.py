"""Tests for DummyLLMClient: complete_json returns dict; get_max_context_tokens; complete returns str."""

import json

import pytest
from pydantic import BaseModel

from llm_service import DummyLLMClient
from llm_service.clients.dummy import (
    _STRIP_FILLERS,
    _STRIP_SUFFIXES,
    _STRIP_VERBS,
    CODE_REVIEW_MIN_PROMPT_LENGTH,
    _extract_name_from_hint,
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


def test_extract_name_from_hint_keeps_usable_words() -> None:
    assert _extract_name_from_hint("Implement User Auth Service") == "user-auth"
    assert _extract_name_from_hint("Build PaymentWebhook", separator="_") == "payment_webhook"


def test_extract_name_from_hint_all_stripped_returns_placeholder() -> None:
    assert _extract_name_from_hint("the component") == "item-1"
    assert _extract_name_from_hint("create service module", separator="_") == "item_1"


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
    """Branding Phase 1 anchors live in system text; content blocks must not be dropped."""
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
        messages,  # type: ignore[arg-type]
        system_prompt=None,
        system_prompt_content=system_prompt_content,  # type: ignore[arg-type]
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


def test_code_review_min_prompt_length_constant() -> None:
    assert CODE_REVIEW_MIN_PROMPT_LENGTH == 200
