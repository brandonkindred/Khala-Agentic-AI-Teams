"""Tests for ``product_requirements_analysis_agent.qa_history``.

Covers the multi-line answer/rationale round trip: ``record_answers`` writes
``selected_answer``/``rationale`` verbatim, so a multi-line value produces
continuation lines after the ``**Answer:**``/``**Rationale:**`` markers in
qa_history.md. Both read-back parsers must preserve those continuation lines
instead of truncating to the first line.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from product_requirements_analysis_agent.models import AnsweredQuestion, OpenQuestion
from product_requirements_analysis_agent.qa_history import (
    extract_answer_from_qa_history,
    format_answered_questions_for_prompt,
    is_same_decision,
    parse_qa_history_blocks,
    record_answers,
)


def _open_question(question_text: str) -> OpenQuestion:
    """Build a minimal OpenQuestion fixture with a fixed id, varying only the question text."""
    return OpenQuestion(id="q1", question_text=question_text)


def _answered_question(
    question_text: str,
    selected_answer: str,
    rationale: str = "",
    other_text: str = "",
    *,
    was_auto_answered: bool = False,
    was_default: bool = False,
    confidence: float = 0.0,
) -> AnsweredQuestion:
    """Build a minimal AnsweredQuestion fixture with a fixed id, for feeding into record_answers."""
    return AnsweredQuestion(
        question_id="q1",
        question_text=question_text,
        selected_answer=selected_answer,
        rationale=rationale,
        other_text=other_text,
        was_auto_answered=was_auto_answered,
        was_default=was_default,
        confidence=confidence,
    )


# ---------------------------------------------------------------------------
# extract_answer_from_qa_history
# ---------------------------------------------------------------------------


def test_extract_answer_preserves_multiline_answer(tmp_path: Path) -> None:
    """A multi-line selected_answer round-trips intact through record_answers and read-back."""
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
    """A multi-line rationale round-trips intact through record_answers and read-back."""
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
    """A Markdown bullet mid-value is not treated as a section boundary and is preserved as part of the buffered answer."""
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
    """The literal substring '**Answer:**' mid-line (not at line start) isn't stripped."""
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
    """A plain single-line answer/rationale block (the common case) still parses correctly."""
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
    """A question with no matching recorded block returns None instead of a false match."""
    qa_history = (
        "# Q&A History\n\n"
        "## Iteration 1\n\n"
        "### Which logging library should we use for the backend service?\n"
        "**Answer:** structlog\n\n"
    )
    question = _open_question("Should we containerize the frontend build pipeline?")

    assert extract_answer_from_qa_history(question, qa_history) is None


def test_extract_answer_empty_input_returns_none() -> None:
    """Empty or whitespace-only qa_history content returns None rather than raising."""
    question = _open_question("Which logging library should we use for the backend service?")

    assert extract_answer_from_qa_history(question, "") is None
    assert extract_answer_from_qa_history(question, "   \n  ") is None


def test_extract_answer_finds_block_when_file_starts_with_question_header() -> None:
    """A qa_history that begins with '### question' (no file preamble) still extracts the answer.

    The old re.split + blocks[1:] path discarded the first split piece as a
    presumed file header, so a leading ### block was silently skipped.
    """
    qa_history = (
        "### Which caching library should the session store use?\n"
        "**Answer:** Redis with TTL eviction.\n"
        "**Rationale:** Matches existing infra.\n"
    )
    question = _open_question("Which caching library should the session store use?")

    result = extract_answer_from_qa_history(question, qa_history)

    assert result is not None
    assert result.selected_answer == "Redis with TTL eviction."
    assert result.rationale == "Matches existing infra."


def test_extract_answer_preserves_answer_line_matching_question_header(tmp_path: Path) -> None:
    """A continuation line that looks like a '### question' header is escaped and preserved."""
    question_text = "What approach should we use for structuring the docs?"
    answer = "Use Markdown.\n### Details\nMore info below."
    record_answers(tmp_path, [_answered_question(question_text, answer)], iteration=1)

    qa_history = (tmp_path / "plan" / "product_analysis" / "qa_history.md").read_text(
        encoding="utf-8"
    )
    result = extract_answer_from_qa_history(_open_question(question_text), qa_history)

    assert result is not None
    assert result.selected_answer == answer


