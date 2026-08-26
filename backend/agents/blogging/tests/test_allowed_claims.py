"""Tests for extract_allowed_claims and the EXTRACT_CLAIMS_PROMPT template."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict

import pytest
from agents.blogging.blog_research_agent.allowed_claims import (
    EXTRACT_CLAIMS_PROMPT,
    AllowedClaims,
    ClaimEntry,
    extract_allowed_claims,
)


class _StubLLMClient:
    """Minimal complete_json stub, queuing one response (or exception) per call."""

    def __init__(self, response: Any) -> None:
        self.response = response
        self.calls: list[str] = []

    def complete_json(
        self, prompt: str, *, temperature: float = 0.0, objective: str = "", think: bool = False
    ):
        self.calls.append(prompt)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def _references():
    return [
        SimpleNamespace(title="Source One", url="https://example.com/1"),
        SimpleNamespace(title="Source Two", url="https://example.com/2"),
    ]


def test_prompt_template_substitution_preserves_literal_json_example() -> None:
    """The prompt's literal JSON example must survive substitution untouched."""
    prompt = EXTRACT_CLAIMS_PROMPT.replace("__COMPILED_DOCUMENT__", "doc text").replace(
        "__SOURCES_TEXT__", "sources text"
    )
    assert '{"claims": [{"id": "1"' in prompt
    assert "doc text" in prompt
    assert "sources text" in prompt


def test_extract_allowed_claims_happy_path() -> None:
    """A well-formed LLM response yields a populated AllowedClaims without raising."""
    llm_response: Dict[str, Any] = {
        "claims": [
            {
                "id": "1",
                "text": "The sky is blue due to Rayleigh scattering.",
                "citations": ["Source One"],
                "risk_level": "low",
            },
            {
                "id": "2",
                "text": "Water boils at 100C at sea level.",
                "citations": ["Source Two"],
                "risk_level": "medium",
            },
        ]
    }
    client = _StubLLMClient(llm_response)

    result = extract_allowed_claims(
        client,
        compiled_document="A realistic research document body about physics and chemistry facts.",
        references=_references(),
        topic="Physics 101",
    )

    assert isinstance(result, AllowedClaims)
    assert result.topic == "Physics 101"
    assert len(result.claims) == 2
    assert result.claims[0].id == "1"
    assert result.claims[0].text == "The sky is blue due to Rayleigh scattering."
    assert result.claims[0].citations == ["Source One"]
    assert result.claims[0].risk_level == "low"
    assert result.claims[1].risk_level == "medium"

    # The prompt actually reached the LLM client with real content substituted in.
    assert len(client.calls) == 1
    assert "physics and chemistry facts" in client.calls[0]
    assert "Source One" in client.calls[0]


def test_extract_allowed_claims_llm_raises_returns_empty() -> None:
    """An exception from the LLM client is swallowed; empty AllowedClaims returned."""
    client = _StubLLMClient(RuntimeError("boom"))

    result = extract_allowed_claims(
        client,
        compiled_document="doc",
        references=_references(),
        topic="t",
    )

    assert result == AllowedClaims(topic="t", claims=[])


@pytest.mark.parametrize("bad_response", [["not", "a", "dict"], "a string", 42, None])
def test_extract_allowed_claims_non_dict_response_returns_empty(bad_response: Any) -> None:
    """A non-dict LLM response is handled gracefully with empty claims."""
    client = _StubLLMClient(bad_response)

    result = extract_allowed_claims(
        client,
        compiled_document="doc",
        references=_references(),
        topic="t",
    )

    assert result == AllowedClaims(topic="t", claims=[])


def test_extract_allowed_claims_missing_claims_key_returns_empty() -> None:
    """A dict response with no 'claims' key yields empty claims, not a KeyError."""
    client = _StubLLMClient({"unexpected": "shape"})

    result = extract_allowed_claims(
        client,
        compiled_document="doc",
        references=_references(),
        topic="t",
    )

    assert result == AllowedClaims(topic="t", claims=[])


