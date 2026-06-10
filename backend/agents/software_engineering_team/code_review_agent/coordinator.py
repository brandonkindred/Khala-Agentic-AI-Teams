"""Code Review Coordinator: map-reduce review with bounded per-call prompts.

Pipeline: input → (path, content) blocks → bounded ``FileSegment``s →
``ReviewChunk``s → per-chunk LLM review with bisect-and-degrade failure
handling → line re-anchoring → deterministic merge (dedupe, severity gate,
safety nets). Every LLM call carries at most ``compute_code_review_map_chunk_chars``
of code regardless of input size, and no input file is ever silently dropped:
a chunk whose review keeps failing degrades to a non-blocking info finding
that names the un-reviewed line range.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from llm_service import LLMClient, compact_text
from software_engineering_team.shared.context_sizing import (
    compute_code_review_arch_overview_chars,
    compute_code_review_existing_codebase_chars,
    compute_code_review_map_chunk_chars,
    compute_code_review_spec_excerpt_chars,
)

from .chunk_reviewer import ChunkReviewAgent
from .models import (
    ChunkReviewInput,
    CodeReviewInput,
    CodeReviewIssue,
    CodeReviewOutput,
    FileSegment,
    ReviewChunk,
    coerce_line,
)

logger = logging.getLogger(__name__)

# Pattern: ### path/to/file ### at start of a block (content may contain \n\n)
_FILE_HEADER_PATTERN = re.compile(r"###\s+(.+?)\s+###\s*\n", re.DOTALL)

# Lines like "123: code" — the coding team's pre-numbered PR-diff hunks.
_PRENUMBERED_RE = re.compile(r"^\s*\d+: ")

# Suffix that ``ReviewChunk.paths_label`` appends to partial segments; stripped
# when the model echoes it back inside an issue's file_path.
_LINES_SUFFIX_RE = re.compile(r"\s*\(lines \d+-\d+ of \d+\)\s*$")

# A failing chunk is bisected and retried; below this content size or past this
# recursion depth it degrades to a non-blocking info finding instead.
MIN_SPLIT_SEGMENT_CHARS = 8_000
MAX_CHUNK_BISECT_DEPTH = 3

_BLOCK_JOINER_CHARS = 2  # "\n\n" between rendered blocks in a chunk


def parse_code_into_file_blocks(code: str) -> List[Tuple[str, str]]:
    """
    Parse concatenated code into (path, content) blocks using ### path ### pattern.
    Returns list of (file_path, content) tuples.
    """
    blocks: List[Tuple[str, str]] = []
    matches = list(_FILE_HEADER_PATTERN.finditer(code))
    if not matches:
        if code.strip():
            blocks.append(("", code.strip()))
        return blocks
    for i, m in enumerate(matches):
        path = m.group(1).strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(code)
        content = code[start:end].rstrip()
        blocks.append((path, content))
    return blocks


def build_chunks(blocks: List[Tuple[str, str]], max_chars: int) -> List[Tuple[List[str], str]]:
    """
    Group file blocks into chunks so each chunk is ≤ max_chars.
    Returns list of (list_of_paths, combined_content).

    Legacy: kept for back-compat; the coordinator now uses ``build_review_chunks``,
    which also splits single blocks that exceed the budget.
    """
    chunks: List[Tuple[List[str], str]] = []
    current_paths: List[str] = []
    current_parts: List[str] = []
    current_len = 0

    for path, content in blocks:
        block_text = f"### {path} ###\n{content}" if path else content
        block_len = len(block_text)
        if current_len + block_len > max_chars and current_parts:
            combined = "\n\n".join(current_parts)
            chunks.append((list(current_paths), combined))
            current_paths = []
            current_parts = []
            current_len = 0
        current_paths.append(path or "(unknown)")
        current_parts.append(block_text)
        current_len += block_len

    if current_parts:
        combined = "\n\n".join(current_parts)
        chunks.append((list(current_paths), combined))
    return chunks


def _blocks_from_input(input_data: CodeReviewInput) -> List[Tuple[str, str]]:
    """Resolve the review input into ordered (path, content) blocks.

    Preconditions:
        - ``input_data`` is a valid ``CodeReviewInput``.

    Postconditions:
        - When ``files`` is set: one block per file with non-blank content,
          insertion order preserved, no header parsing of ``code``.
        - Otherwise blocks come from ``parse_code_into_file_blocks(code)``;
          headerless code yields one ``('', code)`` block.
        - No returned block has blank content.
    """
    if input_data.files is not None:
        return [
            (path, content)
            for path, content in input_data.files.items()
            if content and content.strip()
        ]
    blocks = parse_code_into_file_blocks(input_data.code or "")
    return [(path, content) for path, content in blocks if content.strip()]


def _is_pre_numbered(content: str) -> bool:
    """Detect pre-line-numbered content (the coding team's PR-diff hunks).

    Postconditions:
        - Returns True when at least 3 of the first 5 non-empty lines (or all
          of them, when fewer than 3 exist) match ``^\\s*\\d+: ``.
    """
    checked = 0
    hits = 0
    for line in content.splitlines():
        if not line.strip():
            continue
        checked += 1
        if _PRENUMBERED_RE.match(line):
            hits += 1
        if checked == 5:
            break
    return checked > 0 and hits >= min(3, checked)


def split_block_into_segments(path: str, content: str, max_chars: int) -> List[FileSegment]:
    """Split one file block into line-boundary segments of at most ``max_chars``.

    Preconditions:
        - ``max_chars`` > 0.

    Postconditions:
        - Concatenating segment contents in order reproduces ``content`` exactly.
        - Each segment's content is ≤ ``max_chars``, except when a single line
          alone exceeds it (line boundaries are never broken).
        - A within-budget block yields exactly one whole-file segment.
        - ``start_line``/``part_index``/``part_count`` are mutually consistent
          and ``pre_numbered`` is flagged uniformly across the file's segments.
    """
    assert max_chars > 0, "max_chars must be positive"
    pre_numbered = _is_pre_numbered(content)
    total_lines = len(content.splitlines()) or 1
    if len(content) <= max_chars:
        return [
            FileSegment(
                path=path,
                content=content,
                start_line=1,
                total_lines=total_lines,
                part_index=1,
                part_count=1,
                pre_numbered=pre_numbered,
            )
        ]
    lines = content.splitlines(keepends=True)
    pieces: List[Tuple[int, str]] = []
    buf: List[str] = []
    buf_len = 0
    buf_start = 1
    line_no = 1
    for ln in lines:
        if buf and buf_len + len(ln) > max_chars:
            pieces.append((buf_start, "".join(buf)))
            buf = []
            buf_len = 0
            buf_start = line_no
        buf.append(ln)
        buf_len += len(ln)
        line_no += 1
    if buf:
        pieces.append((buf_start, "".join(buf)))
    part_count = len(pieces)
    return [
        FileSegment(
            path=path,
            content=text,
            start_line=start,
            total_lines=total_lines,
            part_index=i + 1,
            part_count=part_count,
            pre_numbered=pre_numbered,
        )
        for i, (start, text) in enumerate(pieces)
    ]


def build_review_chunks(blocks: List[Tuple[str, str]], max_chars: int) -> List[ReviewChunk]:
    """Group file blocks into review chunks whose rendered content is ≤ ``max_chars``.

    Preconditions:
        - ``max_chars`` > 0.

    Postconditions:
        - Every input block is fully covered exactly once across the returned
          chunks: no file or line range is dropped or duplicated.
        - No chunk holds two segments of the same path (keeps ``offset_by_path``
          unambiguous).
        - Each chunk's rendered ``content`` is ≤ ``max_chars``, except a chunk
          holding a single segment that alone exceeds the budget (a single line
          longer than the cap), which is intentionally placed alone.
    """
    assert max_chars > 0, "max_chars must be positive"
    chunks: List[ReviewChunk] = []
    current: List[FileSegment] = []
    current_len = 0

    def _rendered_len(seg: FileSegment) -> int:
        header = len(f"### {seg.path} ###\n") if seg.path else 0
        return header + len(seg.content)

    def _flush() -> None:
        nonlocal current, current_len
        if current:
            chunks.append(ReviewChunk(segments=current))
            current = []
            current_len = 0

    for path, content in blocks:
        header_len = len(f"### {path} ###\n") if path else 0
        seg_budget = max(1, max_chars - header_len)
        for seg in split_block_into_segments(path, content, seg_budget):
            seg_len = _rendered_len(seg)
            if seg_len > max_chars:
                _flush()
                chunks.append(ReviewChunk(segments=[seg]))
                continue
            joiner = _BLOCK_JOINER_CHARS if current else 0
            same_path = any(s.path == seg.path for s in current)
            if current and (same_path or current_len + joiner + seg_len > max_chars):
                _flush()
                joiner = 0
            current.append(seg)
            current_len += joiner + seg_len
    _flush()
    return chunks


def _segment_notes(chunk: ReviewChunk) -> str:
    """Build reviewer guidance for split or pre-numbered segments in a chunk.

    Postconditions:
        - Returns '' when the chunk holds only whole, plain files.
        - Pre-numbered guidance takes precedence over partial-view guidance for
          a given segment (its cited numbers are already original lines).
    """
    notes: List[str] = []
    for seg in chunk.segments:
        name = seg.path or "the code"
        if seg.pre_numbered:
            notes.append(
                f"The lines of {name} carry their original line-number prefixes (e.g. `123: code`); "
                "set `line` to those exact prefixed numbers."
            )
        elif seg.is_partial:
            notes.append(
                f"{name} is shown only from original line {seg.start_line} to {seg.end_line} "
                f"(of {seg.total_lines} total). Report `line` relative to the snippet "
                "(first shown line = 1); it is re-anchored to the original file automatically."
            )
    return "\n".join(notes)


def _normalize_issue_path(raw_path: str, chunk: ReviewChunk) -> str:
    """Normalize an LLM-reported file path back to a segment path.

    Postconditions:
        - An echoed ``" (lines A-B of N)"`` suffix is stripped.
        - A blank path resolves to the chunk's sole segment path when the chunk
          has exactly one segment; otherwise it stays blank.
    """
    path = _LINES_SUFFIX_RE.sub("", (raw_path or "").strip())
    if not path and len(chunk.segments) == 1:
        return chunk.segments[0].path
    return path


def _issues_from_chunk_output(chunk: ReviewChunk, raw_issues: List[dict]) -> List[CodeReviewIssue]:
    """Convert chunk-reviewer issue dicts into re-anchored ``CodeReviewIssue``s.

    Postconditions:
        - ``line``/``start_line`` are shifted by the owning segment's
          ``line_offset`` (0 for pre-numbered segments), so they refer to
          original file lines.
        - Issues whose path is unknown to the chunk keep their numbers as-is.
    """
    offsets = chunk.offset_by_path
    issues: List[CodeReviewIssue] = []
    for item in raw_issues:
        if not isinstance(item, dict):
            continue
        path = _normalize_issue_path(item.get("file_path", ""), chunk)
        offset = offsets.get(path, 0)
        line = coerce_line(item.get("line"))
        start_line = coerce_line(item.get("start_line"))
        issues.append(
            CodeReviewIssue(
                severity=item.get("severity", "high"),
                category=item.get("category", "general"),
                file_path=path or chunk.paths_label,
                line=line + offset if line is not None else None,
                start_line=start_line + offset if start_line is not None else None,
                description=item.get("description", ""),
                suggestion=item.get("suggestion", ""),
            )
        )
    return issues


@dataclass
class _ChunkOutcome:
    """Accumulated result of reviewing one chunk (possibly via bisection).

    Invariants:
        - ``approved_flags`` holds one entry per *successful* LLM sub-review;
          degraded segments contribute info findings but no flag.
    """

    issues: List[CodeReviewIssue] = field(default_factory=list)
    summaries: List[str] = field(default_factory=list)
    spec_notes: List[str] = field(default_factory=list)
    commit_messages: List[str] = field(default_factory=list)
    approved_flags: List[bool] = field(default_factory=list)

    def merge(self, other: "_ChunkOutcome") -> "_ChunkOutcome":
        """Concatenate two outcomes in order. Postcondition: no entry is lost."""
        return _ChunkOutcome(
            issues=self.issues + other.issues,
            summaries=self.summaries + other.summaries,
            spec_notes=self.spec_notes + other.spec_notes,
            commit_messages=self.commit_messages + other.commit_messages,
            approved_flags=self.approved_flags + other.approved_flags,
        )


def _bisect_segment(seg: FileSegment) -> Optional[Tuple[FileSegment, FileSegment]]:
    """Split one segment into two halves on a line boundary.

    Postconditions:
        - Returns None when the segment has fewer than 2 lines or its content
          is below ``2 * MIN_SPLIT_SEGMENT_CHARS`` (not worth retrying smaller).
        - Otherwise the two halves' contents concatenate to the original and
          ``start_line`` arithmetic stays consistent.
    """
    if len(seg.content) < 2 * MIN_SPLIT_SEGMENT_CHARS:
        return None
    lines = seg.content.splitlines(keepends=True)
    if len(lines) < 2:
        return None
    target = len(seg.content) // 2
    acc = 0
    split_at = 1
    for i, ln in enumerate(lines[:-1], start=1):
        acc += len(ln)
        split_at = i
        if acc >= target:
            break
    first_text = "".join(lines[:split_at])
    second_text = "".join(lines[split_at:])
    first = seg.model_copy(update={"content": first_text})
    second = seg.model_copy(
        update={"content": second_text, "start_line": seg.start_line + split_at}
    )
    return first, second


def _bisect_chunk(chunk: ReviewChunk) -> Optional[Tuple[ReviewChunk, ReviewChunk]]:
    """Split a failing chunk in two for retry.

    Postconditions:
        - Multi-segment chunks split by segment list; a single segment splits
          by lines via ``_bisect_segment``.
        - Returns None when no further split is possible.
    """
    if len(chunk.segments) > 1:
        mid = len(chunk.segments) // 2
        return (
            ReviewChunk(segments=chunk.segments[:mid]),
            ReviewChunk(segments=chunk.segments[mid:]),
        )
    if len(chunk.segments) == 1:
        halves = _bisect_segment(chunk.segments[0])
        if halves is not None:
            return ReviewChunk(segments=[halves[0]]), ReviewChunk(segments=[halves[1]])
    return None


def _degraded_outcome(chunk: ReviewChunk) -> _ChunkOutcome:
    """Terminal-failure fallback: one non-blocking info finding per segment.

    Postconditions:
        - Every segment of the chunk is named with its un-reviewed line range.
        - No ``approved_flags`` entry is added, so degraded segments never
          influence the approval gate.
    """
    issues = [
        CodeReviewIssue(
            severity="info",
            category="general",
            file_path=seg.path or "(unknown)",
            description=(
                f"Automated review failed for this file (lines {seg.start_line}-{seg.end_line} "
                f"of {seg.total_lines}): the review model returned no usable output after "
                "bisect retries. These lines were NOT reviewed."
            ),
            suggestion="Re-run the review or inspect these lines manually.",
        )
        for seg in chunk.segments
    ]
    return _ChunkOutcome(issues=issues)


def _review_chunk_with_degradation(
    reviewer: ChunkReviewAgent,
    chunk: ReviewChunk,
    base_input: Dict,
    depth: int = 0,
) -> _ChunkOutcome:
    """Review one chunk, bisecting on failure and degrading when exhausted.

    Preconditions:
        - ``base_input`` holds the shared ``ChunkReviewInput`` fields
          (task/spec/architecture context), not per-chunk fields.

    Postconditions:
        - Never raises: any LLM/parse failure either recovers via bisection
          (≤ ``MAX_CHUNK_BISECT_DEPTH``) or degrades to info findings.
        - Successful sub-reviews contribute re-anchored issues and one
          ``approved_flags`` entry each.
    """
    try:
        output = reviewer.run(
            ChunkReviewInput(
                code_chunk=chunk.content,
                file_path_or_label=chunk.paths_label,
                segment_note=_segment_notes(chunk),
                **base_input,
            )
        )
    except Exception as exc:
        halves = _bisect_chunk(chunk) if depth < MAX_CHUNK_BISECT_DEPTH else None
        if halves is not None:
            logger.warning(
                "CodeReviewCoordinator: chunk review failed at depth %s (%s: %s) — bisecting [%s]",
                depth,
                type(exc).__name__,
                exc,
                chunk.paths_label,
            )
            left = _review_chunk_with_degradation(reviewer, halves[0], base_input, depth + 1)
            right = _review_chunk_with_degradation(reviewer, halves[1], base_input, depth + 1)
            return left.merge(right)
        logger.error(
            "CodeReviewCoordinator: chunk review failed terminally at depth %s (%s: %s) — "
            "degrading to info findings [%s]",
            depth,
            type(exc).__name__,
            exc,
            chunk.paths_label,
        )
        return _degraded_outcome(chunk)
    return _ChunkOutcome(
        issues=_issues_from_chunk_output(chunk, output.issues),
        summaries=[output.summary],
        spec_notes=[output.spec_compliance_notes],
        commit_messages=[output.suggested_commit_message],
        approved_flags=[output.approved],
    )


def _dedupe_issues(all_issues: List[CodeReviewIssue]) -> List[CodeReviewIssue]:
    """Dedupe issues by (file_path, line, description).

    Line is part of an issue's identity now that it anchors inline PR comments,
    so the same description on two different lines is two distinct findings,
    not a duplicate. An unanchored copy (line=None) of a finding that also
    appears anchored (same file_path+description) is dropped in favour of the
    anchored one, so the issue isn't reported twice (once in the body, once
    inline).

    Postconditions:
        - Order of first occurrence is preserved.
    """
    anchored_pairs = {(i.file_path, i.description) for i in all_issues if i.line is not None}
    seen: set[Tuple[str, Optional[int], str]] = set()
    deduped: List[CodeReviewIssue] = []
    for issue in all_issues:
        if issue.line is None and (issue.file_path, issue.description) in anchored_pairs:
            continue
        key = (issue.file_path, issue.line, issue.description)
        if key not in seen:
            seen.add(key)
            deduped.append(issue)
    return deduped


def _reconcile_approval(
    llm_approved: bool,
    issues: List[CodeReviewIssue],
    summary: str,
) -> Tuple[bool, List[CodeReviewIssue]]:
    """Deterministic approval gate with the anti-loop safety nets.

    Preconditions:
        - ``issues`` is the deduped merged issue list.

    Postconditions:
        - ``approved is False`` implies the returned issues contain at least
          one critical/high finding (rejections are always actionable).
        - A reject with only minor/info issues, or with no feedback at all,
          flips to approve; a reject with zero issues but a summary gains one
          synthesized high issue built from that summary.
    """
    critical_or_high = [i for i in issues if i.severity in ("critical", "high")]
    approved = llm_approved and not critical_or_high
    if not approved and not critical_or_high:
        if issues:
            logger.info(
                "CodeReview: overriding to approved=True (only %s minor/nit issues, no critical/high)",
                len(issues),
            )
            approved = True
        elif summary and summary.strip():
            logger.warning(
                "CodeReview: LLM returned approved=False with 0 issues -- "
                "synthesizing issue from summary: %s",
                summary[:200],
            )
            issues = [
                *issues,
                CodeReviewIssue(
                    severity="high",
                    category="general",
                    file_path="",
                    description=f"Code review rejected: {summary}",
                    suggestion="Address the concerns described in the review summary. "
                    "Ensure the code meets all acceptance criteria and follows project conventions.",
                ),
            ]
        else:
            logger.warning(
                "CodeReview: LLM returned approved=False with no issues and no summary -- "
                "auto-approving (no actionable feedback to give coding agent)"
            )
            approved = True
    return approved, issues


def run_coordinator(llm: LLMClient, input_data: CodeReviewInput) -> CodeReviewOutput:
    """Map-reduce review entry point: bounded chunks in, merged verdict out.

    Preconditions:
        - ``llm`` implements ``LLMClient`` (context sizing + chunk review calls).
        - ``input_data`` carries the code under review via ``files`` or ``code``.

    Postconditions:
        - Never raises on per-chunk LLM failures; every input file/line range is
          either reviewed or named by a non-blocking info finding.
        - ``approved is False`` implies at least one critical/high issue.
        - When at least one chunk exists and every chunk fails, the result is
          ``approved=False`` with one synthesized high issue (fail closed).
        - The code under review is never compacted or truncated; only the
          spec/architecture/existing-codebase excerpts are.
    """
    blocks = _blocks_from_input(input_data)
    if not blocks:
        return CodeReviewOutput(
            approved=True,
            issues=[],
            summary="No code to review.",
            spec_compliance_notes="",
            suggested_commit_message="",
        )

    max_spec = compute_code_review_spec_excerpt_chars(llm)
    max_arch = compute_code_review_arch_overview_chars(llm)
    max_existing = compute_code_review_existing_codebase_chars(llm)
    spec_content = compact_text(input_data.spec_content or "", max_spec, llm, "specification")
    arch_overview = ""
    if input_data.architecture:
        arch_overview = compact_text(
            input_data.architecture.overview or "", max_arch, llm, "architecture overview"
        )
    existing_codebase = compact_text(
        input_data.existing_codebase or "", max_existing, llm, "existing codebase"
    )

    chunks = build_review_chunks(blocks, compute_code_review_map_chunk_chars(llm))
    logger.info(
        "CodeReviewCoordinator: %s blocks -> %s chunks",
        len(blocks),
        len(chunks),
    )

    base_input = {
        "task_description": input_data.task_description or "",
        "task_requirements": input_data.task_requirements or "",
        "acceptance_criteria": input_data.acceptance_criteria or [],
        "spec_excerpt": spec_content,
        "architecture_overview": arch_overview,
        "existing_codebase_excerpt": existing_codebase or None,
    }

    chunk_reviewer = ChunkReviewAgent(llm)
    outcome = _ChunkOutcome()
    for chunk in chunks:
        outcome = outcome.merge(_review_chunk_with_degradation(chunk_reviewer, chunk, base_input))

    all_issues = list(outcome.issues)
    successes = len(outcome.approved_flags)
    if successes == 0:
        logger.error("CodeReviewCoordinator: all %s chunks failed — failing closed", len(chunks))
        all_issues.append(
            CodeReviewIssue(
                severity="high",
                category="general",
                file_path="",
                description=(
                    "Automated code review could not run: every review chunk failed, so none "
                    "of the submitted code was reviewed."
                ),
                suggestion="Re-run the review; if it keeps failing, check review model health "
                "and the size of the submitted change.",
            )
        )

    deduped = _dedupe_issues(all_issues)
    all_llm_approved = successes > 0 and all(outcome.approved_flags)
    merged_summary = "\n\n".join(s for s in outcome.summaries if s.strip())
    approved, deduped = _reconcile_approval(all_llm_approved, deduped, merged_summary)

    spec_notes = outcome.spec_notes[0] if successes == 1 else ""
    commit_message = outcome.commit_messages[0] if successes == 1 else ""

    logger.info(
        "CodeReviewCoordinator: done, approved=%s, issues=%s, chunks=%s (successful=%s)",
        approved,
        len(deduped),
        len(chunks),
        successes,
    )

    return CodeReviewOutput(
        approved=approved,
        issues=deduped,
        summary=merged_summary,
        spec_compliance_notes=spec_notes,
        suggested_commit_message=commit_message,
    )
