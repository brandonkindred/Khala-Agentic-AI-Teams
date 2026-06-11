"""Code Review Coordinator: map-reduce review with bounded per-call prompts.

Pipeline: input → (path, content) blocks → bounded ``FileSegment``s →
``ReviewChunk``s → per-chunk LLM review (parallel, with retry/bisect recovery)
→ line re-anchoring → deterministic merge (dedupe, severity gate, safety
nets). Every LLM call carries at most ``compute_code_review_map_chunk_chars``
of code regardless of input size, and no input file is ever silently dropped:
empty files are named by info findings, and a chunk that cannot be reviewed
after recovery fails the whole run loudly with
``CodeReviewUnavailableError`` — the review never renders a verdict on code
it did not see.
"""

from __future__ import annotations

import logging
import re
import threading
from concurrent.futures import FIRST_EXCEPTION, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from llm_service import (
    LLMClient,
    LLMJsonParseError,
    LLMPermanentError,
    LLMRateLimitError,
    LLMSchemaValidationError,
    LLMUnreachableAfterRetriesError,
    compact_text,
)
from software_engineering_team.shared.context_sizing import (
    compute_code_review_arch_overview_chars,
    compute_code_review_existing_codebase_chars,
    compute_code_review_map_chunk_chars,
    compute_code_review_spec_excerpt_chars,
    env_int,
)

from .chunk_reviewer import ChunkReviewAgent
from .models import (
    ChunkReviewInput,
    CodeReviewInput,
    CodeReviewIssue,
    CodeReviewOutput,
    CodeReviewUnavailableError,
    FileSegment,
    ReviewChunk,
    ReviewProgressCallback,
    coerce_line,
    notify_review_progress,
)
from .synthesis import synthesize_review_findings

logger = logging.getLogger(__name__)

# Pattern: a whole line of the form "### path/to/file ###". Anchored to line
# boundaries with a single-line path so header-like fragments inside source
# (markdown headings, "### x" comments, mid-line strings) can never match,
# and a false header can never swallow lines of code the way an unanchored
# DOTALL pattern could.
_FILE_HEADER_PATTERN = re.compile(r"^###[ \t]+(\S[^\n]*?)[ \t]+###[ \t]*\n", re.MULTILINE)

# First capture: the original line number embedded in a pre-numbered line.
_PRENUMBERED_LINE_RE = re.compile(r"^\s*(\d+):")

# Suffix that ``ReviewChunk.paths_label`` appends to partial segments; stripped
# when the model echoes it back inside an issue's file_path.
_LINES_SUFFIX_RE = re.compile(r"\s*\(lines \d+-\d+ of \d+\)\s*$")

# A failing chunk is bisected and retried; below this content size it gets one
# same-input retry instead, and past the depth cap the run fails loudly.
# Both knobs are env-overridable (see docs/ENV_VARS.md).
MIN_SPLIT_SEGMENT_CHARS = 8_000  # CODE_REVIEW_MIN_SPLIT_SEGMENT_CHARS, floor 1_000
MAX_CHUNK_BISECT_DEPTH = 3  # CODE_REVIEW_MAX_BISECT_DEPTH, floor 0
DEFAULT_MAP_PARALLELISM = 4  # CODE_REVIEW_MAP_PARALLELISM, floor 1

_BLOCK_JOINER_CHARS = 2  # "\n\n" between rendered blocks in a chunk

_VALID_SEVERITIES = frozenset({"critical", "high", "medium", "low", "info"})


def _min_split_segment_chars() -> int:
    return env_int("CODE_REVIEW_MIN_SPLIT_SEGMENT_CHARS", MIN_SPLIT_SEGMENT_CHARS, 1_000)


def _max_bisect_depth() -> int:
    return env_int("CODE_REVIEW_MAX_BISECT_DEPTH", MAX_CHUNK_BISECT_DEPTH, 0)


def _map_parallelism() -> int:
    return env_int("CODE_REVIEW_MAP_PARALLELISM", DEFAULT_MAP_PARALLELISM, 1)