def test_extract_allowed_claims_filters_malformed_entries() -> None:
    """Malformed claim entries are skipped or coerced; valid ones survive."""
    llm_response = {
        "claims": [
            {"id": "1", "text": "Valid claim.", "citations": "Source One", "risk_level": "HIGH"},
            "not a dict",
            {"id": "3", "text": "   ", "citations": [], "risk_level": "low"},
            {"id": "4", "citations": [], "risk_level": "low"},
            {
                "id": "5",
                "text": "Another valid claim.",
                "citations": ["a", "b"],
                "risk_level": "nonsense",
            },
        ]
    }
    client = _StubLLMClient(llm_response)

    result = extract_allowed_claims(
        client,
        compiled_document="doc",
        references=_references(),
        topic="t",
    )

    assert [c.id for c in result.claims] == ["1", "5"]
    # citations coerced from a bare string to a single-item list
    assert result.claims[0].citations == ["Source One"]
    # invalid risk_level values fall back to "low"
    assert result.claims[0].risk_level == "high"
    assert result.claims[1].risk_level == "low"


def test_allowed_claims_to_dict() -> None:
    """to_dict() exports a plain-dict-serializable representation."""
    claims = AllowedClaims(
        topic="t",
        claims=[ClaimEntry(id="1", text="x", citations=["a"], risk_level="low")],
    )

    assert claims.to_dict() == {
        "topic": "t",
        "claims": [{"id": "1", "text": "x", "citations": ["a"], "risk_level": "low"}],
    }


def test_extract_allowed_claims_skips_entry_that_raises_during_construction() -> None:
    """A claim entry whose 'citations' value can't be listified is skipped, not raised."""
    llm_response = {
        "claims": [
            {"id": "1", "text": "Valid claim.", "citations": ["ok"], "risk_level": "low"},
            # citations=5 is truthy (so not replaced by []), but not a string either,
            # so list(5) is attempted and raises TypeError inside the loop.
            {"id": "2", "text": "Bad citations claim.", "citations": 5, "risk_level": "low"},
        ]
    }
    client = _StubLLMClient(llm_response)

    result = extract_allowed_claims(
        client,
        compiled_document="doc",
        references=_references(),
        topic="t",
    )

    assert [c.id for c in result.claims] == ["1"]


def test_extract_allowed_claims_claims_key_read_raises_returns_empty() -> None:
    """If reading the 'claims' key itself raises, the function still returns cleanly."""

    class _ExplodingDict(dict):
        def get(self, key, default=None):
            if key == "claims":
                raise TypeError("simulated get() failure")
            return super().get(key, default)

    client = _StubLLMClient(_ExplodingDict({"claims": []}))

    result = extract_allowed_claims(
        client,
        compiled_document="doc",
        references=_references(),
        topic="t",
    )

    assert result == AllowedClaims(topic="t", claims=[])


def test_extract_allowed_claims_no_references_uses_placeholder_sources() -> None:
    """With an empty references list, the sources section still substitutes cleanly."""
    client = _StubLLMClient({"claims": []})

    result = extract_allowed_claims(
        client,
        compiled_document="doc",
        references=[],
        topic="",
    )

    assert result == AllowedClaims(topic="", claims=[])
    assert "No sources" in client.calls[0]


def test_extract_allowed_claims_document_containing_placeholder_literal_is_preserved() -> None:
    """A compiled_document containing a placeholder-like literal must not be
    mangled by a later substitution (regression: chained .replace() would let
    the __SOURCES_TEXT__ substitution re-match text just inserted from
    compiled_document)."""
    client = _StubLLMClient({"claims": []})
    tricky_document = "This document literally discusses the __SOURCES_TEXT__ token."

    extract_allowed_claims(
        client,
        compiled_document=tricky_document,
        references=_references(),
        topic="t",
    )

    prompt = client.calls[0]
    assert tricky_document in prompt
    # The real sources section (built from references) must still appear once,
    # not have overwritten the literal text inside compiled_document.
    assert "Source One" in prompt
    assert prompt.count("__SOURCES_TEXT__") == 1