def test_extract_answer_preserves_answer_line_matching_iteration_header(tmp_path: Path) -> None:
    """A continuation line that looks like a '## Iteration' header is escaped and preserved."""
    question_text = "How should the changelog be organized?"
    answer = "By release.\n## Iteration notes\nSee below for details."
    record_answers(tmp_path, [_answered_question(question_text, answer)], iteration=1)

    qa_history = (tmp_path / "plan" / "product_analysis" / "qa_history.md").read_text(
        encoding="utf-8"
    )
    result = extract_answer_from_qa_history(_open_question(question_text), qa_history)

    assert result is not None
    assert result.selected_answer == answer


def test_extract_answer_preserves_line_matching_status_marker(tmp_path: Path) -> None:
    """A continuation line starting with '*Auto-answered' as plain content is escaped and preserved."""
    question_text = "What disclaimer text should ship with auto-generated answers?"
    answer = (
        "Show this notice.\n*Auto-answered previously, verify before relying on it.\nEnd of notice."
    )
    record_answers(tmp_path, [_answered_question(question_text, answer)], iteration=1)

    qa_history = (tmp_path / "plan" / "product_analysis" / "qa_history.md").read_text(
        encoding="utf-8"
    )
    result = extract_answer_from_qa_history(_open_question(question_text), qa_history)

    assert result is not None
    assert result.selected_answer == answer


def test_extract_answer_preserves_interior_line_trailing_whitespace(tmp_path: Path) -> None:
    """Trailing whitespace on an interior (non-first, non-last) continuation line survives verbatim.

    Only the first line (stripped after its marker is removed) and the last line
    (via the final ``.strip()`` on the joined value) lose surrounding whitespace;
    a truly interior line's own trailing spaces are part of the value and must
    round-trip exactly, not be silently trimmed.
    """
    question_text = "Does interior trailing whitespace round-trip?"
    answer = "Line one.\nMiddle line trailing spaces.   \nLine three."
    record_answers(tmp_path, [_answered_question(question_text, answer)], iteration=1)

    qa_history = (tmp_path / "plan" / "product_analysis" / "qa_history.md").read_text(
        encoding="utf-8"
    )
    result = extract_answer_from_qa_history(_open_question(question_text), qa_history)

    assert result is not None
    assert result.selected_answer == answer


def test_extract_answer_preserves_indented_marker_lookalike_continuation_line(
    tmp_path: Path,
) -> None:
    """A continuation line that is itself an indented '**Answer:**'-like string round-trips intact.

    _format_field_value escapes this at write time (it matches _is_boundary_line
    regardless of indentation), so the read side never has to guess whether an
    indented marker-shaped line is genuine structure or literal content.
    """
    question_text = "Does indented marker-lookalike content round-trip?"
    answer = "Line one.\n  **Answer:** indented fake marker as content\nLine three."
    record_answers(tmp_path, [_answered_question(question_text, answer)], iteration=1)

    qa_history = (tmp_path / "plan" / "product_analysis" / "qa_history.md").read_text(
        encoding="utf-8"
    )
    result = extract_answer_from_qa_history(_open_question(question_text), qa_history)

    assert result is not None
    assert result.selected_answer == answer


