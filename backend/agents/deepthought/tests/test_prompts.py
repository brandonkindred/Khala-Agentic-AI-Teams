"""Tests for deepthought prompt helpers and contracts.

Preconditions:
    - ``prompts`` module is importable under the agents package path.

Postconditions:
    - Assertions document full specialist-result formatting (no truncation).
"""

from __future__ import annotations

from deepthought.prompts import format_specialist_results


def _result(
    *,
    answer: str,
    agent_name: str = "analyst",
    focus_question: str = "What happened?",
    confidence: float = 0.8,
) -> dict:
    return {
        "answer": answer,
        "agent_name": agent_name,
        "focus_question": focus_question,
        "confidence": confidence,
    }


def test_format_specialist_results_preserves_long_answer_verbatim() -> None:
    long_answer = "A" * 5000
    out = format_specialist_results([_result(answer=long_answer)])

    assert long_answer in out
    assert "[truncated]" not in out
    assert " ..." not in out
    assert "### Specialist 1: analyst" in out
    assert "**Focus:** What happened?" in out
    assert "**Confidence:** 80%" in out


def test_format_specialist_results_preserves_each_result_independently() -> None:
    a = "X" * 4000
    b = "Y" * 4500
    out = format_specialist_results(
        [_result(answer=a, agent_name="one"), _result(answer=b, agent_name="two")],
    )

    assert a in out
    assert b in out
    assert "\n\n---\n\n" in out
    assert "### Specialist 1: one" in out
    assert "### Specialist 2: two" in out
    assert "[truncated]" not in out
