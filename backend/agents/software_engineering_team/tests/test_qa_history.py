"""Tests for ``product_requirements_analysis_agent.qa_history``.

Covers the multi-line answer/rationale round trip: ``record_answers`` writes
``selected_answer``/``rationale`` verbatim, so a multi-line value produces
continuation lines after the ``**Answer:**``/``**Rationale:**`` markers in
qa_history.md. Both read-back parsers must preserve those continuation lines
instead of truncating to the first line.
"""

from __future__ import annotations

from pathlib import Path

from product_requirements_analysis_agent.models import AnsweredQuestion, OpenQuestion
from product_requirements_analysis_agent.qa_history import (
    extract_answer_from_qa_history,
    parse_qa_history_blocks,
    record_answers,
)


def _open_question(question_text: str) -> OpenQuestion:
    return OpenQuestion(id="q1", question_text=question_text)


def _answered_question(
    question_text: str, selected_answer: str, rationale: str = ""
) -> AnsweredQuestion:
    return AnsweredQuestion(
        question_id="q1",
        question_text=question_text,
        selected_answer=selected_answer,
        rationale=rationale,
    )


# ---------------------------------------------------------------------------
# extract_answer_from_qa_history
# ---------------------------------------------------------------------------


def test_extract_answer_preserves_multiline_answer(tmp_path: Path) -> None:
    question_text = "Which authentication strategy should we use for the API?"
    answer = "Use JWT access tokens.\nRefresh tokens are stored server-side.\nRotate on every use."
    record_answers(tmp_path, [_answered_question(question_text, answer)], iteration=1)

    qa_history = (tmp_path / "plan" / "product_analysis" / "qa_history.md").read_text(
        encoding="utf-8"
    )
    result = extract_answer_from_qa_history(_open_question(question_text), qa_history)

    assert result is not None
    assert result.selected_answer == answer


def test_extract_answer_preserves_multiline_rationale(tmp_path: Path) -> None:
    question_text = "Which database should store session state for the service?"
    answer = "Redis"
    rationale = "Fast in-memory reads.\nBuilt-in TTL support for session expiry."
    record_answers(
        tmp_path,
        [_answered_question(question_text, answer, rationale=rationale)],
        iteration=1,
    )

    qa_history = (tmp_path / "plan" / "product_analysis" / "qa_history.md").read_text(
        encoding="utf-8"
    )
    result = extract_answer_from_qa_history(_open_question(question_text), qa_history)

    assert result is not None
    assert result.rationale == rationale


def test_extract_answer_preserves_bullet_in_multiline_answer_and_rationale(tmp_path: Path) -> None:
    question_text = "What migration strategy should we use for the users table?"
    answer = "Line one\n* Bullet\nLine three"
    rationale = "Rationale one\n* Bullet rationale\nRationale three"
    record_answers(
        tmp_path,
        [_answered_question(question_text, answer, rationale=rationale)],
        iteration=1,
    )

    qa_history = (tmp_path / "plan" / "product_analysis" / "qa_history.md").read_text(
        encoding="utf-8"
    )
    result = extract_answer_from_qa_history(_open_question(question_text), qa_history)

    assert result is not None
    assert result.selected_answer == answer
    assert result.rationale == rationale


def test_extract_answer_preserves_answer_containing_marker_substring(tmp_path: Path) -> None:
    question_text = "How should we document the answer format in the runbook?"
    answer = "The runbook should state **Answer:** followed by the decision text."
    record_answers(tmp_path, [_answered_question(question_text, answer)], iteration=1)

    qa_history = (tmp_path / "plan" / "product_analysis" / "qa_history.md").read_text(
        encoding="utf-8"
    )
    result = extract_answer_from_qa_history(_open_question(question_text), qa_history)

    assert result is not None
    assert result.selected_answer == answer


def test_extract_answer_single_line_still_works() -> None:
    qa_history = (
        "# Q&A History\n\n"
        "## Iteration 1\n\n"
        "### Which logging library should we use for the backend service?\n"
        "**Answer:** structlog\n"
        "**Rationale:** Already used elsewhere in the codebase.\n\n"
    )
    question = _open_question("Which logging library should we use for the backend service?")

    result = extract_answer_from_qa_history(question, qa_history)

    assert result is not None
    assert result.selected_answer == "structlog"
    assert result.rationale == "Already used elsewhere in the codebase."


def test_extract_answer_returns_none_for_no_match() -> None:
    qa_history = (
        "# Q&A History\n\n"
        "## Iteration 1\n\n"
        "### Which logging library should we use for the backend service?\n"
        "**Answer:** structlog\n\n"
    )
    question = _open_question("Should we containerize the frontend build pipeline?")

    assert extract_answer_from_qa_history(question, qa_history) is None