def test_extract_answer_strips_indented_markers() -> None:
    """A genuinely indented '**Answer:**'/'**Rationale:**' marker line (not escaped) still parses.

    Tolerating indentation on real markers (as opposed to escaped look-alike
    content) matters for hand-edited or externally-produced qa_history.md files.
    """
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
    """The parsed answer field spans multiple lines when the source block does."""
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
    """A Markdown bullet mid-answer is not treated as a section boundary and is preserved in the parsed answer."""
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
    """An indented '**Answer:**' marker line is still recognized and its prefix stripped."""
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
    """The literal substring '**Answer:**' mid-line (not at line start) isn't stripped."""
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
    """A multi-line rationale isn't extracted as a separate field but survives in full_block_text.

    parse_qa_history_blocks doesn't structurally return the rationale (nothing
    consumes it), but record_answers relies on full_block_text to reproduce the
    block verbatim on rewrite, so it must retain every rationale line unmodified.
    """
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
    _iteration, _question_text, answer, full_block = blocks[0]
    # The rationale continuation must not be accidentally buffered into the
    # parsed answer field; only full_block_text preserves rationale verbatim.
    assert answer == "Exponential backoff."
    assert "First line of rationale.\nSecond line of rationale." not in answer
    assert "First line of rationale.\nSecond line of rationale." in full_block


def test_parse_qa_history_blocks_does_not_split_on_embedded_question_header(
    tmp_path: Path,
) -> None:
    """An answer containing a '### '-prefixed line doesn't get split into two bogus blocks."""
    question_text = "What approach should we use for structuring the docs?"
    answer = "Use Markdown.\n### Details\nMore info below."
    record_answers(tmp_path, [_answered_question(question_text, answer)], iteration=1)

    qa_history = (tmp_path / "plan" / "product_analysis" / "qa_history.md").read_text(
        encoding="utf-8"
    )
    blocks = parse_qa_history_blocks(qa_history)

    assert len(blocks) == 1
    _iteration, question_text_out, answer_out, _full_block = blocks[0]
    assert question_text_out == question_text
    assert answer_out == answer


def test_parse_qa_history_blocks_does_not_split_on_embedded_iteration_header(
    tmp_path: Path,
) -> None:
    """An answer containing a '## Iteration'-like line doesn't get split into two bogus blocks."""
    question_text = "How should the changelog be organized?"
    answer = "By release.\n## Iteration notes\nSee below for details."
    record_answers(tmp_path, [_answered_question(question_text, answer)], iteration=1)

    qa_history = (tmp_path / "plan" / "product_analysis" / "qa_history.md").read_text(
        encoding="utf-8"
    )
    blocks = parse_qa_history_blocks(qa_history)

    assert len(blocks) == 1
    _iteration, question_text_out, answer_out, _full_block = blocks[0]
    assert question_text_out == question_text
    assert answer_out == answer


def test_record_answers_prunes_superseded_block_with_escaped_content(tmp_path: Path) -> None:
    """A later answer correctly prunes an earlier same-decision block containing escaped content."""
    question_text = "Which retry policy should the payment worker use?"
    first_answer = "Backoff.\n*Auto-answered fallback text\n## Iteration notes\nEnd."
    record_answers(tmp_path, [_answered_question(question_text, first_answer)], iteration=1)

    record_answers(
        tmp_path,
        [_answered_question(question_text, "New answer.")],
        iteration=2,
    )

    qa_history = (tmp_path / "plan" / "product_analysis" / "qa_history.md").read_text(
        encoding="utf-8"
    )
    blocks = parse_qa_history_blocks(qa_history)

    assert len(blocks) == 1
    iteration, question_text_out, answer_out, _full_block = blocks[0]
    assert iteration == 2
    assert question_text_out == question_text
    assert answer_out == "New answer."


def test_parse_qa_history_blocks_single_line_still_works() -> None:
    """A plain single-line answer block (the common case) still parses correctly."""
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
    """Empty or whitespace-only qa_history content returns an empty list rather than raising."""
    assert parse_qa_history_blocks("") == []
    assert parse_qa_history_blocks("   \n  ") == []


def test_parse_qa_history_blocks_keeps_block_with_empty_question_but_answer() -> None:
    """A ### header with empty question text is kept when an answer is present."""
    qa_history = (
        "# Q&A History\n\n"
        "## Iteration 1\n\n"
        "### \n"
        "**Answer:** yes\n\n"
    )

    blocks = parse_qa_history_blocks(qa_history)

    assert len(blocks) == 1
    _iteration, question_text, answer, _full_block = blocks[0]
    assert question_text == ""
    assert answer == "yes"


