"""Unit tests for the Repair Expert agent and crash handling.

The orchestrator-worker-thread tests at the bottom of this file talk to the
job store from background threads; the autouse ``_autouse_patched_job_store``
fixture below applies the ``_client`` monkeypatch before threads spawn, so
every write lands in the in-memory ``FakeJobServiceClient``.
"""

import json
from pathlib import Path
from unittest.mock import patch

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


def test_log_agent_crash_banner_does_not_raise() -> None:
    """_log_agent_crash_banner logs without raising."""
    import orchestrator

    try:
        raise ValueError("test crash")
    except ValueError as e:
        orchestrator._log_agent_crash_banner("task-1", "backend", e, "")


def test_log_agent_crash_banner_logs_error_with_task_and_exception() -> None:
    """_log_agent_crash_banner logs at ERROR level with task_id and exception info."""
    import orchestrator

    error_calls = []
    original_error = orchestrator.logger.error

    def capture_error(msg, *args, **kwargs):
        error_calls.append((msg, args, kwargs))
        original_error(msg, *args, **kwargs)

    try:
        raise NameError("undefined_var")
    except NameError as e:
        with patch.object(orchestrator.logger, "error", side_effect=capture_error):
            orchestrator._log_agent_crash_banner("backend-task-1", "backend", e, "")
    assert len(error_calls) >= 3
    all_text = " ".join(str(c[0]) + " " + " ".join(str(a) for a in c[1]) for c in error_calls)
    assert "backend-task-1" in all_text
    assert "NameError" in all_text or "undefined_var" in all_text


def test_apply_repair_fixes_applies_valid_fix(tmp_path: Path) -> None:
    """_apply_repair_fixes applies a valid fix and returns True."""
    import orchestrator

    target_file = tmp_path / "test_file.py"
    target_file.write_text("line1\nline2\nline3\nline4\nline5\n")
    suggested_fixes = [
        {
            "file_path": str(target_file.name),
            "line_start": 2,
            "line_end": 2,
            "replacement_content": "fixed\n",
        }
    ]
    # agent_source_path is tmp_path so target is tmp_path/test_file.py
    applied = orchestrator._apply_repair_fixes(tmp_path, suggested_fixes)
    assert applied
    content = target_file.read_text()
    assert "fixed" in content
    assert "line2" not in content


def test_apply_repair_fixes_rejects_path_outside_tree(tmp_path: Path) -> None:
    """_apply_repair_fixes rejects paths outside agent_source_path."""
    import orchestrator

    agent_root = tmp_path / "software_engineering_team"
    agent_root.mkdir()
    (agent_root / "backend_agent").mkdir(parents=True)
    target = agent_root / "backend_agent" / "agent.py"
    target.write_text("x = 1\n")
    # Try to fix a path that resolves outside agent_root
    suggested_fixes = [
        {
            "file_path": "../../../etc/passwd",
            "line_start": 1,
            "line_end": 1,
            "replacement_content": "evil\n",
        }
    ]
    applied = orchestrator._apply_repair_fixes(agent_root, suggested_fixes)
    assert not applied
