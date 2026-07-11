"""Tests for the shared ``agent_call_json`` helper and ``parse_code_edits``.

``agent_call_json`` (promoted into ``shared_llm_recovery``) is the resilient
replacement for the ``json.loads(str(agent(prompt)).strip())`` idiom; it must
recover fenced/prose-wrapped JSON and only re-raise when nothing parses.
``parse_code_edits`` deduplicates the ``{"edits": [...]}`` -> ``CodeEdit`` loop
that the Build Fix Specialist and lint-fix agent shared.
"""

from __future__ import annotations

import json

import pytest
from build_fix_specialist.models import CodeEdit, parse_code_edits

from shared_llm_recovery import agent_call_json


class _Agent:
    def __init__(self, reply: str) -> None:
        self._reply = reply

    def __call__(self, prompt: str) -> str:
        return self._reply


def test_agent_call_json_parses_clean_object() -> None:
    out = agent_call_json(_Agent('{"a": 1, "b": "x"}'), "p")
    assert out == {"a": 1, "b": "x"}


def test_agent_call_json_strips_json_fence() -> None:
    out = agent_call_json(_Agent('```json\n{"ok": true}\n```'), "p")
    assert out == {"ok": True}


def test_agent_call_json_recovers_prose_wrapped_object() -> None:
    out = agent_call_json(_Agent('Sure! Here is the result: {"edits": []} — hope that helps'), "p")
    assert out == {"edits": []}


def test_agent_call_json_honors_required_keys() -> None:
    # The usage echo lacks the anchor; the real payload carries it.
    reply = 'example: {"usage": 1}\nanswer: {"approved": true, "notes": "ok"}'
    out = agent_call_json(_Agent(reply), "p", required_keys={"approved"})
    assert out.get("approved") is True


def test_agent_call_json_reraises_when_nothing_parses() -> None:
    with pytest.raises(json.JSONDecodeError):
        agent_call_json(_Agent("no json here at all"), "p")


def test_parse_code_edits_builds_wellformed_edits() -> None:
    data = {
        "edits": [
            {"file_path": "a.py", "old_text": "x", "new_text": "y", "line_start": 3, "line_end": 4},
            {"file_path": "b.py", "old_text": "1", "new_text": "2"},
        ]
    }
    edits = parse_code_edits(data)
    assert len(edits) == 2
    assert all(isinstance(e, CodeEdit) for e in edits)
    assert edits[0].file_path == "a.py" and edits[0].line_start == 3
    assert edits[1].line_start is None


def test_parse_code_edits_skips_malformed_entries() -> None:
    data = {
        "edits": [
            {"file_path": "a.py", "old_text": "x"},  # missing new_text
            {"old_text": "x", "new_text": "y"},  # missing file_path
            "not-a-dict",
            {"file_path": "", "old_text": "x", "new_text": "y"},  # empty file_path
            {"file_path": "ok.py", "old_text": "x", "new_text": "y"},  # valid
        ]
    }
    edits = parse_code_edits(data)
    assert [e.file_path for e in edits] == ["ok.py"]


def test_parse_code_edits_non_dict_and_missing_edits() -> None:
    assert parse_code_edits(["not", "a", "dict"]) == []
    assert parse_code_edits({}) == []
    assert parse_code_edits({"edits": None}) == []
