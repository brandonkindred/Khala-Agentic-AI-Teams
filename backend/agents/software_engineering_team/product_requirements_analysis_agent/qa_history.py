"""
Q&A history persistence and parsing for the Product Requirements Analysis Agent.

``plan/product_analysis/qa_history.md`` is the durable record of every question the
agent asked and the answer it received. This module owns reading that file,
formatting in-memory answers into the same Markdown shape for prompt injection,
parsing the file back into structured blocks, and appending a new iteration while
pruning any prior block that the new answers supersede (a later directive replaces
an earlier one for the same decision).

Pure functions plus filesystem I/O — no LLM.
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
from pathlib import Path
from typing import List, NamedTuple, Optional, Tuple

from .models import AnsweredQuestion, OpenQuestion

logger = logging.getLogger(__name__)

# Minimum keyword/word-overlap ratio for two questions to be treated as the
# same underlying decision (duplicate-answer lookup and supersede detection).
_QUESTION_MATCH_THRESHOLD = 0.5

# Function/interrogative words excluded from is_same_decision overlap scoring.
# Without this, questions that only share boilerplate ("what", "should", "we",
# "use") clear the 0.5 bar and cause record_answers to prune unrelated history.
_DECISION_STOPWORDS = frozenset(
    {
        "",
        "a",
        "an",
        "the",
        "what",
        "which",
        "who",
        "whom",
        "whose",
        "where",
        "when",
        "why",
        "how",
        "we",
        "you",
        "they",
        "i",
        "it",
        "our",
        "your",
        "their",
        "this",
        "that",
        "these",
        "those",
        "should",
        "would",
        "could",
        "can",
        "will",
        "shall",
        "may",
        "might",
        "must",
        "do",
        "does",
        "did",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "have",
        "has",
        "had",
        "for",
        "of",
        "to",
        "in",
        "on",
        "at",
        "by",
        "with",
        "from",
        "as",
        "or",
        "and",
        "if",
        "than",
        "into",
        "about",
        "use",
        "using",
        "used",
        "please",
    }
)

# qa_history.md field/section markers. Shared between the writers
# (format_answered_questions_for_prompt, record_answers) and the readers
# (extract_answer_from_qa_history, parse_qa_history_blocks via _consume_block_body)
# so the read and write formats can't silently drift apart.
_ANSWER_MARKER = "**Answer:**"
_RATIONALE_MARKER = "**Rationale:**"
_AUTO_ANSWERED_MARKER = "*Auto-answered"
_CUSTOM_TEXT_MARKER = "*Custom text:*"
_DEFAULT_APPLIED_MARKER = "*(Default applied)*"

# Every marker that terminates an in-progress answer/rationale field (used by
# _is_boundary_line). A "###"/"##" line also always terminates a field, checked
# separately since it's a prefix class rather than a fixed string.
_FIELD_BOUNDARY_MARKERS = (
    _ANSWER_MARKER,
    _RATIONALE_MARKER,
    _AUTO_ANSWERED_MARKER,
    _CUSTOM_TEXT_MARKER,
    _DEFAULT_APPLIED_MARKER,
)

# Persisted auto-answer line written by record_answers / format_answered_questions_for_prompt.
_AUTO_CONFIDENCE_RE = re.compile(
    r"^\*Auto-answered with (\d+(?:\.\d+)?)% confidence\*$"
)


class _ParsedBlockBody(NamedTuple):
    """Parsed answer/rationale/provenance fields from one qa_history.md Q&A block body."""

    answer: str
    rationale: str
    was_auto_answered: bool = False
    was_default: bool = False
    confidence: float = 0.0
    other_text: str = ""



def _atomic_write_text(path: Path, content: str) -> None:
    """Write ``content`` to ``path`` atomically (write-temp-then-rename).

    Avoids truncating ``path`` in place: an interruption mid-write (crash, kill)
    leaves the original file untouched instead of partially overwritten.

    Preconditions: ``path.parent`` exists; ``content`` is a string.
    Postconditions: ``path`` contains exactly ``content`` on success; on any
        failure before the final rename, ``path`` is left unmodified and the
        temporary file is cleaned up.
    """
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def format_answered_questions_for_prompt(answered_questions: List[AnsweredQuestion]) -> str:
    """Format in-memory answered questions in qa_history.md style for inclusion in the LLM prompt.

    Handles empty list and optional fields (rationale, other_text, was_auto_answered, was_default).

    Preconditions: ``answered_questions`` is a list of :class:`AnsweredQuestion`
        with ``confidence`` numeric on every entry (guaranteed by normal Pydantic
        construction; violated only if a caller bypasses validation, e.g. via
        ``AnsweredQuestion.model_construct``).
    Postconditions: returns an empty string for an empty list, otherwise a Markdown
        block. Raises ``TypeError``/``ValueError`` if any ``was_auto_answered=True``
        entry's ``confidence`` cannot be formatted as a percentage — a precondition
        violation, not normal operation.
    """
    if not answered_questions:
        return ""
    lines: List[str] = []
    for aq in answered_questions:
        lines.append(f"### {aq.question_text}")
        lines.append(_format_field_value(_ANSWER_MARKER, aq.selected_answer).rstrip("\n"))
        if aq.rationale:
            lines.append(_format_field_value(_RATIONALE_MARKER, aq.rationale).rstrip("\n"))
        if aq.was_auto_answered:
            lines.append(f"{_AUTO_ANSWERED_MARKER} with {aq.confidence:.0%} confidence*")
        elif aq.was_default:
            lines.append(_DEFAULT_APPLIED_MARKER)
        if aq.other_text:
            lines.append(_format_field_value(_CUSTOM_TEXT_MARKER, aq.other_text).rstrip("\n"))
        lines.append("")
    return "\n".join(lines)


def read_qa_history(repo_path: Path) -> str:
    """Read the QA history file if it exists (from plan/product_analysis).

    Preconditions: ``repo_path`` is a repository root path.
    Postconditions: returns the file's text, or an empty string if it is missing or
        unreadable; never raises.
    """
    qa_file = repo_path / "plan" / "product_analysis" / "qa_history.md"
    if qa_file.exists():
        try:
            return qa_file.read_text(encoding="utf-8")
        except Exception as e:
            logger.warning("Failed to read qa_history.md: %s", e)
    return ""


def _is_boundary_line(line: str) -> bool:
    """Return True if a qa_history.md line is a field or section boundary marker.

    Field/status markers (``**Answer:**``, ``**Rationale:**``, auto/default/custom
    markers) are detected after stripping. ``##``/``###`` section headers are
    recognized only at column 0 so an indented markdown heading inside a value is
    treated as content, not a boundary. Used by :func:`_consume_block_body` and
    :func:`_escape_continuation_line`.

    Preconditions: ``line`` is a single physical line from qa_history.md.
    Postconditions: returns ``True`` iff ``line`` starts with ``##`` at column 0,
        or the stripped line starts with a field/status marker prefix; ``False``
        otherwise; never raises.
    """
    stripped = line.strip()
    if stripped.startswith(_FIELD_BOUNDARY_MARKERS):
        return True
    return bool(re.match(r"^##", line))


def _escape_continuation_line(line: str) -> str:
    """Escape a continuation line so it can't be read back as a structural marker.

    Applied to every line of a multi-line answer/rationale value except its first
    (which is already unambiguous, since it's introduced by the field marker on the
    same physical line). Without this, a continuation line that happens to start
    with ``###``, ``##``, or one of the field/status markers would be silently
    misread as a new question header, iteration header, or field boundary — either
    splitting one answer into multiple bogus Q&A blocks, or truncating it early.
    Reversed by :func:`_unescape_continuation_line` on read.

    Preconditions: ``line`` is a single line (no embedded ``\\n``).
    Postconditions: returns ``line`` prefixed with one ``\\`` if it already starts
        with ``\\`` (to keep the escape reversible) or would collide with
        :func:`_is_boundary_line`; otherwise returns ``line`` unchanged; never
        raises.
    """
    if line.startswith("\\") or _is_boundary_line(line):
        return "\\" + line
    return line


def _unescape_continuation_line(line: str) -> str:
    """Reverse :func:`_escape_continuation_line` for a line read back from qa_history.md.

    Preconditions: ``line`` is a persisted continuation line from inside a qa_history.md
        field value.
    Postconditions: returns ``line`` with exactly one leading ``\\`` removed if
        present; otherwise ``line`` unchanged; never raises.
    """
    return line[1:] if line.startswith("\\") else line


def _format_field_value(marker: str, value: str) -> str:
    """Render a (possibly multi-line) field value as ``marker`` plus its lines, newline-terminated.

    Continuation lines (every line of ``value`` after the first) are escaped via
    :func:`_escape_continuation_line` so a value that happens to contain a line
    matching a qa_history.md structural marker round-trips intact instead of
    corrupting the file on the next :func:`record_answers` write.

    Preconditions: ``marker`` is one of this module's field marker constants;
        ``value`` is a string (may be empty; may contain embedded ``\\n``).
    Postconditions: returns a string ending in ``\\n`` whose first line is
        ``f"{marker} {value's first line}"`` (``f"{marker} \\n"`` when ``value``
        is empty) and whose remaining lines are ``value``'s continuation lines,
        escaped; never raises.
    """
    value_lines = value.split("\n")
    rendered = [value_lines[0], *(_escape_continuation_line(vl) for vl in value_lines[1:])]
    return f"{marker} " + "\n".join(rendered) + "\n"


def _consume_block_body(lines: List[str]) -> _ParsedBlockBody:
    """Buffer a Q&A block's answer/rationale/provenance fields until the next boundary.

    Shared by :func:`extract_answer_from_qa_history` and :func:`parse_qa_history_blocks`
    so the two parsers can't drift on what counts as a continuation line versus a new
    field or section boundary. Continuation lines are unescaped via
    :func:`_unescape_continuation_line` as they're buffered, reversing the escaping
    :func:`_format_field_value` applies on write. Status markers
    (``*Auto-answered ...*``, ``*(Default applied)*``) and ``*Custom text:*`` are
    captured as provenance rather than discarded.

    Preconditions: ``lines`` are the lines of a qa_history.md block following its
        ``### question`` header (may be empty).
    Postconditions: returns a :class:`_ParsedBlockBody` whose answer/rationale/
        other_text fields are built as follows: the first line of each field is
        taken from the marker line after ``.strip()`` (so leading/trailing
        whitespace on that first line is normalized away); continuation lines are
        unescaped but otherwise preserved verbatim (including interior
        whitespace); the final joined string for each field is then stripped;
        provenance flags and confidence reflect any status markers found; never
        raises.
    """
    answer_lines: List[str] = []
    rationale_lines: List[str] = []
    other_text_lines: List[str] = []
    current_field: Optional[List[str]] = None
    was_auto_answered = False
    was_default = False
    confidence = 0.0

    for line in lines:
        stripped = line.strip()
        if stripped.startswith(_ANSWER_MARKER):
            answer_lines = [stripped.removeprefix(_ANSWER_MARKER).strip()]
            current_field = answer_lines
        elif stripped.startswith(_RATIONALE_MARKER):
            rationale_lines = [stripped.removeprefix(_RATIONALE_MARKER).strip()]
            current_field = rationale_lines
        elif stripped.startswith(_CUSTOM_TEXT_MARKER):
            other_text_lines = [stripped.removeprefix(_CUSTOM_TEXT_MARKER).strip()]
            current_field = other_text_lines
        elif stripped.startswith(_AUTO_ANSWERED_MARKER):
            was_auto_answered = True
            was_default = False
            conf_match = _AUTO_CONFIDENCE_RE.match(stripped)
            if conf_match:
                confidence = float(conf_match.group(1)) / 100.0
            current_field = None
        elif stripped == _DEFAULT_APPLIED_MARKER:
            was_default = True
            was_auto_answered = False
            current_field = None
        elif _is_boundary_line(line):
            current_field = None
        elif current_field is not None:
            current_field.append(_unescape_continuation_line(line))

    return _ParsedBlockBody(
        answer="\n".join(answer_lines).strip(),
        rationale="\n".join(rationale_lines).strip(),
        was_auto_answered=was_auto_answered,
        was_default=was_default,
        confidence=confidence,
        other_text="\n".join(other_text_lines).strip(),
    )


def extract_answer_from_qa_history(
    question: OpenQuestion,
    qa_history: str,
) -> Optional[AnsweredQuestion]:
    """Extract a previously recorded answer from qa_history.md for a duplicate question.

    Uses :func:`parse_qa_history_blocks` to locate Q&A blocks (so a leading
    ``###`` header with no file preamble is not discarded), then scores each
    recorded question against ``question`` and rebuilds the matched block body
    via :func:`_consume_block_body`.

    Args:
        question: The duplicate question to find an answer for.
        qa_history: Raw content of qa_history.md file.

    Returns:
        AnsweredQuestion if a matching answer was found, None otherwise.
        Also returns ``None`` when ``question.question_text`` has no
        content-bearing keyword (every word is a stopword, or the text is
        blank) — see :func:`content_words`.

    Preconditions: ``question`` is an :class:`OpenQuestion` (``question_text`` a
        string); ``qa_history`` is a string (possibly empty).
    Postconditions: returns the best-matching recorded answer as an
        :class:`AnsweredQuestion` (including ``was_auto_answered`` /
        ``was_default`` / ``confidence`` / ``other_text`` parsed from the block's
        status markers when present), or ``None`` when no block matches or the
        question has no content-bearing keyword; when multiple blocks share the
        same match ratio, the later (more recent) block wins; never raises for
        input satisfying the preconditions above.
    """
    if not qa_history:
        return None

    key_words = list(content_words(question.question_text))

    if not key_words:
        return None

    # Parse qa_history.md via parse_qa_history_blocks so column-0 ### / ## Iteration
    # boundaries (including a leading ### with no file preamble) are handled the
    # same way as pruning/rewriting. Format per block (fields may span continuation
    # lines until the next known marker / column-0 section boundary):
    # ### Question text
    # **Answer:** First line
    # second line
    # **Rationale:** Line one
    # Line two
    # *Auto-answered with 80% confidence*
    #   or *(Default applied)*

    best_match: Optional[tuple[float, str, _ParsedBlockBody]] = None  # (score, question, parsed)

    for _iteration, recorded_question, _answer, full_block_text in parse_qa_history_blocks(
        qa_history
    ):
        recorded_words = content_words(recorded_question)

        # Calculate match score by whole-token overlap, not substring containment:
        # a short key word (e.g. "api") must match a whole word in the recorded
        # question, not merely appear inside an unrelated longer word (e.g.
        # "capitalizing") — a false-positive risk once short words are eligible
        # key words (see content_words).
        matches = sum(1 for w in key_words if w in recorded_words)
        match_ratio = matches / len(key_words)

        if (
            match_ratio >= _QUESTION_MATCH_THRESHOLD
        ):  # Good enough match based on extract_answer keyword coverage
            # Reconstruct body lines from the verbatim block (header is line 0).
            parsed = _consume_block_body(full_block_text.split("\n")[1:])

            # >= so equal scores prefer the later (more recent) block
            if parsed.answer and (best_match is None or match_ratio >= best_match[0]):
                best_match = (match_ratio, recorded_question, parsed)

    if best_match:
        _, matched_q, parsed = best_match
        logger.debug(
            "Extracted answer for duplicate question: '%s' -> '%s'",
            question.question_text,
            parsed.answer,
        )
        if parsed.was_auto_answered:
            confidence = parsed.confidence
        elif parsed.was_default:
            confidence = 0.0
        else:
            confidence = 0.9  # High confidence for previously user-confirmed answers
        return AnsweredQuestion(
            question_id=question.id,
            question_text=question.question_text,
            selected_option_id="from_history",
            selected_answer=parsed.answer,
            was_auto_answered=parsed.was_auto_answered,
            was_default=parsed.was_default,
            rationale=parsed.rationale or f"Previously answered (matched: {matched_q})",
            confidence=confidence,
            other_text=parsed.other_text,
        )

    return None


def parse_qa_history_blocks(qa_history: str) -> List[Tuple[int, str, str, str]]:
    """Parse qa_history.md content into blocks for pruning and rewriting.

    Returns:
        List of (iteration, question_text, answer, full_block_text). The rationale is
        not extracted as its own field here — pruning/rewriting only needs the
        question text (for :func:`is_same_decision` matching) and the verbatim block
        text to reproduce; a multi-line rationale is preserved unmodified inside
        ``full_block_text`` when the block is written back out.

    Preconditions: ``qa_history`` is a string (possibly empty).
    Postconditions: returns one tuple per column-0 ``### question`` block found
        that has a non-empty question text or answer, tagged with the iteration
        heading it falls under; blocks that appear before any explicit
        ``## Iteration N`` heading are tagged as iteration 1; a block with
        neither (e.g. a stray ``###`` header with no content) is skipped; empty
        list for empty input. Structural headers (``###`` / ``## Iteration``)
        are recognized only at column 0 — an indented ``### `` / ``## Iteration``
        line inside a value is treated as content, not a block boundary.
    """
    if not qa_history or not qa_history.strip():
        return []
    blocks_out: List[Tuple[int, str, str, str]] = []
    current_iteration = 1
    lines = qa_history.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        # Column-0 only: writers always emit headers at the start of the line.
        iter_match = re.match(r"^##\s+Iteration\s+(\d+)", line)
        if iter_match:
            current_iteration = int(iter_match.group(1))
            i += 1
            continue
        block_match = re.match(r"^###\s+(.*)$", line)
        if block_match:
            question_text = block_match.group(1).strip()
            block_lines = [line]
            i += 1
            while i < len(lines):
                next_line = lines[i]
                # Match at column 0 only so an indented "### " / "## Iteration"
                # continuation inside a value cannot truncate the block.
                if re.match(r"^###\s+", next_line) or re.match(r"^##\s+Iteration", next_line):
                    break
                block_lines.append(next_line)
                i += 1
            answer = _consume_block_body(block_lines[1:]).answer
            full_block_text = "\n".join(block_lines)
            if question_text or answer:
                blocks_out.append((current_iteration, question_text, answer, full_block_text))
            continue
        i += 1
    return blocks_out


def content_words(text: str) -> set[str]:
    """Return the content-bearing word set of ``text`` for decision/duplicate matching.

    Strips punctuation, lowercases, and drops :data:`_DECISION_STOPWORDS` so
    interrogative/boilerplate words cannot dominate the overlap score used by
    :func:`is_same_decision` and the keyword matching in
    :func:`extract_answer_from_qa_history`. Public (no leading underscore)
    because :mod:`question_processing`'s ``filter_duplicate_questions`` reuses
    it too, so the upstream duplicate-candidate filter and this module's
    answer extractor agree on which short words carry meaning.

    Preconditions: ``text`` is a string.
    Postconditions: returns a (possibly empty) set of lowercase tokens; never raises.
    """
    return set(re.sub(r"[^\w\s]", " ", text.lower()).split()) - _DECISION_STOPWORDS


def is_same_decision(existing_question: str, new_question: str) -> bool:
    """Return True if the two questions are about the same decision (new answer supersedes old).

    Matching prefers substring containment of the normalized full question text,
    then falls back to Jaccard overlap of content-bearing words (interrogative and
    other function words are excluded) against :data:`_QUESTION_MATCH_THRESHOLD`.

    Preconditions: both arguments are strings.
    Postconditions: returns ``True`` when one question contains the other or their
        content-word Jaccard overlap is >= 0.5; ``False`` otherwise (including when
        either is blank or either side has no content words after stopword removal).
    """
    if not existing_question.strip() or not new_question.strip():
        return False
    existing_norm = " ".join(existing_question.lower().split())
    new_norm = " ".join(new_question.lower().split())
    if existing_norm in new_norm or new_norm in existing_norm:
        return True

    existing_w = content_words(existing_question)
    new_w = content_words(new_question)
    if not existing_w or not new_w:
        return False
    overlap = len(existing_w & new_w) / len(existing_w | new_w)
    return overlap >= _QUESTION_MATCH_THRESHOLD


def record_answers(
    repo_path: Path,
    answered_questions: List[AnsweredQuestion],
    iteration: int,
) -> None:
    """Save answered questions to plan/product_analysis/qa_history.md.

    Removes any existing qa_history entry that is the same decision as a new answer
    (new directive replaces old); then writes pruned history + new iteration. The
    write is atomic (via :func:`_atomic_write_text`): an interruption mid-write
    cannot leave ``qa_history.md`` truncated or partially overwritten.

    Preconditions: ``repo_path`` is a repository root; ``answered_questions`` is a
        list of :class:`AnsweredQuestion`; ``iteration`` is a non-negative int (SOP
        discovery flows pass ``0``).
    Postconditions: ``qa_history.md`` exists and ends with the new iteration's
        block; blocks the new answers supersede are removed.
    """
    plan_dir = repo_path / "plan" / "product_analysis"
    plan_dir.mkdir(parents=True, exist_ok=True)
    qa_file = plan_dir / "qa_history.md"

    # New iteration section (same format as before)
    section_lines: List[str] = [f"\n## Iteration {iteration}\n\n"]
    for aq in answered_questions:
        section_lines.append(f"### {aq.question_text}\n")
        section_lines.append(_format_field_value(_ANSWER_MARKER, aq.selected_answer))
        if aq.rationale:
            section_lines.append(_format_field_value(_RATIONALE_MARKER, aq.rationale))
        if aq.was_auto_answered:
            section_lines.append(f"{_AUTO_ANSWERED_MARKER} with {aq.confidence:.0%} confidence*\n")
        elif aq.was_default:
            section_lines.append(f"{_DEFAULT_APPLIED_MARKER}\n")
        if aq.other_text:
            section_lines.append(_format_field_value(_CUSTOM_TEXT_MARKER, aq.other_text))
        section_lines.append("\n")
    new_section = "".join(section_lines)

    if not qa_file.exists():
        content = (
            "# Q&A History\n\n"
            "This file records all questions and answers from Product Requirements Analysis.\n"
            + new_section
        )
        _atomic_write_text(qa_file, content)
        logger.info("Recorded %d answers to %s", len(answered_questions), qa_file)
        return

    existing_content = qa_file.read_text(encoding="utf-8")
    blocks = parse_qa_history_blocks(existing_content)
    remove_indices: set = set()
    for aq in answered_questions:
        for idx, (_, block_question, _, _) in enumerate(blocks):
            if is_same_decision(block_question, aq.question_text):
                remove_indices.add(idx)
    kept_blocks = [
        (it, qt, ans, full)
        for idx, (it, qt, ans, full) in enumerate(blocks)
        if idx not in remove_indices
    ]
    header = (
        "# Q&A History\n\n"
        "This file records all questions and answers from Product Requirements Analysis.\n"
    )
    parts = [header]
    current_iter: Optional[int] = None
    for it, _qt, _ans, full_block_text in kept_blocks:
        if current_iter != it:
            current_iter = it
            parts.append(f"\n## Iteration {it}\n\n")
        parts.append(full_block_text)
        if not full_block_text.endswith("\n"):
            parts.append("\n")
    parts.append(new_section)
    content = "".join(parts)
    _atomic_write_text(qa_file, content)
    logger.info("Recorded %d answers to %s", len(answered_questions), qa_file)
