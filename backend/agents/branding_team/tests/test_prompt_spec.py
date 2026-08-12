"""Unit tests for the branding-team prompt spec schema and renderer."""

from __future__ import annotations

import pytest

from branding_team.prompt_spec import AgentPromptSpec, PromptFieldSpec, render_agent_prompt


def test_prompt_field_spec_rejects_blank_name() -> None:
    with pytest.raises(AssertionError, match="name must be a non-blank string"):
        PromptFieldSpec("", "a description")
    with pytest.raises(AssertionError, match="name must be a non-blank string"):
        PromptFieldSpec("   ", "a description")


def test_prompt_field_spec_rejects_blank_description() -> None:
    with pytest.raises(AssertionError, match="description must be a non-blank string"):
        PromptFieldSpec("a_field", "")


def test_agent_prompt_spec_rejects_blank_opening() -> None:
    with pytest.raises(AssertionError, match="opening must be a non-blank string"):
        AgentPromptSpec(opening="  ", fields=(PromptFieldSpec("f", "d"),))


def test_agent_prompt_spec_rejects_empty_fields() -> None:
    with pytest.raises(AssertionError, match="fields must be non-empty"):
        AgentPromptSpec(opening="You are an agent.", fields=())


def test_agent_prompt_spec_rejects_blank_closing() -> None:
    with pytest.raises(AssertionError, match="closing must be a non-blank string"):
        AgentPromptSpec(
            opening="You are an agent.", fields=(PromptFieldSpec("f", "d"),), closing="   "
        )


def test_render_agent_prompt_numbers_fields_and_uses_em_dash() -> None:
    spec = AgentPromptSpec(
        opening="You are a Test Agent. Do this:",
        fields=(
            PromptFieldSpec("first_field", "the first thing"),
            PromptFieldSpec("second_field", "the second thing"),
        ),
    )
    assert render_agent_prompt(spec) == (
        "You are a Test Agent. Do this:\n"
        "1. first_field — the first thing\n"
        "2. second_field — the second thing"
    )


def test_render_agent_prompt_appends_closing_when_present() -> None:
    spec = AgentPromptSpec(
        opening="You are a Test Agent. Do this:",
        fields=(PromptFieldSpec("only_field", "the only thing"),),
        closing="Be concise.",
    )
    assert render_agent_prompt(spec) == (
        "You are a Test Agent. Do this:\n1. only_field — the only thing\nBe concise."
    )


def test_render_agent_prompt_omits_closing_line_when_absent() -> None:
    spec = AgentPromptSpec(
        opening="You are a Test Agent. Do this:",
        fields=(PromptFieldSpec("only_field", "the only thing"),),
    )
    rendered = render_agent_prompt(spec)
    assert rendered == "You are a Test Agent. Do this:\n1. only_field — the only thing"
    assert not rendered.endswith("\n")


def test_render_agent_prompt_rejects_non_spec_argument() -> None:
    with pytest.raises(AssertionError, match="spec must be an AgentPromptSpec"):
        render_agent_prompt("not a spec")  # type: ignore[arg-type]