def parse_code_into_file_blocks(code: str) -> List[Tuple[str, str]]:
    """
    Parse concatenated code into (path, content) blocks using ### path ### pattern.
    Returns list of (file_path, content) tuples.

    Only a complete line of the form ``### path ###`` counts as a header, so a
    header can never span source lines. A source line that happens to match
    that exact shape (e.g. inside a docstring) is still read as a header — an
    inherent ambiguity of the legacy ``code=`` transport; callers whose content
    may contain such lines must use ``CodeReviewInput.files`` instead, which
    skips header parsing entirely.

    Postconditions:
        - Every non-blank character of ``code`` except recognized header lines
          is covered by some block: content before the first header (or all of
          it, when no header exists) becomes a ``('', content)`` block rather
          than being dropped.
    """
    blocks: List[Tuple[str, str]] = []
    matches = list(_FILE_HEADER_PATTERN.finditer(code))
    if not matches:
        if code.strip():
            blocks.append(("", code.strip()))
        return blocks
    preamble = code[: matches[0].start()]
    if preamble.strip():
        blocks.append(("", preamble.strip()))
    for i, m in enumerate(matches):
        path = m.group(1).strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(code)
        content = code[start:end].rstrip()
        blocks.append((path, content))
    return blocks


def _blocks_from_input(input_data: CodeReviewInput) -> Tuple[List[Tuple[str, str]], List[str]]:
    """Resolve the review input into ordered (path, content) blocks.

    Preconditions:
        - ``input_data`` is a valid ``CodeReviewInput`` (its validator already
          guarantees a code source is present).

    Postconditions:
        - When ``files`` is set: one block per file with non-blank content,
          insertion order preserved, no header parsing of ``code``.
        - Otherwise blocks come from ``parse_code_into_file_blocks(code)``.
        - No returned block has blank content; the second element names every
          non-blank path whose content was blank, so the caller can report the
          skip instead of silently dropping the file.
    """
    skipped: List[str] = []
    if input_data.files is not None:
        blocks = []
        for path, content in input_data.files.items():
            if content and content.strip():
                blocks.append((path, content))
            else:
                skipped.append(path)
        return blocks, skipped
    blocks = []
    for path, content in parse_code_into_file_blocks(input_data.code or ""):
        if content.strip():
            blocks.append((path, content))
        elif path:
            skipped.append(path)
    return blocks, skipped


def split_block_into_segments(
    path: str, content: str, max_chars: int, pre_numbered: bool = False
) -> List[FileSegment]:
    """Split one file block into line-boundary segments of at most ``max_chars``.

    Preconditions:
        - ``max_chars`` > 0.
        - ``pre_numbered`` is True only when the caller declared (via
          ``CodeReviewInput.pre_numbered``) that lines carry ``N: `` prefixes;
          it is never inferred from content.

    Postconditions:
        - Concatenating segment contents in order reproduces ``content`` exactly.
        - Each segment's *rendered* size (content plus the original-line-number
          prefixes partial segments gain in the prompt) is ≤ ``max_chars``,
          except when a single line alone exceeds it (line boundaries are
          never broken).
        - A within-budget block yields exactly one whole-file segment.
    """
    assert max_chars > 0, "max_chars must be positive"
    total_lines = len(content.splitlines()) or 1
    if len(content) <= max_chars:
        return [
            FileSegment(
                path=path,
                content=content,
                start_line=1,
                total_lines=total_lines,
                pre_numbered=pre_numbered,
            )
        ]
    # Split pieces become partial segments, which render with "N: " prefixes
    # (unless already pre-numbered); budget each line's rendered size so the
    # prompt stays within max_chars after prefixing.
    prefix_width = 0 if pre_numbered else len(str(total_lines)) + 2
    lines = content.splitlines(keepends=True)
    pieces: List[Tuple[int, str]] = []
    buf: List[str] = []
    buf_len = 0
    buf_start = 1
    line_no = 1
    for ln in lines:
        rendered_len = len(ln) + prefix_width
        if buf and buf_len + rendered_len > max_chars:
            pieces.append((buf_start, "".join(buf)))
            buf = []
            buf_len = 0
            buf_start = line_no
        buf.append(ln)
        buf_len += rendered_len
        line_no += 1
    if buf:
        pieces.append((buf_start, "".join(buf)))
    return [
        FileSegment(
            path=path,
            content=text,
            start_line=start,
            total_lines=total_lines,
            pre_numbered=pre_numbered,
        )
        for start, text in pieces
    ]


