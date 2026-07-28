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
from typing import List, Optional, Tuple

from .models import AnsweredQuestion, OpenQuestion

logger = logging.getLogger(__name__)


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

    Preconditions: ``answered_questions`` is a list of :class:`AnsweredQuestion`.
    Postconditions: returns an empty string for an empty list, otherwise a Markdown
        block; never raises.
    """
    if not answered_questions:
        return ""
    lines: List[str] = []
    for aq in answered_questions:
        lines.append(f"### {aq.question_text}")
        lines.append(f"**Answer:** {aq.selected_answer}")
        if aq.rationale:
            lines.append(f"**Rationale:** {aq.rationale}")
        if aq.was_auto_answered:
            lines.append(f"*Auto-answered with {aq.confidence:.0%} confidence*")
        elif aq.was_default:
            lines.append("*(Default applied)*")
        if aq.other_text:
            lines.append(f"*Custom text:* {aq.other_text}")
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


def extract_answer_from_qa_history(
    question: OpenQuestion,
    qa_history: str,
) -> Optional[AnsweredQuestion]:
    """Extract a previously recorded answer from qa_history.md for a duplicate question.

    Parses the qa_history.md markdown format to find the best matching Q&A pair.

    Args:
        question: The duplicate question to find an answer for.
        qa_history: Raw content of qa_history.md file.

    Returns:
        AnsweredQuestion if a matching answer was found, None otherwise.

    Preconditions: ``question`` is an :class:`OpenQuestion`; ``qa_history`` is a
        string (possibly empty).
    Postconditions: returns the best-matching recorded answer as an
        :class:`AnsweredQuestion`, or ``None`` when no block matches; never raises.
    """
    if not qa_history:
        return None

    q_text_lower = question.question_text.lower()
    key_words = [w for w in q_text_lower.split() if len(w) > 4]

    if not key_words:
        return None

    # Parse qa_history.md sections - format is:
    # ### Question text
    # **Answer:** Answer text
    # **Rationale:** Optional rationale
    # *Auto-answered with X% confidence* or *(Default applied)*

    # Split into Q&A blocks by "### " headers
    blocks = re.split(r"\n###\s+", qa_history)

    best_match: Optional[tuple[float, str, str, str]] = None  # (score, question, answer, rationale)

    for block in blocks[1:]:  # Skip first block (header)
        lines = block.strip().split("\n")
        if not lines:
            continue

        recorded_question = lines[0].strip()
        recorded_question_lower = recorded_question.lower()

        # Calculate match score
        matches = sum(1 for w in key_words if w in recorded_question_lower)
        match_ratio = matches / len(key_words) if key_words else 0

        if match_ratio > 0.5:  # Good enough match
            # Extract answer from block, buffering continuation lines until the next
            # known marker so multi-line answers/rationales survive the round trip.
            answer_lines: List[str] = []
            rationale_lines: List[str] = []
            current_field: Optional[List[str]] = None

            for line in lines[1:]:
                if line.startswith("**Answer:**"):
                    answer_lines = [line.removeprefix("**Answer:**").strip()]
                    current_field = answer_lines
                elif line.startswith("**Rationale:**"):
                    rationale_lines = [line.removeprefix("**Rationale:**").strip()]
                    current_field = rationale_lines
                elif (
                    line.startswith("###")
                    or line.startswith("##")
                    or line.startswith(("*Auto-answered", "*Custom text:*", "*(Default applied)*"))
                ):
                    current_field = None
                elif current_field is not None:
                    current_field.append(line)

            answer = "\n".join(answer_lines).strip()
            rationale = "\n".join(rationale_lines).strip()

            if answer and (best_match is None or match_ratio > best_match[0]):
                best_match = (match_ratio, recorded_question, answer, rationale)

    if best_match:
        _, matched_q, answer, rationale = best_match
        logger.debug(
            "Extracted answer for duplicate question: '%s' -> '%s'",
            question.question_text[:40],
            answer[:40],
        )
        return AnsweredQuestion(
            question_id=question.id,
            question_text=question.question_text,
            selected_option_id="from_history",
            selected_answer=answer,
            was_auto_answered=False,
            was_default=False,
            rationale=rationale or f"Previously answered (matched: {matched_q[:50]})",
            confidence=0.9,  # High confidence since it was user-answered before
        )

    return None


def parse_qa_history_blocks(qa_history: str) -> List[Tuple[int, str, str, str]]:
    """Parse qa_history.md content into blocks for pruning and rewriting.

    Returns:
        List of (iteration, question_text, answer, full_block_text).

    Preconditions: ``qa_history`` is a string (possibly empty).
    Postconditions: returns one tuple per ``### question`` block found, tagged with
        the iteration heading it falls under; empty list for empty input.
    """
    if not qa_history or not qa_history.strip():
        return []
    blocks_out: List[Tuple[int, str, str, str]] = []
    current_iteration = 1
    lines = qa_history.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        iter_match = re.match(r"^##\s+Iteration\s+(\d+)", line.strip())
        if iter_match:
            current_iteration = int(iter_match.group(1))
            i += 1
            continue
        block_match = re.match(r"^###\s+(.*)$", line)
        if block_match:
            question_text = block_match.group(1).strip()
            answer_lines: List[str] = []
            current_field: Optional[List[str]] = None
            block_lines = [line]
            i += 1
            while i < len(lines):
                next_line = lines[i]
                if next_line.strip().startswith("### ") or re.match(
                    r"^##\s+Iteration", next_line.strip()
                ):
                    break
                block_lines.append(next_line)
                stripped = next_line.strip()
                if stripped.startswith("**Answer:**"):
                    answer_lines = [next_line.removeprefix("**Answer:**").strip()]
                    current_field = answer_lines
                elif stripped.startswith(
                    ("**Rationale:**", "*Auto-answered", "*Custom text:*", "*(Default applied)*")
                ):
                    current_field = None
                elif current_field is not None:
                    current_field.append(next_line)
                i += 1
            answer = "\n".join(answer_lines).strip()
            full_block_text = "\n".join(block_lines)
            if question_text or answer:
                blocks_out.append((current_iteration, question_text, answer, full_block_text))
            continue
        i += 1
    return blocks_out


def is_same_decision(existing_question: str, new_question: str) -> bool:
    """Return True if the two questions are about the same decision (new answer supersedes old).

    Preconditions: both arguments are strings.
    Postconditions: returns ``True`` when one question contains the other or their
        significant-word overlap is >= 0.5; ``False`` otherwise (including when
        either is blank).
    """
    if not existing_question.strip() or not new_question.strip():
        return False
    existing_norm = " ".join(existing_question.lower().split())
    new_norm = " ".join(new_question.lower().split())
    if existing_norm in new_norm or new_norm in existing_norm:
        return True

    # Word overlap ratio
    def words(t: str) -> set:
        return set(re.sub(r"[^\w\s]", " ", t.lower()).split()) - {"", "the", "a", "an"}

    existing_w = words(existing_question)
    new_w = words(new_question)
    if not existing_w or not new_w:
        return False
    overlap = len(existing_w & new_w) / max(len(existing_w), len(new_w))
    return overlap >= 0.5


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
    new_section = f"\n## Iteration {iteration}\n\n"
    for aq in answered_questions:
        new_section += f"### {aq.question_text}\n"
        new_section += f"**Answer:** {aq.selected_answer}\n"
        if aq.rationale:
            new_section += f"**Rationale:** {aq.rationale}\n"
        if aq.was_auto_answered:
            new_section += f"*Auto-answered with {aq.confidence:.0%} confidence*\n"
        elif aq.was_default:
            new_section += "*(Default applied)*\n"
        if aq.other_text:
            new_section += f"*Custom text:* {aq.other_text}\n"
        new_section += "\n"

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
