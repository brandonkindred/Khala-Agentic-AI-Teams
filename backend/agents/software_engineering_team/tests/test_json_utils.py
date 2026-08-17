"""Recovery-ladder tests for the canonical ``json_utils.parse_json_object``.

``parse_json_object`` is the single entrypoint every SE-team JSON-wrapper
function (``product_requirements_analysis_agent.llm_io.parse_llm_json``,
``code_review_agent.synthesis._parse_json_object``) now delegates to. These
tests exercise its recovery ladder directly (fence stripping, prose-prefix
trimming, trailing-comma repair, truncation salvage) plus its own two failure
contracts (``LLMJsonParseError`` on unrecoverable text, ``TypeError`` on a
non-object payload).
"""

from __future__ import annotations

import pytest

from llm_service import LLMJsonParseError
from software_engineering_team.shared.json_utils import parse_json_object


def test_parses_clean_json() -> None:
    assert parse_json_object('{"summary": "ok"}') == {"summary": "ok"}


def test_recovers_markdown_fenced_json() -> None:
    raw = '```json\n{"summary": "ok", "files": {"a.py": "x"}}\n```'
    assert parse_json_object(raw) == {"summary": "ok", "files": {"a.py": "x"}}


def test_recovers_prose_prefixed_json() -> None:
    raw = "Here's the JSON:\n" + '{"summary": "ok"}'
    assert parse_json_object(raw) == {"summary": "ok"}


def test_recovers_trailing_comma() -> None:
    assert parse_json_object('{"summary": "ok", "files": {},}') == {"summary": "ok", "files": {}}


def test_recovers_truncated_object() -> None:
    """A response cut off mid-stream (e.g. by a max-tokens limit) is salvaged
    by fabricating the missing closing brackets, not rejected outright."""
    raw = 'Here is the plan: {"tasks": [{"id": "t1"}, {"id": "t2"'
    result = parse_json_object(raw)
    assert isinstance(result, dict)
    assert result.get("tasks"), "truncated task list should be completed by repair"


def test_unrecoverable_text_raises_llm_json_parse_error() -> None:
    with pytest.raises(LLMJsonParseError):
        parse_json_object("no json anywhere in this response at all")


def test_non_object_payload_raises_type_error() -> None:
    """A top-level JSON array is valid JSON but not the object callers expect."""
    with pytest.raises(TypeError):
        parse_json_object("[1, 2, 3]")
