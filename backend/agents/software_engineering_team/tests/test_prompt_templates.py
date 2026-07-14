"""Tests for shared code-v2 prompt template builders."""

from __future__ import annotations

from software_engineering_team.shared.prompts import (
    REQUIREMENT_CITATION_GUARDRAIL,
    build_code_review_prompt,
)


def test_build_code_review_prompt_includes_requirement_citation_guardrail() -> None:
    """LLM-fallback review prompt carries the citation guardrail and optional field."""
    prompt = build_code_review_prompt(project_kind="backend")
    assert REQUIREMENT_CITATION_GUARDRAIL in prompt
    assert "requirement_citation:" in prompt
