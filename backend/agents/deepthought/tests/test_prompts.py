"""Tests for deepthought prompt helpers and contracts.

Preconditions:
    - ``prompts`` module is importable under the agents package path.

Postconditions:
    - Assertions document call-site expectations for prompt shape (prose vs JSON)
      and full specialist-result formatting (no truncation).
"""

from __future__ import annotations

from deepthought.prompts import DELIBERATION_SYSTEM_PROMPT, format_specialist_results


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