def test_parse_qa_history_blocks_indented_question_header_is_content_not_boundary() -> None:
    """An indented '### ' line inside an answer does not truncate the block.

    Structural ### headers are recognized only at column 0; writers escape
    column-0 collisions, but an indented heading (e.g. quoted markdown) must
    still stay inside the current answer field.
    """
    qa_history = (
        "# Q&A History\n\n"
        "## Iteration 1\n\n"
        "### What docs format should we use?\n"
        "**Answer:** Use Markdown.\n"
        "  ### Details\n"
        "More info below.\n\n"
    )

    blocks = parse_qa_history_blocks(qa_history)

    assert len(blocks) == 1
    _iteration, _question_text, answer, _full_block = blocks[0]
    assert answer == "Use Markdown.\n  ### Details\nMore info below."


# ---------------------------------------------------------------------------
# other_text escaping (record_answers / format_answered_questions_for_prompt)
# ---------------------------------------------------------------------------


def test_record_answers_escapes_other_text_matching_question_header(tmp_path: Path) -> None:
    """other_text containing a '### '-like line is escaped so the file doesn't get corrupted."""
    question_text = "Which option should we pick for the custom answer?"
    other_text = "My custom reason.\n### Not a real header\nStill part of the note."
    aq = _answered_question(question_text, "Other", other_text=other_text)

    record_answers(tmp_path, [aq], iteration=1)

    qa_history = (tmp_path / "plan" / "product_analysis" / "qa_history.md").read_text(
        encoding="utf-8"
    )
    blocks = parse_qa_history_blocks(qa_history)

    assert len(blocks) == 1
    _iteration, question_text_out, _answer, _full_block = blocks[0]
    assert question_text_out == question_text
    assert "\\### Not a real header" in qa_history


def test_format_answered_questions_for_prompt_escapes_other_text_matching_marker() -> None:
    """other_text containing a '**Answer:**'-like line is escaped in the LLM prompt block too."""
    aq = _answered_question(
        "Which option should we pick?",
        "Other",
        other_text="First line.\n**Answer:** looks like a marker but isn't",
    )

    prompt_block = format_answered_questions_for_prompt([aq])

    assert "\\**Answer:** looks like a marker but isn't" in prompt_block


def test_format_answered_questions_for_prompt_escapes_multiline_answer_and_rationale() -> None:
    """selected_answer/rationale continuation lines are escaped in the LLM prompt block too."""
    aq = _answered_question(
        "What approach should we use?",
        selected_answer="Line one.\n### Not a real header",
        rationale="Rationale one.\n## Not a real iteration",
    )

    prompt_block = format_answered_questions_for_prompt([aq])

    assert "\\### Not a real header" in prompt_block
    assert "\\## Not a real iteration" in prompt_block


def test_format_answered_questions_for_prompt_raises_for_non_numeric_confidence() -> None:
    """A caller-constructed AnsweredQuestion with a non-numeric confidence raises.

    Normal ``AnsweredQuestion(...)`` construction runs Pydantic validation and
    can't produce this; ``model_construct`` bypasses it, simulating a caller
    that skips validation. Confirms the corrected docstring's contract
    (previously it wrongly claimed the function "never raises") — this is a
    precondition violation, not normal operation.
    """
    malformed = AnsweredQuestion.model_construct(
        question_id="q1",
        question_text="Which cache should we use?",
        selected_option_id="",
        selected_option_ids=[],
        selected_answer="Redis",
        was_auto_answered=True,
        was_default=False,
        rationale="",
        confidence="not-a-number",
        other_text="",
    )

    with pytest.raises((TypeError, ValueError)):
        format_answered_questions_for_prompt([malformed])


# ---------------------------------------------------------------------------
# Match-threshold consistency (extract_answer_from_qa_history vs is_same_decision)
# ---------------------------------------------------------------------------


