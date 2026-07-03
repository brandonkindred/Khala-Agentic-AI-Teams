"""Tech Lead ``_agent_call_json`` parses strict JSON and recovers imperfect output."""

from __future__ import annotations

import json

import pytest

from coding_team.tech_lead_agent.agent import _agent_call_json


class _FakeAgent:
    """Minimal Strands-Agent stand-in: calling it returns a canned response."""

    def __init__(self, response: str) -> None:
        self._response = response

    def __call__(self, prompt: str) -> str:
        return self._response


def test_strict_json_parse() -> None:
    assert _agent_call_json(_FakeAgent('{"a": 1}'), "p") == {"a": 1}


def test_strips_markdown_fence() -> None:
    assert _agent_call_json(_FakeAgent('```json\n{"a": 2}\n```'), "p") == {"a": 2}


def test_recovers_prose_wrapped_json() -> None:
    agent = _FakeAgent('Sure! Here is the plan: {"tasks": [{"id": "t1"}]} Done.')
    assert _agent_call_json(agent, "p") == {"tasks": [{"id": "t1"}]}


def test_recovers_from_think_block() -> None:
    assert _agent_call_json(_FakeAgent('<think>reasoning</think>\n{"ok": true}'), "p") == {
        "ok": True
    }


def test_raises_when_unrecoverable() -> None:
    with pytest.raises(json.JSONDecodeError):
        _agent_call_json(_FakeAgent("there is absolutely no json here"), "p")


def test_required_keys_anchor_skips_usage_echo() -> None:
    """When the call site knows its schema, a trailing usage echo that lacks the
    anchor key must not be returned in place of the real (recoverable) payload."""
    agent = _FakeAgent('Verdict: {"approved": false, "issues": ["x"],}\nUsage: {"tokens": 9}')
    assert _agent_call_json(agent, "p", required_keys=("approved",)) == {
        "approved": False,
        "issues": ["x"],
    }