def build_review_chunks(
    blocks: List[Tuple[str, str]], max_chars: int, pre_numbered: bool = False
) -> List[ReviewChunk]:
    """Group file blocks into review chunks whose rendered content is ≤ ``max_chars``.

    Preconditions:
        - ``max_chars`` > 0.

    Postconditions:
        - Every input block is fully covered exactly once across the returned
          chunks: no file or line range is dropped or duplicated.
        - No chunk holds two segments of the same path (so an issue's cited
          path resolves to exactly one segment).
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
        return header + len(seg.prompt_content)

    def _flush() -> None:
        nonlocal current, current_len
        if current:
            chunks.append(ReviewChunk(segments=current))
            current = []
            current_len = 0

    for path, content in blocks:
        header_len = len(f"### {path} ###\n") if path else 0
        seg_budget = max(1, max_chars - header_len)
        for seg in split_block_into_segments(path, content, seg_budget, pre_numbered):
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


def _segment_range_label(seg: FileSegment) -> str:
    """Describe the original-file line range a segment covers.

    Postconditions:
        - Pre-numbered segments report the first/last embedded ``N:`` prefixes
          (their positional indices are meaningless); plain segments report
          ``start_line``–``end_line`` of ``total_lines``.
    """
    name = seg.path or "(headerless code)"
    if seg.pre_numbered:
        numbers = [
            int(m.group(1))
            for line in seg.content.splitlines()
            if (m := _PRENUMBERED_LINE_RE.match(line)) is not None
        ]
        if numbers:
            return f"{name} (original lines {numbers[0]}-{numbers[-1]})"
    return f"{name} (lines {seg.start_line}-{seg.end_line} of {seg.total_lines})"


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
                f"(of {seg.total_lines} total), and every line carries its original line-number "
                "prefix (e.g. `123: code`); set `line` to those exact prefixed numbers."
            )
    return "\n".join(notes)


def _normalize_issue_path(raw_path: str, chunk: ReviewChunk) -> str:
    """Normalize an LLM-reported file path back to a segment path.

    Postconditions:
        - An echoed ``" (lines A-B of N)"`` suffix is stripped.
        - A blank path resolves to the chunk's sole segment path when the chunk
          has exactly one segment; otherwise it stays blank (a blank path is
          never replaced with a fabricated multi-file label).
    """
    path = _LINES_SUFFIX_RE.sub("", (raw_path or "").strip())
    if not path and len(chunk.segments) == 1:
        return chunk.segments[0].path
    return path


def _clean_str(value: object, default: str) -> str:
    """Coerce an untrusted LLM field to a non-empty stripped string.

    Postconditions:
        - Returns ``default`` for None/blank values; never raises.
    """
    if value is None:
        return default
    text = str(value).strip()
    return text or default


def _validate_line(line: Optional[int], seg: Optional[FileSegment]) -> Optional[int]:
    """Validate a cited original-file line number against its segment.

    Cited numbers are absolute by construction: whole files are shown in full
    (relative == absolute), and partial segments are rendered with original
    line-number prefixes (``FileSegment.prompt_content``), so no re-anchoring
    arithmetic — and none of its relative-vs-absolute ambiguity — exists.

    Postconditions:
        - Returns the line unchanged when it falls inside the segment's
          original range (or the segment/its bounds are unknown:
          ``pre_numbered`` content owns its own numbering).
        - Returns None for a citation outside the segment's range, so a
          disobeying model can never anchor feedback to the wrong source line.
    """
    if line is None:
        return None
    if seg is None or seg.pre_numbered:
        return line
    if seg.start_line <= line <= seg.end_line:
        return line
    return None


def _issues_from_chunk_output(chunk: ReviewChunk, raw_issues: List[dict]) -> List[CodeReviewIssue]:
    """Convert chunk-reviewer issue dicts into validated ``CodeReviewIssue``s.

    Postconditions:
        - Untrusted LLM fields are sanitized (severity restricted to the known
          set, strings coerced), so conversion never raises on malformed output.
        - ``line``/``start_line`` are original-file absolute and within the
          cited segment's range, or dropped (see ``_validate_line``).
    """
    seg_by_path = {seg.path: seg for seg in chunk.segments}
    issues: List[CodeReviewIssue] = []
    for item in raw_issues:
        if not isinstance(item, dict):
            continue
        description = _clean_str(item.get("description"), "")
        if not description:
            continue
        path = _normalize_issue_path(_clean_str(item.get("file_path"), ""), chunk)
        seg = seg_by_path.get(path)
        severity = _clean_str(item.get("severity"), "high").lower()
        if severity not in _VALID_SEVERITIES:
            severity = "high"
        issues.append(
            CodeReviewIssue(
                severity=severity,
                category=_clean_str(item.get("category"), "general"),
                file_path=path,
                line=_validate_line(coerce_line(item.get("line")), seg),
                start_line=_validate_line(coerce_line(item.get("start_line")), seg),
                description=description,
                suggestion=_clean_str(item.get("suggestion"), ""),
            )
        )
    return issues


@dataclass
class _ChunkOutcome:
    """Accumulated result of reviewing one chunk (possibly via bisection).

    Invariants:
        - ``approved_flags`` holds one entry per successful LLM sub-review;
          a chunk that cannot be reviewed raises instead of producing an
          outcome, so every outcome reflects reviewed code only.
    """

    issues: List[CodeReviewIssue] = field(default_factory=list)
    summaries: List[str] = field(default_factory=list)
    spec_notes: List[str] = field(default_factory=list)
    commit_messages: List[str] = field(default_factory=list)
    approved_flags: List[bool] = field(default_factory=list)

    def absorb(self, other: "_ChunkOutcome") -> None:
        """Append ``other``'s entries in order. Postcondition: no entry is lost."""
        self.issues.extend(other.issues)
        self.summaries.extend(other.summaries)
        self.spec_notes.extend(other.spec_notes)
        self.commit_messages.extend(other.commit_messages)
        self.approved_flags.extend(other.approved_flags)