def test_extract_answer_retrieves_answer_at_exactly_the_match_threshold() -> None:
    """A question at exactly the 0.5 match ratio is retrievable.

    Two key words ("aaaaa", "bbbbb") in the incoming question; the recorded
    question only contains one of them, so match_ratio is exactly 0.5 —
    previously required strictly > 0.5 and returned None here.
    """
    qa_history = (
        "# Q&A History\n\n## Iteration 1\n\n### aaaaa ccccc\n**Answer:** Recorded answer.\n\n"
    )
    question = _open_question("aaaaa bbbbb")

    result = extract_answer_from_qa_history(question, qa_history)

    assert result is not None
    assert result.selected_answer == "Recorded answer."


def test_extract_answer_prefers_later_block_on_equal_match_ratio() -> None:
    """When two blocks share the same match ratio, the later (more recent) block wins."""
    qa_history = (
        "# Q&A History\n\n"
        "## Iteration 1\n\n"
        "### aaaaa ccccc\n"
        "**Answer:** Earlier answer.\n\n"
        "## Iteration 2\n\n"
        "### aaaaa ddddd\n"
        "**Answer:** Later answer.\n\n"
    )
    question = _open_question("aaaaa bbbbb")

    result = extract_answer_from_qa_history(question, qa_history)

    assert result is not None
    assert result.selected_answer == "Later answer."


# ---------------------------------------------------------------------------
# Keyword filter (content-word based, not length-based) short-question matching
# ---------------------------------------------------------------------------


def test_extract_answer_matches_short_content_word_keywords() -> None:
    """A question made up entirely of short (<=4 char) content words still matches.

    Previously key_words filtered to len(w) > 4, so a question with no word
    over 4 characters (e.g. an all-short-acronym question) produced an empty
    key_words list and always returned None, even when an equivalent prior
    question existed in qa_history.md.
    """
    qa_history = (
        "# Q&A History\n\n"
        "## Iteration 1\n\n"
        "### Should we use IAM or ACL policies on S3 buckets?\n"
        "**Answer:** Use IAM policies exclusively.\n\n"
    )
    question = _open_question("Do we use IAM or ACL on S3?")

    result = extract_answer_from_qa_history(question, qa_history)

    assert result is not None
    assert result.selected_answer == "Use IAM policies exclusively."


def test_extract_answer_returns_none_for_stopword_only_question() -> None:
    """A question with no content-bearing word (only stopwords) returns None,
    even when an identical question is recorded in history.

    Preserves the prior "no usable keyword" guard behavior, now expressed via
    the stopword-based _content_words filter shared with is_same_decision
    instead of a raw word-length cutoff.
    """
    qa_history = (
        "# Q&A History\n\n"
        "## Iteration 1\n\n"
        "### Should we use it?\n"
        "**Answer:** Yes.\n\n"
    )
    question = _open_question("Should we use it?")

    assert extract_answer_from_qa_history(question, qa_history) is None


def test_extract_answer_short_keyword_does_not_match_as_a_substring() -> None:
    """A short key word must match a whole word in the recorded question, not
    merely appear as a substring inside an unrelated longer word.

    Once short (<=4 char) words became eligible key words, comparing them via
    ``w in recorded_question_lower`` risked a false match: the key word "api"
    is a substring of "capitalizing", so an unrelated later block could tie
    (or beat) the genuine earlier match and win the later-block tie-break,
    silently returning the wrong answer.
    """
    qa_history = (
        "# Q&A History\n\n"
        "## Iteration 1\n\n"
        "### Do we use an API for external calls?\n"
        "**Answer:** REST API answer.\n\n"
        "## Iteration 2\n\n"
        "### Are we capitalizing gains this quarter?\n"
        "**Answer:** Unrelated wrong answer.\n\n"
    )
    question = _open_question("Do we use an API?")

    result = extract_answer_from_qa_history(question, qa_history)

    assert result is not None
    assert result.selected_answer == "REST API answer."


# ---------------------------------------------------------------------------
# is_same_decision content-word matching (pruning safety)
# ---------------------------------------------------------------------------


