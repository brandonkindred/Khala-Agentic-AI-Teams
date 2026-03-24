"""Unit tests for shared prompt utilities."""

import logging

import pytest

from software_engineering_team.shared.prompt_utils import (
    build_problem_solving_header,
    log_llm_prompt,
)


def test_build_problem_solving_header_includes_issue_summary() -> None:
    """build_problem_solving_header includes summary of issue counts."""
    header = build_problem_solving_header(
        {"QA issues": 2, "code review issues": 1},
        "Backend",
    )
    assert "PROBLEM-SOLVING MODE" in header
    assert "Backend" in header
    assert "QA issues: 2" in header
    assert "code review issues: 1" in header
    assert "Identify the likely root cause" in header
    assert "using the issue details in this section" in header
    assert "error details above" not in header


def test_build_problem_solving_header_skips_zero_counts() -> None:
    """build_problem_solving_header omits issue types with zero count."""
    header = build_problem_solving_header(
        {"QA issues": 0, "security issues": 1},
        "Backend",
    )
    assert "QA issues" not in header
    assert "security issues: 1" in header


def test_build_problem_solving_header_custom_instructions() -> None:
    """build_problem_solving_header uses custom instructions when provided."""
    custom = "Step 1: Read error. Step 2: Fix it."
    header = build_problem_solving_header(
        {"build errors": 1},
        "Frontend",
        instructions=custom,
    )
    assert custom in header
    assert "Identify the likely root cause" not in header


def test_build_problem_solving_header_includes_issue_descriptions() -> None:
    """build_problem_solving_header includes issue descriptions when provided."""
    descriptions = "  - Code review [style]: Missing error handling (file: src/checkout.ts)"
    header = build_problem_solving_header(
        {"code review issues": 1},
        "Frontend / Angular",
        issue_descriptions=descriptions,
    )
    assert "The following issues were reported:" in header
    assert "code review issues: 1" in header
    assert "Missing error handling" in header
    assert "src/checkout.ts" in header


def test_build_problem_solving_header_includes_test_failure_and_401_instructions() -> None:
    """Default instructions include test-failure and 401/auth guidance."""
    header = build_problem_solving_header(
        {"code review issues": 1},
        "Backend",
    )
    assert "For test failures" in header
    assert "Failing tests" in header or "Interpretation" in header
    assert "expected 200, got 401" in header
    assert "auth" in header.lower()
    assert "Do not change unrelated files or tests" in header or "unrelated files" in header


def test_log_llm_prompt_emits_info_record(caplog: pytest.LogCaptureFixture) -> None:
    """log_llm_prompt emits an INFO log record with agent, mode, and prompt_len."""
    caplog.set_level(logging.INFO)
    log = logging.getLogger("test_prompt_utils")
    log_llm_prompt(log, "TestAgent", "initial", "Add feature", "Short prompt")
    assert any("LLM call" in rec.message for rec in caplog.records)
    assert any("agent=TestAgent" in rec.message for rec in caplog.records)
    assert any("mode=initial" in rec.message for rec in caplog.records)
    assert any("prompt_len=" in rec.message for rec in caplog.records)


def test_log_llm_prompt_logs_metadata_only_for_long_prompt(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """log_llm_prompt logs metadata only (agent, mode, task, prompt_len); no prompt body."""
    caplog.set_level(logging.INFO)
    log = logging.getLogger("test_prompt_utils")
    long_prompt = "x" * 5000
    log_llm_prompt(log, "TestAgent", "problem_solving", "Fix", long_prompt)
    record = next(
        r
        for r in caplog.records
        if "agent=TestAgent" in r.message and "problem_solving" in r.message
    )
    assert "prompt_len=5000" in record.message
    assert "x" * 100 not in record.message  # No prompt body in log


def test_log_llm_prompt_initial_mode_omits_body(caplog: pytest.LogCaptureFixture) -> None:
    """log_llm_prompt with mode=initial logs metadata only, not the prompt body."""
    caplog.set_level(logging.INFO)
    log = logging.getLogger("test_prompt_utils")
    body = "This is the secret prompt body that should not appear in logs"
    log_llm_prompt(log, "TestAgent", "initial", "Task", body)
    record = next(
        r for r in caplog.records if "agent=TestAgent" in r.message and "mode=initial" in r.message
    )
    assert "secret prompt body" not in record.message
    assert "prompt_len=" in record.message


def test_log_llm_prompt_handles_none_gracefully(caplog: pytest.LogCaptureFixture) -> None:
    """log_llm_prompt does not raise when prompt is None."""
    caplog.set_level(logging.INFO)
    log = logging.getLogger("test_prompt_utils")
    log_llm_prompt(log, "TestAgent", "initial", "Task", None)
    assert any(
        "LLM call" in rec.message and "agent=TestAgent" in rec.message for rec in caplog.records
    )
