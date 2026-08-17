"""Unit tests for the branding-team prompt spec schema and renderer."""

from __future__ import annotations

import pytest
from pydantic import BaseModel, Field

from branding_team.prompt_spec import AgentPromptSpec, PromptFieldSpec, render_agent_prompt


class _TwoFieldModel(BaseModel):
    first_field: str = Field(default="", description="the first thing")
    second_field: str = Field(default="", description="the second thing")


class _MissingDescriptionModel(BaseModel):
    first_field: str = Field(default="")


class _BlankDescriptionModel(BaseModel):
    first_field: str = Field(default="", description="   ")


class _PunctuatedDescriptionModel(BaseModel):
    alpha: str = Field(default="", description="the first thing — with an em dash inside it")
    beta: str = Field(default="", description="the second (parenthetical) thing, comma included")
    gamma: str = Field(default="", description="third-thing with a hyphen")


def test_prompt_field_spec_rejects_blank_name() -> None:
    with pytest.raises(AssertionError, match="name must be a non-blank string"):
        PromptFieldSpec("", "a description")
    with pytest.raises(AssertionError, match="name must be a non-blank string"):
        PromptFieldSpec("   ", "a description")


def test_prompt_field_spec_rejects_blank_description() -> None:
    with pytest.raises(AssertionError, match="description must be a non-blank string"):
        PromptFieldSpec("a_field", "")


def test_prompt_field_spec_rejects_blank_sub_item() -> None:
    with pytest.raises(AssertionError, match="sub_items entries must be non-blank strings"):
        PromptFieldSpec("a_field", "a description", sub_items=("valid", "   "))


def test_prompt_field_spec_defaults_to_no_sub_items() -> None:
    assert PromptFieldSpec("a_field", "a description").sub_items == ()


def test_agent_prompt_spec_rejects_blank_opening() -> None:
    with pytest.raises(AssertionError, match="opening must be a non-blank string"):
        AgentPromptSpec(opening="  ", fields=(PromptFieldSpec("f", "d"),))


def test_agent_prompt_spec_rejects_empty_fields_and_no_structured_output() -> None:
    with pytest.raises(AssertionError, match="requires exactly one of"):
        AgentPromptSpec(opening="You are an agent.", fields=())


def test_agent_prompt_spec_rejects_both_fields_and_structured_output() -> None:
    with pytest.raises(AssertionError, match="requires exactly one of"):
        AgentPromptSpec(
            opening="You are an agent.",
            fields=(PromptFieldSpec("f", "d"),),
            structured_output=_TwoFieldModel,
        )


def test_agent_prompt_spec_rejects_non_basemodel_structured_output() -> None:
    with pytest.raises(AssertionError, match="structured_output must be a BaseModel subclass"):
        AgentPromptSpec(opening="You are an agent.", structured_output=dict)


def test_agent_prompt_spec_rejects_structured_output_with_no_fields() -> None:
    class _EmptyModel(BaseModel):
        pass

    with pytest.raises(AssertionError, match="structured_output must declare at least one field"):
        AgentPromptSpec(opening="You are an agent.", structured_output=_EmptyModel)


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


def test_render_agent_prompt_indents_sub_items_under_their_field() -> None:
    spec = AgentPromptSpec(
        opening="You are a Test Agent. Do this:",
        fields=(
            PromptFieldSpec(
                "first_field", "the first thing", sub_items=("detail one", "detail two")
            ),
            PromptFieldSpec("second_field", "the second thing"),
        ),
    )
    assert render_agent_prompt(spec) == (
        "You are a Test Agent. Do this:\n"
        "1. first_field — the first thing\n"
        "   - detail one\n"
        "   - detail two\n"
        "2. second_field — the second thing"
    )


def test_render_agent_prompt_omits_sub_items_when_absent() -> None:
    spec = AgentPromptSpec(
        opening="You are a Test Agent. Do this:",
        fields=(PromptFieldSpec("only_field", "the only thing"),),
    )
    assert (
        render_agent_prompt(spec)
        == "You are a Test Agent. Do this:\n1. only_field — the only thing"
    )


def test_render_agent_prompt_derives_field_lines_from_structured_output() -> None:
    spec = AgentPromptSpec(
        opening="You are a Test Agent. Do this:",
        structured_output=_TwoFieldModel,
    )
    assert render_agent_prompt(spec) == (
        "You are a Test Agent. Do this:\n"
        "1. first_field — the first thing\n"
        "2. second_field — the second thing"
    )


def test_render_agent_prompt_appends_closing_with_structured_output() -> None:
    spec = AgentPromptSpec(
        opening="You are a Test Agent. Do this:",
        structured_output=_TwoFieldModel,
        closing="Be concise.",
    )
    assert render_agent_prompt(spec) == (
        "You are a Test Agent. Do this:\n"
        "1. first_field — the first thing\n"
        "2. second_field — the second thing\n"
        "Be concise."
    )


def test_render_agent_prompt_preserves_order_and_text_with_punctuated_descriptions() -> None:
    spec = AgentPromptSpec(
        opening="You are a Test Agent. Do this:",
        structured_output=_PunctuatedDescriptionModel,
    )
    assert render_agent_prompt(spec) == (
        "You are a Test Agent. Do this:\n"
        "1. alpha — the first thing — with an em dash inside it\n"
        "2. beta — the second (parenthetical) thing, comma included\n"
        "3. gamma — third-thing with a hyphen"
    )


def test_render_agent_prompt_rejects_structured_output_field_missing_description() -> None:
    spec = AgentPromptSpec(opening="You are an agent.", structured_output=_MissingDescriptionModel)
    with pytest.raises(AssertionError, match="must declare a non-blank Field.description=...."):
        render_agent_prompt(spec)


def test_render_agent_prompt_rejects_structured_output_field_blank_description() -> None:
    spec = AgentPromptSpec(opening="You are an agent.", structured_output=_BlankDescriptionModel)
    with pytest.raises(AssertionError, match="must declare a non-blank Field.description=...."):
        render_agent_prompt(spec)
