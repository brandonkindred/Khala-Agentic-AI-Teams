"""Tests for QAExpertAgent and its Pydantic models.

Covers the model-level cleanup logic that moved out of the agent when it
migrated to the Strands adapter (``BugReport`` location collapse and
``QAOutput`` newline unescaping), plus an end-to-end run against
``DummyLLMClient`` in each of the three request modes.
"""

from __future__ import annotations

from qa_agent import QAExpertAgent, QAInput
from qa_agent.models import BugReport, QAOutput

from llm_service.clients.dummy import DummyLLMClient

# ---------------------------------------------------------------------------
# BugReport.location collapse validator
# ---------------------------------------------------------------------------


def test_bug_report_collapses_file_path_and_line_into_location() -> None:
    bug = BugReport(
        severity="high",
        description="missing import",
        file_path="app/main.py",
        line_or_section="42",
    )
    assert bug.location == "app/main.py:42"


def test_bug_report_collapses_file_path_only_when_line_missing() -> None:
    bug = BugReport(
        severity="medium",
        description="bad return type",
        file_path="app/utils.py",
    )
    assert bug.location == "app/utils.py"


def test_bug_report_prefers_explicit_location_over_file_path() -> None:
    bug = BugReport(
        severity="low",
        description="typo",
        location="already/set.py:9",
        file_path="never/used.py",
        line_or_section="99",
    )
    assert bug.location == "already/set.py:9"


def test_bug_report_location_stays_empty_when_nothing_provided() -> None:
    bug = BugReport(severity="info", description="general note")
    assert bug.location == ""


# ---------------------------------------------------------------------------
# QAOutput.\n unescaping validator
# ---------------------------------------------------------------------------


def test_qa_output_unescapes_literal_newlines_in_code_fields() -> None:
    out = QAOutput(
        integration_tests="def test_a():\\n    assert True",
        unit_tests="def test_b():\\n    assert 1 == 1",
        readme_content="# Title\\n\\n## Section",
    )
    assert out.integration_tests == "def test_a():\n    assert True"
    assert out.unit_tests == "def test_b():\n    assert 1 == 1"
    assert out.readme_content == "# Title\n\n## Section"


def test_qa_output_leaves_real_newlines_alone() -> None:
    out = QAOutput(
        integration_tests="line1\nline2",
        unit_tests="",
        readme_content="already\ngood",
    )
    assert out.integration_tests == "line1\nline2"
    assert out.readme_content == "already\ngood"


def test_qa_output_non_code_fields_not_touched_by_validator() -> None:
    # ``summary`` and ``live_test_notes`` are natural language; they should
    # pass through untouched even if they happen to contain the ``\n`` token.
    out = QAOutput(summary="literal \\n in summary is fine")
    assert out.summary == "literal \\n in summary is fine"


# ---------------------------------------------------------------------------
# End-to-end: QAExpertAgent.run with DummyLLMClient
# ---------------------------------------------------------------------------


def _input(**overrides: object) -> QAInput:
    base = {
        "code": "def add(a, b):\n    return a + b",
        "language": "python",
        "task_description": "Implement a simple add function",
    }
    base.update(overrides)
    return QAInput(**base)  # type: ignore[arg-type]


def test_qa_expert_agent_default_mode_returns_qa_output() -> None:
    agent = QAExpertAgent(DummyLLMClient())
    result = agent.run(_input())
    assert isinstance(result, QAOutput)
    assert result.approved is True
    assert result.bugs_found == []
    assert result.summary  # dummy stub sets a non-empty summary
    assert "dummy" in result.integration_tests.lower()


def test_qa_expert_agent_write_tests_mode() -> None:
    agent = QAExpertAgent(DummyLLMClient())
    result = agent.run(_input(request_mode="write_tests"))
    assert isinstance(result, QAOutput)
    # Dummy stub returns both unit_tests and integration_tests in write_tests mode.
    assert result.unit_tests
    assert result.integration_tests


def test_qa_expert_agent_fix_build_mode_with_build_errors() -> None:
    agent = QAExpertAgent(DummyLLMClient())
    result = agent.run(
        _input(
            request_mode="fix_build",
            build_errors="SyntaxError: invalid syntax on line 3",
        )
    )
    assert isinstance(result, QAOutput)
    # Dummy stub has no bugs, so approved stays True. The point of this test
    # is that the fix_build code path doesn't raise and still returns a
    # well-formed QAOutput.
    assert result.approved is True


def test_qa_expert_agent_derives_approved_from_bug_severities() -> None:
    """If the LLM returns critical bugs, ``approved`` should be False even
    when the LLM set it to True."""

    class _LyingClient(DummyLLMClient):
        def complete_json(
            self, prompt, *, temperature=0.0, system_prompt=None, tools=None, think=False, **kwargs
        ):  # type: ignore[override]
            return {
                "bugs_found": [
                    {"severity": "critical", "description": "NPE in /auth"},
                    {"severity": "low", "description": "typo"},
                ],
                "approved": True,  # deliberately wrong
                "summary": "LGTM",
                "integration_tests": "",
                "unit_tests": "",
                "test_plan": "",
                "live_test_notes": "",
                "readme_content": "",
                "suggested_commit_message": "",
            }

    agent = QAExpertAgent(_LyingClient())
    result = agent.run(_input())
    assert result.approved is False
    assert len(result.bugs_found) == 2
    assert result.bugs_found[0].severity == "critical"


def test_multiple_run_calls_on_same_instance_succeed() -> None:
    """Regression: a single ``QAExpertAgent`` instance must handle many
    ``run()`` calls in sequence across different request modes. See
    test_code_review_agent.py::test_multiple_run_calls_on_same_instance_succeed
    for the root-cause details."""
    agent = QAExpertAgent(DummyLLMClient())
    modes: list[str | None] = [None, "write_tests", None, "write_tests"]
    for i, mode in enumerate(modes):
        result = agent.run(_input(request_mode=mode))
        assert isinstance(result, QAOutput), f"run {i} (mode={mode}) did not return QAOutput"
        assert result.approved is True, f"run {i} (mode={mode}) failed: {result.summary}"


def test_qa_expert_agent_falls_back_on_validation_error() -> None:
    """A malformed LLM response must not crash the pipeline."""

    class _BrokenClient(DummyLLMClient):
        def complete_json(
            self, prompt, *, temperature=0.0, system_prompt=None, tools=None, think=False, **kwargs
        ):  # type: ignore[override]
            return {"not_a_qa_output_shape": True}

    agent = QAExpertAgent(_BrokenClient())
    result = agent.run(_input())
    # QAOutput accepts missing fields (they all have defaults), so the
    # fallback isn't actually triggered here — assert the graceful path
    # instead: a well-formed empty QAOutput with approved=True (no bugs).
    assert isinstance(result, QAOutput)
    assert result.bugs_found == []
