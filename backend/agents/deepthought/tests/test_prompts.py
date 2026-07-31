"""Tests for deepthought prompt helpers and contracts.

Preconditions:
    - ``prompts`` module is importable under the agents package path.

Postconditions:
    - Assertions document call-site expectations for prompt shape (prose vs JSON)
      and specialist-result truncation.
"""

from __future__ import annotations

from deepthought.prompts import DELIBERATION_SYSTEM_PROMPT, format_specialist_results

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


def test_deliberation_system_prompt_asks_for_structured_prose_not_json():
    """Deliberation notes are returned via ``complete()`` as prose for synthesis.

    Preconditions:
        - ``DELIBERATION_SYSTEM_PROMPT`` is the system prompt for ``_deliberate``.

    Postconditions:
        - Prompt requests structured prose and does not instruct JSON object output.
    """
    assert "structured prose" in DELIBERATION_SYSTEM_PROMPT
    assert "not JSON" in DELIBERATION_SYSTEM_PROMPT
    assert "produce a JSON object" not in DELIBERATION_SYSTEM_PROMPT
    for topic in (
        "Contradictions",
        "Gaps",
        "Agreements",
        "Quality flags",
        "Synthesis guidance",
    ):
        assert topic in DELIBERATION_SYSTEM_PROMPT


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