def _bisect_segment(seg: FileSegment) -> Optional[Tuple[FileSegment, FileSegment]]:
    """Split one segment into two halves on a line boundary.

    Postconditions:
        - Returns None when the segment has fewer than 2 lines or its content
          is below ``2 * MIN_SPLIT_SEGMENT_CHARS`` (not worth retrying smaller).
        - Otherwise the two halves' contents concatenate to the original and
          ``start_line`` arithmetic stays consistent.
    """
    if len(seg.content) < 2 * _min_split_segment_chars():
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


def _is_infra_failure(exc: BaseException) -> bool:
    """Classify a chunk-review failure as infrastructure vs content-related.

    Infrastructure failures (rate limit, unreachable endpoint, auth/config
    errors) cannot be fixed by reviewing a smaller chunk, so retrying or
    bisecting them only multiplies doomed LLM calls. Content-related failures
    (JSON parse, schema validation, semantic exhaustion, anything else) may
    succeed on a smaller or repeated input.

    Postconditions:
        - Walks the ``__cause__``/``__context__`` chain (strands may wrap the
          client error) up to a bounded depth; never raises.
    """
    seen: set[int] = set()
    current: Optional[BaseException] = exc
    while current is not None and id(current) not in seen and len(seen) < 10:
        seen.add(id(current))
        if isinstance(current, (LLMJsonParseError, LLMSchemaValidationError)):
            return False
        if isinstance(
            current,
            (LLMRateLimitError, LLMUnreachableAfterRetriesError, LLMPermanentError),
        ):
            return True
        current = current.__cause__ or current.__context__
    return False


def _chunk_ranges(chunk: ReviewChunk) -> List[str]:
    """Name every original-file line range the chunk covers."""
    return [_segment_range_label(seg) for seg in chunk.segments]


