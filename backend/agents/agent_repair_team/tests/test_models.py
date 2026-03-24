"""Tests for agent_repair_team models."""

from pathlib import Path

from agent_repair_team.models import RepairInput, RepairOutput


def test_repair_input_required_fields():
    inp = RepairInput(
        traceback="Traceback (most recent call last):\n  ...\nNameError: foo",
        exception_type="NameError",
        exception_message="name 'foo' is not defined",
        task_id="task-123",
        agent_type="backend",
        agent_source_path=Path("/tmp/se_team"),
    )
    assert inp.traceback.startswith("Traceback")
    assert inp.exception_type == "NameError"
    assert inp.task_id == "task-123"
    assert inp.agent_type == "backend"


def test_repair_output_defaults():
    out = RepairOutput()
    assert out.applied is False
    assert out.suggested_fixes == []
    assert out.summary == ""


def test_repair_output_with_fixes():
    fix = {
        "file_path": "agents/foo.py",
        "line_start": 10,
        "line_end": 10,
        "replacement_content": "x = 1\n",
    }
    out = RepairOutput(suggested_fixes=[fix], summary="Fixed NameError", applied=True)
    assert len(out.suggested_fixes) == 1
    assert out.suggested_fixes[0]["file_path"] == "agents/foo.py"
    assert out.summary == "Fixed NameError"
    assert out.applied is True


def test_repair_input_field_types():
    inp = RepairInput(
        traceback="tb",
        exception_type="ImportError",
        exception_message="No module named 'foo'",
        task_id="t1",
        agent_type="frontend",
        agent_source_path=Path("/workspace"),
    )
    assert isinstance(inp.agent_source_path, Path)
    assert inp.agent_type == "frontend"


def test_repair_output_multiple_fixes():
    fixes = [
        {"file_path": "a.py", "line_start": 1, "line_end": 1, "replacement_content": "import x\n"},
        {
            "file_path": "b.py",
            "line_start": 5,
            "line_end": 7,
            "replacement_content": "def foo():\n    pass\n",
        },
    ]
    out = RepairOutput(suggested_fixes=fixes, summary="Two fixes applied")
    assert len(out.suggested_fixes) == 2
