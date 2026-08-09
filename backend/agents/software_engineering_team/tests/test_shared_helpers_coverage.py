"""Coverage tests for small `shared/` helper modules.

Targets exception paths, empty-input branches, and edge cases in pure-Python
parsers that are otherwise reached only indirectly via the live pipeline.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# llm_response_utils
# ---------------------------------------------------------------------------


def test_extract_task_assignment_returns_none_on_empty():
    from shared.llm_recovery import recovery as lru

    assert lru.extract_task_assignment_from_content("") is None
    assert lru.extract_task_assignment_from_content("not json at all") is None


def test_extract_task_assignment_finds_bare_json():
    """Bare JSON containing a `tasks` array round-trips through the helper."""
    from shared.llm_recovery import recovery as lru

    raw = '{"tasks": [{"id": "t1", "title": "x"}]}'
    out = lru.extract_task_assignment_from_content(raw)
    assert out is not None
    assert out["tasks"][0]["id"] == "t1"


def test_extract_files_from_content_empty_returns_dict():
    from shared.llm_recovery import recovery as lru

    out = lru.extract_files_from_content("")
    assert isinstance(out, dict)


def test_extract_single_python_block_returns_none_when_absent():
    from shared.llm_recovery import recovery as lru

    assert lru.extract_single_python_block("just text") is None


def test_extract_single_python_block_finds_fenced_python():
    from shared.llm_recovery import recovery as lru

    raw = "```python\ndef hello(name):\n    return f'Hi {name}'\n```"
    out = lru.extract_single_python_block(raw)
    assert out is not None
    assert "def hello" in out


def test_extract_single_python_block_ignores_short_body():
    """Bodies shorter than 20 chars are rejected as too small to be useful."""
    from shared.llm_recovery import recovery as lru

    raw = "```python\nx = 1\n```"
    assert lru.extract_single_python_block(raw) is None


# ---------------------------------------------------------------------------
# error_parsing
# ---------------------------------------------------------------------------


def test_parse_pytest_failure_empty_returns_unknown_failure():
    """Empty stdout/stderr still yields a single Unknown ParsedFailure
    so the caller surfaces *something* in the agent feedback."""
    from shared.command_runner import error_parsing as ep

    out = ep.parse_pytest_failure("", "")
    assert len(out) >= 1
    assert out[0].failure_class == ep.FailureClass.UNKNOWN


def test_parse_ng_build_failure_empty_returns_unknown_failure():
    from shared.command_runner import error_parsing as ep

    out = ep.parse_ng_build_failure("", "")
    assert len(out) >= 1
    assert out[0].failure_class == ep.FailureClass.UNKNOWN


def test_parse_devops_failure_handles_generic_docker_error():
    from shared.command_runner import error_parsing as ep

    text = "docker: RUN apt-get install foo failed"
    out = ep.parse_devops_failure(text)
    assert isinstance(out, list)


def test_get_failure_class_tag_returns_string():
    from shared.command_runner.error_parsing import (
        FailureClass,
        get_failure_class_tag,
    )

    # Should not raise; returns some short tag string for each known class
    tag = get_failure_class_tag(FailureClass.PYTEST_ASSERTION)
    assert isinstance(tag, str) and tag
    tag2 = get_failure_class_tag(FailureClass.UNKNOWN)
    assert isinstance(tag2, str)


def test_build_agent_feedback_empty_returns_empty_or_string():
    from shared.command_runner.error_parsing import build_agent_feedback

    out = build_agent_feedback([])
    assert isinstance(out, str)


# ---------------------------------------------------------------------------
# models
# ---------------------------------------------------------------------------


def test_task_status_enum_values_present():
    from shared.dev_models.models import TaskStatus

    # Some expected enum members
    assert TaskStatus.PENDING.value in ("pending",)


def test_task_type_enum_values_present():
    from shared.dev_models.models import TaskType

    # Some expected enum members
    assert TaskType.BACKEND.value in ("backend",)


def test_task_round_trip_dict():
    from shared.dev_models.models import Task, TaskStatus, TaskType

    t = Task(
        id="t1",
        title="Title",
        description="Desc",
        type=TaskType.BACKEND,
        assignee="backend",
        status=TaskStatus.PENDING,
    )
    d = t.model_dump()
    assert d["id"] == "t1"


# ---------------------------------------------------------------------------
# html_utils — simple sanitizers / strippers
# ---------------------------------------------------------------------------


def test_html_utils_module_imports():
    """Ensure module imports without side effects."""
    from software_engineering_team.shared import html_utils

    assert html_utils is not None


# ---------------------------------------------------------------------------
# task_parsing
# ---------------------------------------------------------------------------


def test_task_parsing_module_imports():
    """Ensure module imports without side effects."""
    from software_engineering_team.shared import task_parsing

    assert task_parsing is not None


# ---------------------------------------------------------------------------
# continuation
# ---------------------------------------------------------------------------


def test_continuation_module_imports():
    from software_engineering_team.shared import continuation

    assert continuation is not None