def _review_chunk_with_recovery(
    reviewer: ChunkReviewAgent,
    chunk: ReviewChunk,
    base_input: Dict,
    depth: int = 0,
    retried: bool = False,
) -> _ChunkOutcome:
    """Review one chunk, recovering from content failures by retry or bisection.

    Preconditions:
        - ``base_input`` holds the shared ``ChunkReviewInput`` fields
          (task/spec/architecture context), not per-chunk fields.

    Postconditions:
        - Returns an outcome covering every line of the chunk, or raises
          ``CodeReviewUnavailableError`` naming the unreviewed ranges — the
          chunk is never silently skipped or scored.
        - Infrastructure failures raise immediately without retry or bisection.
        - Content failures bisect up to the depth cap; any chunk that cannot
          bisect further — the original or a bisected child — gets exactly one
          same-input retry before the run fails, so a one-off transient error
          in a terminal child never aborts the review.
        - A sub-review that rejects with no extractable issues but a non-empty
          summary contributes one synthesized high issue built from that
          summary: applied here, per sub-review, because at the merged level
          other chunks' findings would mask the empty-issues condition and the
          minor-only auto-approve net would silently discard the rejection.
    """
    chunk_input = ChunkReviewInput(
        code_chunk=chunk.content,
        file_path_or_label=chunk.paths_label,
        segment_note=_segment_notes(chunk),
        **base_input,
    )
    try:
        output = reviewer.run(chunk_input)
    except Exception as exc:
        if _is_infra_failure(exc):
            raise CodeReviewUnavailableError(
                f"Review model unavailable ({type(exc).__name__}: {exc}); "
                "no verdict was produced for this submission.",
                unreviewed=_chunk_ranges(chunk),
            ) from exc
        halves = _bisect_chunk(chunk) if depth < _max_bisect_depth() else None
        if halves is not None:
            logger.warning(
                "CodeReviewCoordinator: chunk review failed at depth %s (%s: %s) — bisecting [%s]",
                depth,
                type(exc).__name__,
                exc,
                chunk.paths_label,
            )
            outcome = _review_chunk_with_recovery(reviewer, halves[0], base_input, depth + 1)
            outcome.absorb(_review_chunk_with_recovery(reviewer, halves[1], base_input, depth + 1))
            return outcome
        if not retried:
            logger.warning(
                "CodeReviewCoordinator: chunk review failed (%s: %s) — retrying once [%s]",
                type(exc).__name__,
                exc,
                chunk.paths_label,
            )
            return _review_chunk_with_recovery(reviewer, chunk, base_input, depth, retried=True)
        raise CodeReviewUnavailableError(
            f"Chunk review failed after recovery attempts ({type(exc).__name__}: {exc}).",
            unreviewed=_chunk_ranges(chunk),
        ) from exc
    issues = _issues_from_chunk_output(chunk, output.issues)
    if not output.approved and not issues and output.summary and output.summary.strip():
        issues = [
            CodeReviewIssue(
                severity="high",
                category="general",
                file_path="",
                description=f"Code review rejected: {output.summary}",
                suggestion="Address the concerns described in the review summary. "
                "Ensure the code meets all acceptance criteria and follows project conventions.",
            )
        ]
    return _ChunkOutcome(
        issues=issues,
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
) -> Tuple[bool, List[CodeReviewIssue]]:
    """Deterministic approval gate with the anti-loop safety nets.

    Preconditions:
        - ``issues`` is the deduped merged issue list. Any rejecting
          sub-review's summary has already been synthesized into a high issue
          per sub-review (``_review_chunk_with_recovery``), so issue text and
          verdicts are correctly paired before they reach this gate.

    Postconditions:
        - ``approved is False`` implies the returned issues contain at least
          one critical/high finding (rejections are always actionable).
        - A reject with only minor/info issues, or with no actionable feedback
          at all, flips to approve. The merged summary is never consulted here:
          it mixes every chunk's text, so synthesizing a rejection from it
          could attribute an approving chunk's words to a rejecting chunk.
    """
    critical_or_high = [i for i in issues if i.severity in ("critical", "high")]
    approved = llm_approved and not critical_or_high
    if not approved and not critical_or_high:
        if issues:
            logger.info(
                "CodeReview: overriding to approved=True (only %s minor/nit issues, no critical/high)",
                len(issues),
            )
        else:
            logger.warning(
                "CodeReview: LLM rejected with no issues and no actionable feedback -- "
                "auto-approving (nothing to give the coding agent)"
            )
        approved = True
    return approved, issues


def _merge_narrative(
    llm: LLMClient,
    input_data: CodeReviewInput,
    approved: bool,
    issues: List[CodeReviewIssue],
    outcome: "_ChunkOutcome",
) -> Tuple[str, str]:
    """Produce the merged ``(summary, spec_compliance_notes)`` for the review.

    The reduce phase's narrative — never the verdict, which is already fixed.

    Preconditions:
        - ``approved`` and ``issues`` are the authoritative deterministic
          results from ``_reconcile_approval``; this function only shapes prose
          and never reconsults or mutates them.
        - ``outcome.summaries`` holds one entry per successful sub-review.

    Postconditions:
        - With exactly one sub-review, returns that sub-review's summary/notes
          verbatim and makes no synthesis LLM call.
        - With more than one sub-review, attempts a single findings-only
          synthesis pass; on any failure (``None``) falls back to the
          ``"\\n\\n"``-joined per-pass summaries/notes.
    """
    if len(outcome.summaries) == 1:
        return outcome.summaries[0], (outcome.spec_notes[0] if outcome.spec_notes else "")

    concatenated_summary = "\n\n".join(s for s in outcome.summaries if s.strip())
    concatenated_notes = "\n\n".join(n for n in outcome.spec_notes if n.strip())

    if len(outcome.summaries) > 1:
        synthesized = synthesize_review_findings(
            llm,
            input_data=input_data,
            approved=approved,
            issues=issues,
            chunk_summaries=outcome.summaries,
        )
        if synthesized is not None:
            return synthesized.summary, synthesized.spec_compliance_notes

    return concatenated_summary, concatenated_notes