def test_is_same_decision_rejects_shared_interrogative_boilerplate() -> None:
    """Questions that only share interrogative/boilerplate words are not the same decision.

    Without content-word filtering, 'What authentication strategy should we use?' and
    'What strategy should we use for logging?' share enough function words to clear a
    0.5 raw-overlap bar and would cause record_answers to prune the unrelated history.
    """
    assert not is_same_decision(
        "What authentication strategy should we use?",
        "What strategy should we use for logging?",
    )


def test_is_same_decision_accepts_shared_content_topic() -> None:
    """Questions about the same content-bearing topic still match after stopword filtering."""
    assert is_same_decision(
        "What authentication strategy for the API?",
        "What authentication strategy should the API use?",
    )


def test_record_answers_does_not_prune_unrelated_question_with_shared_boilerplate(
    tmp_path: Path,
) -> None:
    """record_answers keeps an unrelated prior block when only interrogative words overlap."""
    record_answers(
        tmp_path,
        [
            _answered_question(
                "What authentication strategy should we use?",
                "OAuth2 with PKCE.",
            )
        ],
        iteration=1,
    )

    record_answers(
        tmp_path,
        [
            _answered_question(
                "What strategy should we use for logging?",
                "Structured JSON to stdout.",
            )
        ],
        iteration=2,
    )

    qa_history = (tmp_path / "plan" / "product_analysis" / "qa_history.md").read_text(
        encoding="utf-8"
    )
    blocks = parse_qa_history_blocks(qa_history)

    assert len(blocks) == 2
    questions = {question_text for _iteration, question_text, _answer, _full in blocks}
    assert "What authentication strategy should we use?" in questions
    assert "What strategy should we use for logging?" in questions

def test_extract_answer_preserves_auto_answered_provenance(tmp_path: Path) -> None:
    """Auto-answered status and confidence round-trip through record_answers and extract."""
    question_text = "Which authentication strategy should the public API use?"
    record_answers(
        tmp_path,
        [
            _answered_question(
                question_text,
                "JWT access tokens with short TTL.",
                rationale="Matches existing gateway defaults.",
                was_auto_answered=True,
                confidence=0.85,
            )
        ],
        iteration=1,
    )

    qa_history = (tmp_path / "plan" / "product_analysis" / "qa_history.md").read_text(
        encoding="utf-8"
    )
    result = extract_answer_from_qa_history(_open_question(question_text), qa_history)

    assert result is not None
    assert result.was_auto_answered is True
    assert result.was_default is False
    assert result.confidence == 0.85
    assert result.selected_answer == "JWT access tokens with short TTL."
    assert result.rationale == "Matches existing gateway defaults."


def test_extract_answer_preserves_default_applied_provenance(tmp_path: Path) -> None:
    """Default-applied status round-trips so callers can label provenance in prompts."""
    question_text = "Which logging destination should background workers use?"
    record_answers(
        tmp_path,
        [
            _answered_question(
                question_text,
                "Structured JSON to stdout.",
                was_default=True,
            )
        ],
        iteration=1,
    )

    qa_history = (tmp_path / "plan" / "product_analysis" / "qa_history.md").read_text(
        encoding="utf-8"
    )
    result = extract_answer_from_qa_history(_open_question(question_text), qa_history)

    assert result is not None
    assert result.was_default is True
    assert result.was_auto_answered is False
    assert result.confidence == 0.0
    assert result.selected_answer == "Structured JSON to stdout."


def test_extract_answer_preserves_other_text_from_history(tmp_path: Path) -> None:
    """Custom other_text written into qa_history.md is restored on extract."""
    question_text = "Which option should we pick for the custom deployment answer?"
    other_text = "Deploy to staging first.\nThen promote after soak."
    record_answers(
        tmp_path,
        [_answered_question(question_text, "Other", other_text=other_text)],
        iteration=1,
    )

    qa_history = (tmp_path / "plan" / "product_analysis" / "qa_history.md").read_text(
        encoding="utf-8"
    )
    result = extract_answer_from_qa_history(_open_question(question_text), qa_history)

    assert result is not None
    assert result.other_text == other_text
