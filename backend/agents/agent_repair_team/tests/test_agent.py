"""Tests for RepairExpertAgent."""

import json
from pathlib import Path
from unittest.mock import MagicMock

from agent_repair_team.agent import RepairExpertAgent
from agent_repair_team.models import RepairInput, RepairOutput


def _make_input(**kwargs):
    defaults = dict(
        traceback="Traceback (most recent call last):\n  File 'foo.py', line 1\nNameError: bar",
        exception_type="NameError",
        exception_message="name 'bar' is not defined",
        task_id="task-001",
        agent_type="backend",
        agent_source_path=Path("/tmp/se"),
    )
    defaults.update(kwargs)
    return RepairInput(**defaults)


def _make_llm(response):
    llm = MagicMock()
    llm.complete_json.return_value = response
    return llm


def test_agent_initializes_with_llm_client():
    llm = MagicMock()
    agent = RepairExpertAgent(llm_client=llm)
    assert agent.llm is llm


def test_run_returns_repair_output():
    llm = _make_llm({"suggested_fixes": [], "summary": "All good"})
    agent = RepairExpertAgent(llm_client=llm)
    result = agent.run(_make_input())
    assert isinstance(result, RepairOutput)
    assert result.applied is False


def test_run_passes_traceback_to_llm():
    llm = _make_llm({"suggested_fixes": [], "summary": ""})
    agent = RepairExpertAgent(llm_client=llm)
    inp = _make_input(traceback="unique-traceback-string-xyz")
    agent.run(inp)
    call_args = llm.complete_json.call_args[0][0]
    assert "unique-traceback-string-xyz" in call_args


def test_run_returns_empty_fixes_on_empty_response():
    llm = _make_llm({"suggested_fixes": [], "summary": "Nothing to fix"})
    agent = RepairExpertAgent(llm_client=llm)
    result = agent.run(_make_input())
    assert result.suggested_fixes == []
    assert result.summary == "Nothing to fix"


def test_run_propagates_summary():
    llm = _make_llm({"suggested_fixes": [], "summary": "Fixed import error"})
    agent = RepairExpertAgent(llm_client=llm)
    result = agent.run(_make_input())
    assert result.summary == "Fixed import error"


def test_run_with_multiple_fixes():
    fixes = [
        {"file_path": "a.py", "line_start": 1, "line_end": 1, "replacement_content": "import x\n"},
        {"file_path": "b.py", "line_start": 3, "line_end": 3, "replacement_content": "y = 2\n"},
    ]
    llm = _make_llm({"suggested_fixes": fixes, "summary": "Two fixes"})
    agent = RepairExpertAgent(llm_client=llm)
    result = agent.run(_make_input())
    assert len(result.suggested_fixes) == 2
    assert result.summary == "Two fixes"


def test_run_invalid_json_raises_or_returns_empty():
    llm = MagicMock()
    llm.complete_json.side_effect = ValueError("bad json")
    agent = RepairExpertAgent(llm_client=llm)
    result = agent.run(_make_input())
    # Should not raise, should return empty output on failure
    assert isinstance(result, RepairOutput)
    assert result.suggested_fixes == []
    assert "failed" in result.summary.lower()


def test_run_non_list_fixes_becomes_empty():
    # If LLM returns suggested_fixes as non-list, it should be normalized to []
    llm = _make_llm({"suggested_fixes": "not-a-list", "summary": "odd"})
    agent = RepairExpertAgent(llm_client=llm)
    result = agent.run(_make_input())
    assert result.suggested_fixes == []


def test_run_string_json_response_is_parsed():
    payload = json.dumps({"suggested_fixes": [], "summary": "from string"})
    llm = _make_llm(payload)
    agent = RepairExpertAgent(llm_client=llm)
    result = agent.run(_make_input())
    assert result.summary == "from string"