def _map_chunks(
    chunk_reviewer: ChunkReviewAgent,
    chunks: List[ReviewChunk],
    base_input: Dict,
    progress_callback: Optional[ReviewProgressCallback] = None,
) -> List[_ChunkOutcome]:
    """Review all chunks, fanning out independent map calls.

    Preconditions:
        - ``chunk_reviewer`` is safe for concurrent ``run`` calls: the agent is
          stateless and ``_run_chunk_review`` builds a fresh strands agent and
          model per call, so the only object shared across workers is the
          injected LLM client, whose central implementations guard their own
          state (clients injected here must support concurrent calls).

    Postconditions:
        - Returns one outcome per chunk in input order, or raises
          ``CodeReviewUnavailableError``. The first failure is observed as it
          happens — never delayed behind an earlier, slower chunk — pending
          chunks are cancelled, and the exception propagates immediately;
          already-running reviews are left to finish in the background rather
          than blocking the failure behind in-flight model calls.
        - When ``progress_callback`` is provided, one ``reviewing`` report is
          emitted per completed chunk ("chunk i/N reviewed", i = completion
          order) with fractions in (0.10, 0.90]; the counter update and the
          callback run under one lock, so fractions stay non-decreasing even
          with parallel workers.
        - After a failure propagates, no further progress is ever reported:
          abandoned in-flight workers finish in the background with their
          callback suppressed, so stale "reviewing" reports can never
          overwrite the caller's failure state.
    """
    total = len(chunks)
    progress_lock = threading.Lock()
    completed_count = [0]
    abandoned = threading.Event()

    def _run_one(chunk: ReviewChunk) -> _ChunkOutcome:
        outcome = _review_chunk_with_recovery(chunk_reviewer, chunk, base_input)
        with progress_lock:
            if not abandoned.is_set():
                completed_count[0] += 1
                notify_review_progress(
                    progress_callback,
                    "reviewing",
                    f"chunk {completed_count[0]}/{total} reviewed: {chunk.paths_label[:120]}",
                    0.10 + 0.80 * completed_count[0] / total,
                )
        return outcome

    workers = min(_map_parallelism(), total)
    if workers <= 1:
        return [_run_one(c) for c in chunks]
    executor = ThreadPoolExecutor(max_workers=workers)
    try:
        futures = [executor.submit(_run_one, c) for c in chunks]
        # Wake on the first failure instead of joining futures in submission
        # order, so a fast failure is never hidden behind a slow earlier chunk.
        wait(futures, return_when=FIRST_EXCEPTION)
        for f in futures:
            if f.done() and f.exception() is not None:
                f.result()  # re-raises the worker's exception with its traceback
        results = [f.result() for f in futures]
    except BaseException:
        # Setting the flag under the progress lock guarantees any in-flight
        # report finishes before the failure propagates and none follows it.
        with progress_lock:
            abandoned.set()
        executor.shutdown(wait=False, cancel_futures=True)
        raise
    executor.shutdown(wait=True)
    return results


