"""Unit tests for the Repair Expert agent and crash handling.

The orchestrator-worker-thread tests at the bottom of this file talk to the
job store from background threads; the autouse ``_autouse_patched_job_store``
fixture below applies the ``_client`` monkeypatch before threads spawn, so
every write lands in the in-memory ``FakeJobServiceClient``.
"""

import json
from pathlib import Path

import pytest

from agent_repair_team.agent import RepairExpertAgent
from agent_repair_team.models import RepairInput


@pytest.fixture(autouse=True)
def _autouse_patched_job_store(patched_job_store):
    return patched_job_store


class _FakeAgentResult:
    """Wraps a dict so ``str(result)`` returns a JSON string.

    ``RepairExpertAgent`` stores its ``llm_client`` as ``self._agent`` and
    calls ``self._agent(prompt)``; the agent then does ``str(result).strip()``
    and ``json.loads(raw)``. This class satisfies that contract without
    requiring a live LLM.
    """

    def __init__(self, data: dict) -> None:
        self._data = data

    def __str__(self) -> str:
        return json.dumps(self._data)


class _CallableLLMStub:
    """A callable that returns ``_FakeAgentResult`` wrapping canned data."""

    def __init__(self, response: dict) -> None:
        self._response = response

    def __call__(self, prompt, **kwargs):
        return _FakeAgentResult(self._response)


def test_repair_agent_suggests_import_fix_for_name_error() -> None:
    """Repair agent suggests an import fix for NameError traceback."""
    traceback_str = """Traceback (most recent call last):
  File "software_engineering_team/backend_agent/agent.py", line 407, in _plan_task
    x = compute_spec_content_chars(spec)
NameError: name 'compute_spec_content_chars' is not defined
"""
    mock_llm = _CallableLLMStub(
        {
            "suggested_fixes": [
                {
                    "file_path": "backend_agent/agent.py",
                    "line_start": 1,
                    "line_end": 15,
                    "replacement_content": "from software_engineering_team.shared.context_sizing import compute_existing_code_chars, compute_spec_content_chars\n",
                }
            ],
            "summary": "Added missing import for compute_spec_content_chars",
        }
    )
    agent = RepairExpertAgent(llm_client=mock_llm)
    result = agent.run(
        RepairInput(
            traceback=traceback_str,
            exception_type="NameError",
            exception_message="name 'compute_spec_content_chars' is not defined",
            task_id="backend-task-1",
            agent_type="backend",
            agent_source_path=Path(__file__).resolve().parent.parent,
        )
    )
    assert result.suggested_fixes
    assert len(result.suggested_fixes) == 1
    fix = result.suggested_fixes[0]
    assert "compute_spec_content_chars" in fix.get("replacement_content", "")
    assert fix.get("file_path") == "backend_agent/agent.py"
    assert result.summary


def test_repair_agent_returns_empty_when_no_fix() -> None:
    """Repair agent returns empty suggested_fixes when it cannot determine a fix."""
    mock_llm = _CallableLLMStub(
        {
            "suggested_fixes": [],
            "summary": "Unable to determine fix: ambiguous error",
        }
    )
    agent = RepairExpertAgent(llm_client=mock_llm)
    result = agent.run(
        RepairInput(
            traceback="Traceback...",
            exception_type="RuntimeError",
            exception_message="Something went wrong",
            task_id="task-1",
            agent_type="backend",
            agent_source_path=Path(__file__).resolve().parent.parent,
        )
    )
    assert not result.suggested_fixes
    assert result.summary


def test_parse_traceback_for_crash_extracts_location() -> None:
    """_parse_traceback_for_crash extracts file_path, line_number, function_name."""
    import orchestrator

    try:
        raise NameError("test")
    except NameError as e:
        file_path, line_number, func_name = orchestrator._parse_traceback_for_crash(e)
    assert file_path
    assert "test_repair_agent" in str(file_path) or "orchestrator" in str(file_path)
    assert line_number is not None
    assert isinstance(line_number, int)