def test_extract_answer_empty_input_returns_none() -> None:
    question = _open_question("Which logging library should we use for the backend service?")

    assert extract_answer_from_qa_history(question, "") is None
    assert extract_answer_from_qa_history(question, "   \n  ") is None


def test_extract_answer_strips_indented_markers() -> None:
    qa_history = (
        "# Q&A History\n\n"
        "## Iteration 1\n\n"
        "### Which cache eviction policy should the session store use?\n"
        "  **Answer:** LRU with a 30 minute TTL.\n"
        "  **Rationale:** Bounds memory while matching session expectations.\n\n"
    )
    question = _open_question("Which cache eviction policy should the session store use?")

    result = extract_answer_from_qa_history(question, qa_history)

    assert result is not None
    assert result.selected_answer == "LRU with a 30 minute TTL."
    assert result.rationale == "Bounds memory while matching session expectations."


# ---------------------------------------------------------------------------
# parse_qa_history_blocks
# ---------------------------------------------------------------------------


def test_parse_qa_history_blocks_preserves_multiline_answer() -> None:
    qa_history = (
        "# Q&A History\n\n"
        "## Iteration 1\n\n"
        "### What retry policy should the payment worker use?\n"
        "**Answer:** Exponential backoff starting at 1s.\n"
        "Cap retries at 5 attempts.\n"
        "**Rationale:** Avoids overwhelming the downstream payment gateway.\n\n"
    )

    blocks = parse_qa_history_blocks(qa_history)

    assert len(blocks) == 1
    iteration, question_text, answer, _full_block = blocks[0]
    assert iteration == 1
    assert question_text == "What retry policy should the payment worker use?"
    assert answer == "Exponential backoff starting at 1s.\nCap retries at 5 attempts."


def test_parse_qa_history_blocks_preserves_bullet_in_multiline_answer() -> None:
    qa_history = (
        "# Q&A History\n\n"
        "## Iteration 1\n\n"
        "### What rollout plan should the migration use?\n"
        "**Answer:** Roll out in three phases.\n"
        "* Phase one: canary\n"
        "Phase two: full rollout\n"
        "**Rationale:** Reduces blast radius of a bad migration.\n\n"
    )

    blocks = parse_qa_history_blocks(qa_history)

    assert len(blocks) == 1
    _iteration, _question_text, answer, _full_block = blocks[0]
    assert answer == "Roll out in three phases.\n* Phase one: canary\nPhase two: full rollout"


def test_parse_qa_history_blocks_strips_indented_answer_marker() -> None:
    qa_history = (
        "# Q&A History\n\n"
        "## Iteration 1\n\n"
        "### Should the answer marker line be allowed to be indented?\n"
        "  **Answer:** Yes, whitespace before the marker is tolerated.\n\n"
    )

    blocks = parse_qa_history_blocks(qa_history)

    assert len(blocks) == 1
    _iteration, _question_text, answer, _full_block = blocks[0]
    assert answer == "Yes, whitespace before the marker is tolerated."


def test_parse_qa_history_blocks_preserves_answer_containing_marker_substring() -> None:
    qa_history = (
        "# Q&A History\n\n"
        "## Iteration 1\n\n"
        "### How should the runbook document answers?\n"
        "**Answer:** Prefix every entry with **Answer:** before the decision text.\n\n"
    )

    blocks = parse_qa_history_blocks(qa_history)

    assert len(blocks) == 1
    _iteration, _question_text, answer, _full_block = blocks[0]
    assert answer == "Prefix every entry with **Answer:** before the decision text."


def test_parse_qa_history_blocks_preserves_multiline_rationale_in_full_block() -> None:
    qa_history = (
        "# Q&A History\n\n"
        "## Iteration 1\n\n"
        "### What retry policy should the payment worker use?\n"
        "**Answer:** Exponential backoff.\n"
        "**Rationale:** First line of rationale.\n"
        "Second line of rationale.\n\n"
    )

    blocks = parse_qa_history_blocks(qa_history)

    assert len(blocks) == 1
    _iteration, _question_text, _answer, full_block = blocks[0]
    assert "First line of rationale.\nSecond line of rationale." in full_block


def test_parse_qa_history_blocks_single_line_still_works() -> None:
    qa_history = (
        "# Q&A History\n\n"
        "## Iteration 2\n\n"
        "### Should the export job run nightly?\n"
        "**Answer:** Yes, at 02:00 UTC.\n\n"
    )

    blocks = parse_qa_history_blocks(qa_history)

    assert len(blocks) == 1
    iteration, question_text, answer, _full_block = blocks[0]
    assert iteration == 2
    assert question_text == "Should the export job run nightly?"
    assert answer == "Yes, at 02:00 UTC."


def test_parse_qa_history_blocks_empty_input_returns_empty_list() -> None:
    assert parse_qa_history_blocks("") == []
    assert parse_qa_history_blocks("   \n  ") == []