def run_coordinator(
    llm: LLMClient,
    input_data: CodeReviewInput,
    progress_callback: Optional[ReviewProgressCallback] = None,
) -> CodeReviewOutput:
    """Map-reduce review entry point: bounded chunks in, merged verdict out.

    Preconditions:
        - ``llm`` implements ``LLMClient`` (context sizing + chunk review calls).
        - ``input_data`` carries the code under review via ``files`` or ``code``.
        - ``progress_callback`` is None or satisfies the
          ``ReviewProgressCallback`` contract (non-raising, accepts
          ``(step, detail, fraction)``).

    Postconditions:
        - Every input file/line range is either reviewed or named: empty files
          get info findings, and any chunk that cannot be reviewed after
          recovery raises ``CodeReviewUnavailableError`` (no verdict is ever
          rendered on partially reviewed code).
        - ``approved is False`` implies at least one critical/high issue.
        - The code under review is never compacted or truncated; only the
          spec/architecture/existing-codebase excerpts are.
        - When ``progress_callback`` is provided, it is invoked with
          non-decreasing fractions ending at 1.0 (step ``done``) on every
          successful return, including per-chunk ``reviewing`` reports.

    Raises:
        CodeReviewUnavailableError: when the review model is unavailable or a
            chunk remains unreviewable after retry and bisection.
    """
    notify_review_progress(progress_callback, "preparing", "preparing review input", 0.05)
    blocks, skipped_empty = _blocks_from_input(input_data)
    skipped_issues = [
        CodeReviewIssue(
            severity="info",
            category="general",
            file_path=path,
            description="File content is empty or whitespace-only; nothing to review.",
            suggestion="Confirm the file is intentionally empty.",
        )
        for path in skipped_empty
    ]
    if not blocks:
        notify_review_progress(progress_callback, "done", "no code to review", 1.0)
        return CodeReviewOutput(
            approved=True,
            issues=skipped_issues,
            summary="No code to review.",
            spec_compliance_notes="",
            suggested_commit_message="",
        )

    max_spec = compute_code_review_spec_excerpt_chars(llm)
    max_arch = compute_code_review_arch_overview_chars(llm)
    max_existing = compute_code_review_existing_codebase_chars(llm)
    # Hard caps after compaction: compact_text returns the original text when
    # its LLM call fails, so the slice is what actually guarantees the chunk
    # reviewer's bounded-prompt precondition.
    spec_content = compact_text(input_data.spec_content or "", max_spec, llm, "specification")[
        :max_spec
    ]
    arch_overview = ""
    if input_data.architecture:
        arch_overview = compact_text(
            input_data.architecture.overview or "", max_arch, llm, "architecture overview"
        )[:max_arch]
    existing_codebase = compact_text(
        input_data.existing_codebase or "", max_existing, llm, "existing codebase"
    )[:max_existing]

    chunks = build_review_chunks(
        blocks, compute_code_review_map_chunk_chars(llm), input_data.pre_numbered
    )
    logger.info(
        "CodeReviewCoordinator: %s blocks -> %s chunks",
        len(blocks),
        len(chunks),
    )
    notify_review_progress(progress_callback, "preparing", f"split into {len(chunks)} chunks", 0.10)

    base_input = {
        "language": input_data.language or "",
        "task_description": input_data.task_description or "",
        "task_requirements": input_data.task_requirements or "",
        "acceptance_criteria": input_data.acceptance_criteria or [],
        "spec_excerpt": spec_content,
        "architecture_overview": arch_overview,
        "existing_codebase_excerpt": existing_codebase or None,
    }

    chunk_reviewer = ChunkReviewAgent(llm)
    outcome = _ChunkOutcome()
    for per_chunk in _map_chunks(chunk_reviewer, chunks, base_input, progress_callback):
        outcome.absorb(per_chunk)

    notify_review_progress(
        progress_callback, "finalizing", "deduplicating findings and applying approval rules", 0.95
    )
    deduped = _dedupe_issues([*outcome.issues, *skipped_issues])
    all_llm_approved = bool(outcome.approved_flags) and all(outcome.approved_flags)
    approved, deduped = _reconcile_approval(all_llm_approved, deduped)

    merged_summary, spec_notes = _merge_narrative(llm, input_data, approved, deduped, outcome)
    # A commit message synthesized from a fraction of the change is misleading,
    # so it is only forwarded when a single sub-review saw the whole submission
    # in one piece — a bisected recovery produces per-half messages and drops it.
    commit_message = ""
    if len(chunks) == 1 and len(outcome.commit_messages) == 1:
        commit_message = outcome.commit_messages[0]
        commit_message = commit_message if commit_message.strip() else ""

    logger.info(
        "CodeReviewCoordinator: done, approved=%s, issues=%s, chunks=%s (sub-reviews=%s)",
        approved,
        len(deduped),
        len(chunks),
        len(outcome.approved_flags),
    )

    notify_review_progress(
        progress_callback, "done", f"approved={approved}, issues={len(deduped)}", 1.0
    )
    return CodeReviewOutput(
        approved=approved,
        issues=deduped,
        summary=merged_summary,
        spec_compliance_notes=spec_notes,
        suggested_commit_message=commit_message,
    )
