"""Tests for the generalized (non-code-v2) prompt template builders."""

from __future__ import annotations

from software_engineering_team.shared.prompt_utils import JSON_OUTPUT_INSTRUCTION
from software_engineering_team.shared.prompts import (
    build_document_rewrite_prompt,
    build_json_output_prompt,
    format_context_block,
)


def test_format_context_block_renders_fenced_block() -> None:
    """format_context_block renders the '**Label:**\\n---\\nslot\\n---\\n\\n' shape."""
    block = format_context_block("Specification", "{spec_content}")
    assert block == "**Specification:**\n---\n{spec_content}\n---\n\n"


def test_format_context_block_preserves_format_style_slot_tokens() -> None:
    """A .format()-style slot token survives untouched (no substitution)."""
    block = format_context_block("Answered questions", "{answered_questions}")
    assert "{answered_questions}" in block


def test_build_json_output_prompt_default_trailer_is_json_output_instruction() -> None:
    """Default trailer reuses the codebase's shared JSON_OUTPUT_INSTRUCTION."""
    prompt = build_json_output_prompt(
        role_sentence="You are an expert DevOps Agent.",
        json_schema="- artifacts: object(path -> file_content)",
    )
    assert prompt.endswith(JSON_OUTPUT_INSTRUCTION)


def test_build_json_output_prompt_trailer_override() -> None:
    """Callers can override the trailer with a terser instruction (e.g. devops style)."""
    prompt = build_json_output_prompt(
        role_sentence="You are an expert DevOps Agent.",
        json_schema="- artifacts: object(path -> file_content)",
        trailer="Return JSON only.",
    )
    assert prompt.endswith("Return JSON only.")
    assert JSON_OUTPUT_INSTRUCTION not in prompt


def test_build_json_output_prompt_includes_all_pieces_in_order() -> None:
    """role_sentence, rules, context_blocks, and json_schema all appear, in order."""
    prompt = build_json_output_prompt(
        role_sentence="You are an expert Product Analyst.",
        rules="1. Cite the requirement you drew from.\n\n",
        context_blocks=format_context_block("Specification", "{spec_content}"),
        json_schema='{{\n  "open_questions": [...]\n}}',
    )
    role_idx = prompt.index("You are an expert Product Analyst.")
    rules_idx = prompt.index("Cite the requirement")
    context_idx = prompt.index("**Specification:**")
    schema_idx = prompt.index("open_questions")
    assert role_idx < rules_idx < context_idx < schema_idx


def test_build_json_output_prompt_omits_empty_rules_and_context_blocks() -> None:
    """Omitting rules/context_blocks (defaults '') produces no stray blank blocks."""
    prompt = build_json_output_prompt(
        role_sentence="You are an expert DevOps Agent.",
        json_schema="- artifacts: object(path -> file_content)",
    )
    assert prompt == (
        "You are an expert DevOps Agent.\n\n"
        "**Output format (JSON only):**\n"
        "- artifacts: object(path -> file_content)\n\n" + JSON_OUTPUT_INSTRUCTION
    )


def test_build_document_rewrite_prompt_default_output_instruction() -> None:
    """Default output_instruction asks for a full plain-text/markdown document."""
    prompt = build_document_rewrite_prompt(
        role_sentence="You are an expert Product Specification Writer.",
        context_blocks=format_context_block("Specification", "{spec_content}"),
    )
    assert "Respond with the FULL updated document as plain text (markdown format)." in prompt
    assert "**Output format (JSON only):**" not in prompt


def test_build_document_rewrite_prompt_output_instruction_override() -> None:
    """Callers can override the output_instruction entirely."""
    prompt = build_document_rewrite_prompt(
        role_sentence="You are an expert Product Specification Writer.",
        output_instruction="Respond with only the updated section as markdown.",
    )
    assert prompt.endswith("Respond with only the updated section as markdown.")
    assert "FULL updated document" not in prompt


def test_build_document_rewrite_prompt_omits_empty_rules_and_context_blocks() -> None:
    """Omitting rules/context_blocks (defaults '') produces no stray blank blocks."""
    prompt = build_document_rewrite_prompt(role_sentence="You are an expert Writer.")
    assert prompt == (
        "You are an expert Writer.\n\n"
        "Respond with the FULL updated document as plain text (markdown format). "
        "Do not wrap in code fences. No explanatory text before or after."
    )
