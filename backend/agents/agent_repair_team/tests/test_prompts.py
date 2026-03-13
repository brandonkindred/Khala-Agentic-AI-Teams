"""Tests for agent_repair_team prompts."""

from agent_repair_team.prompts import REPAIR_PROMPT


def test_repair_prompt_is_string():
    assert isinstance(REPAIR_PROMPT, str)
    assert len(REPAIR_PROMPT) > 100


def test_repair_prompt_contains_json_keyword():
    assert "JSON" in REPAIR_PROMPT or "json" in REPAIR_PROMPT.lower()


def test_repair_prompt_contains_suggested_fixes():
    assert "suggested_fixes" in REPAIR_PROMPT


def test_repair_prompt_references_traceback():
    assert "traceback" in REPAIR_PROMPT.lower()


def test_repair_prompt_no_markdown_fence_instruction():
    assert "No markdown" in REPAIR_PROMPT or "no markdown" in REPAIR_PROMPT.lower()
