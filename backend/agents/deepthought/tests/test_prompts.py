"""Tests for deepthought prompt helpers."""

from __future__ import annotations

from deepthought.prompts import format_specialist_results

_TRUNCATION_SUFFIX = "\n\n[truncated]"


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


def test_format_specialist_results_truncates_long_answer():
    max_chars = 50
    long_answer = "A" * (max_chars + 100)
    out = format_specialist_results([_result(answer=long_answer)], max_chars_per_result=max_chars)

    assert long_answer not in out
    assert "[truncated]" in out
    expected_body = long_answer[:max_chars] + _TRUNCATION_SUFFIX
    assert expected_body in out
    assert len(expected_body) == max_chars + len(_TRUNCATION_SUFFIX)


def test_format_specialist_results_leaves_short_answer():
    max_chars = 50
    short_answer = "Short enough"
    exact_answer = "B" * max_chars
    out = format_specialist_results(
        [_result(answer=short_answer), _result(answer=exact_answer, agent_name="exact")],
        max_chars_per_result=max_chars,
    )

    assert short_answer in out
    assert exact_answer in out
    assert "[truncated]" not in out


def test_format_specialist_results_truncates_each_result_independently():
    max_chars = 40
    a = "X" * (max_chars + 20)
    b = "Y" * (max_chars + 30)
    out = format_specialist_results(
        [_result(answer=a, agent_name="one"), _result(answer=b, agent_name="two")],
        max_chars_per_result=max_chars,
    )

    assert a not in out
    assert b not in out
    assert out.count("[truncated]") == 2
    assert (a[:max_chars] + _TRUNCATION_SUFFIX) in out
    assert (b[:max_chars] + _TRUNCATION_SUFFIX) in out
