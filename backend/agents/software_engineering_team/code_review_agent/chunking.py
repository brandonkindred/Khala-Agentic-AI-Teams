"""Chunking for the code-review coordinator: input → blocks → segments → chunks.

Pure, LLM-free transforms that turn a review submission into bounded
``ReviewChunk``s and normalize a chunk reviewer's raw issue dicts back into
validated ``CodeReviewIssue``s. The map phase (``mapping.py``) and the
orchestrator (``coordinator.py``) build on these; nothing here calls an LLM.

Every function is bounded and total: concatenating a block's segments reproduces
it exactly, every block is covered by exactly one chunk, and issue normalization
never raises on malformed model output (it sanitizes instead).
"""

from __future__ import annotations

import re
from typing import List, Optional, Tuple

from software_engineering_team.shared.context_sizing import parse_env_int

from .code_boundaries import preferred_break_lines
from .models import (
    CodeReviewInput,
    CodeReviewIssue,
    FileSegment,
    ReviewChunk,
    coerce_line,
    derive_issue_title,
    is_no_op_suggestion,
)

# First capture: the original line number embedded in a pre-numbered line
# (live ``N| `` gutter, or legacy ``N: ``). Optional leading spaces are
# width-padding on the live format, not source indent.
_PRENUMBERED_LINE_RE = re.compile(r"^[ ]*(\d+)(?::|\|)")

# Suffix that ``ReviewChunk.paths_label`` appends to partial segments; stripped
# when the model echoes it back inside an issue's file_path.
_LINES_SUFFIX_RE = re.compile(r"\s*\(lines \d+-\d+ of \d+\)\s*$")

# A failing chunk is bisected and retried; below this content size it gets one
# same-input retry instead, and past the depth cap the run fails loudly.
# Both knobs are env-overridable (see docs/ENV_VARS.md).
MIN_SPLIT_SEGMENT_CHARS = 8_000  # CODE_REVIEW_MIN_SPLIT_SEGMENT_CHARS, floor 1_000
MAX_CHUNK_BISECT_DEPTH = 3  # CODE_REVIEW_MAX_BISECT_DEPTH, floor 0
DEFAULT_MAP_PARALLELISM = (
    16  # CODE_REVIEW_MAP_PARALLELISM ceiling, floor 1; clamped by LLM_MAX_CONCURRENCY
)

_BLOCK_JOINER_CHARS = 2  # "\n\n" between rendered blocks in a chunk

_VALID_SEVERITIES = frozenset({"critical", "high", "medium", "low", "info"})
# Mirrors CodeReviewIssue.category's documented set (models.py) plus "general",
# the synthetic fallback used for issues with no specific category (e.g. a
# rejected-with-no-issues summary or an unreviewed-range notice).
_VALID_CATEGORIES = frozenset(
    {
        "naming",
        "structure",
        "logic",
        "spec-compliance",
        "standards",
        "integration",
        "testing",
        "architecture",
        "refactor",
        "maintainability",
        "side-effects",
        "documentation",
        "general",
    }
)


def _min_split_segment_chars() -> int:
    return parse_env_int("CODE_REVIEW_MIN_SPLIT_SEGMENT_CHARS", MIN_SPLIT_SEGMENT_CHARS, 1_000)


def _max_bisect_depth() -> int:
    return parse_env_int("CODE_REVIEW_MAX_BISECT_DEPTH", MAX_CHUNK_BISECT_DEPTH, 0)


def _map_parallelism() -> int:
    """Configured map-phase fan-out ceiling, clamped by the process-global LLM gate.

    Postconditions:
        - Returns ``min(CODE_REVIEW_MAP_PARALLELISM, LLM_MAX_CONCURRENCY)``, floored
          at 1, so raising this ceiling for a large review can never request more
          concurrent LLM calls than the process-wide semaphore allows regardless of
          what else is in flight.
    """
    from llm_service.concurrency import get_llm_max_concurrency

    ceiling = parse_env_int("CODE_REVIEW_MAP_PARALLELISM", DEFAULT_MAP_PARALLELISM, 1)
    return max(1, min(ceiling, get_llm_max_concurrency()))


def _blocks_from_input(input_data: CodeReviewInput) -> Tuple[List[Tuple[str, str]], List[str]]:
    """Resolve the review input into ordered (path, content) blocks.

    Preconditions:
        - ``input_data`` is a valid ``CodeReviewInput`` (its validator already
          guarantees ``files`` is a non-empty mapping).

    Postconditions:
        - One block per file with non-blank content, insertion order preserved.
        - No returned block has blank content; the second element names every
          non-blank path whose content was blank, so the caller can report the
          skip instead of silently dropping the file.
    """
    skipped: List[str] = []
    blocks = []
    for path, content in input_data.files.items():
        if content and content.strip():
            blocks.append((path, content))
        else:
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
        - Cuts prefer function/method/class boundaries: when an over-budget
          buffer contains the start of a top-level construct, the split lands
          before that construct so it is not severed mid-body. When no such
          boundary exists in the buffer (minified code, one giant function,
          unparseable or pre-numbered content), the split falls back to the
          line boundary, keeping the other postconditions intact.
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
    # Lines (1-based) that start a top-level construct; cutting before one keeps
    # the preceding construct whole. Pre-numbered hunks carry "N| " prefixes that
    # defeat boundary detection and are rarely whole functions, so they keep the
    # plain line-boundary behavior (empty break set).
    breaks = frozenset() if pre_numbered else preferred_break_lines(path, content)
    # Split pieces become partial segments, which render with "N| " prefixes
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
            # Prefer the latest construct boundary inside the buffer so the
            # flushed head ends right before a function/method/class. Candidates
            # are strictly after buf_start (a non-empty head) and at most the
            # current line; with none, fall back to the line boundary.
            cut = max((b for b in breaks if buf_start < b <= line_no), default=None)
            if cut is not None:
                head = buf[: cut - buf_start]
                pieces.append((buf_start, "".join(head)))
                buf = buf[cut - buf_start :]
                buf_start = cut
                buf_len = sum(len(x) + prefix_width for x in buf)
            else:
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


def _prenumbered_line_numbers(seg: FileSegment) -> List[int]:
    """Parse the embedded ``N| `` / ``N: `` line-number prefixes of a pre-numbered segment.

    Postconditions:
        - Returns the parsed prefixes in content order; empty when the segment
          is not pre-numbered or carries no parseable prefix.
    """
    if not seg.pre_numbered:
        return []
    return [
        int(m.group(1))
        for line in seg.content.splitlines()
        if (m := _PRENUMBERED_LINE_RE.match(line)) is not None
    ]


def _segment_line_range(seg: FileSegment) -> Tuple[int, int]:
    """Return the ``(start, end)`` original line numbers a segment covers.

    Postconditions:
        - Pre-numbered segments derive the range from their first/last embedded
          ``N:`` prefixes — the positional ``start_line``/``end_line`` are
          meaningless for PR-diff hunks, so a cited range stays aligned with the
          real diff lines ``map_issues_to_comments`` anchors against.
        - Plain segments (and pre-numbered segments with no parseable prefix)
          fall back to the positional ``start_line``/``end_line``.
    """
    numbers = _prenumbered_line_numbers(seg)
    if numbers:
        return numbers[0], numbers[-1]
    return seg.start_line, seg.end_line


def cap_chunk_content(content: str, max_chars: int) -> List[str]:
    """Split content over ``max_chars`` into consecutive ≤``max_chars`` pieces.

    Safety net for the one case ``build_review_chunks`` / ``split_block_into_segments``
    cannot bound — a single source line longer than ``max_chars`` (minified bundle,
    long one-line data literal) — which those functions return as one over-budget chunk
    by contract. Callers feed each returned piece to an agent so no over-budget string
    is ever sent (and then silently skipped on context overflow) unreviewed.

    Preconditions:
        - ``max_chars`` > 0.

    Postconditions:
        - ``"".join(result) == content`` (no content dropped or duplicated).
        - Every returned piece has ``len`` ≤ ``max_chars``.
        - ``content`` already ≤ ``max_chars`` yields exactly ``[content]`` (the common
          path).
    """
    assert max_chars > 0, "max_chars must be positive"
    if len(content) <= max_chars:
        return [content]
    return [content[i : i + max_chars] for i in range(0, len(content), max_chars)]


def cap_review_chunk(chunk: ReviewChunk, max_chars: int) -> List[str]:
    """Render a chunk into prompt pieces each ≤ ``max_chars``, keeping the file
    header on every piece of an over-budget single-segment chunk.

    For the code-review prompt the rendered ``chunk.content`` (``### path ###``
    headers, original-line-number prefixes) must be preserved for line anchoring,
    so a plain character split of ``chunk.content`` would strand tail pieces
    without their file header and make findings unattributable. This keeps the
    header on each piece instead.

    Preconditions:
        - ``max_chars`` > 0.

    Postconditions:
        - ``chunk.content`` ≤ ``max_chars`` yields ``[chunk.content]`` (the common
          path).
        - An over-budget chunk (one segment whose rendered content exceeds the cap
          — a line longer than the cap) yields multiple pieces, each ≤ ``max_chars``.
          When the segment's ``### path ###`` header itself fits under the cap,
          each piece is prefixed with it (header counts against the budget) so a
          finding in any piece stays attributable; when the header alone would
          meet or exceed the cap, headers are dropped and the raw content is
          split instead — every piece staying ≤ ``max_chars`` always wins over
          preserving attribution.
    """
    assert max_chars > 0, "max_chars must be positive"
    content = chunk.content
    if len(content) <= max_chars:
        return [content]
    # build_review_chunks places an oversized segment alone, so an over-budget
    # chunk holds exactly one segment; re-attach its header to every body piece,
    # unless the header itself would already blow the budget.
    if len(chunk.segments) == 1 and chunk.segments[0].path:
        seg = chunk.segments[0]
        header = f"### {seg.path} ###\n"
        if len(header) < max_chars:
            body_budget = max_chars - len(header)
            return [header + piece for piece in cap_chunk_content(seg.prompt_content, body_budget)]
    # Headerless (path == ""), header alone ≥ max_chars, or, defensively,
    # multi-segment: fall back to a raw character split — there is no
    # per-piece header that can fit within the budget.
    return cap_chunk_content(content, max_chars)


def _segment_range_label(seg: FileSegment) -> str:
    """Describe the original-file line range a segment covers.

    Postconditions:
        - Pre-numbered segments report the first/last embedded ``N:`` prefixes
          (their positional indices are meaningless); plain segments report
          ``start_line``–``end_line`` of ``total_lines``.
    """
    name = seg.path or "(headerless code)"
    numbers = _prenumbered_line_numbers(seg)
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
                f"The lines of {name} carry their original line-number prefixes (e.g. `123| code`); "
                "set `line` to those exact prefixed numbers. The `N| ` gutter is metadata, not "
                "source: ignore it when judging indentation. A continuation line indented 4 spaces "
                "past its opening `(` / `[` / `{` is standard hanging indent, not extra whitespace."
            )
        elif seg.is_partial:
            notes.append(
                f"{name} is shown only from original line {seg.start_line} to {seg.end_line} "
                f"(of {seg.total_lines} total), and every line carries its original line-number "
                "prefix (e.g. `123| code`); set `line` to those exact prefixed numbers. The "
                "`N| ` gutter is metadata, not source: ignore it when judging indentation."
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
    """Coerce an untrusted LLM field to a stripped string, falling back to ``default`` when blank.

    Postconditions:
        - Returns ``default`` for None/blank values; never raises.
    """
    if value is None:
        return default
    text = str(value).strip()
    return text or default


def _coerce_bool(value: object) -> bool:
    """Coerce an untrusted LLM field to a bool, tolerating string encodings.

    LLMs emit booleans inconsistently — as a JSON ``true``/``false``, as the
    string ``"true"``/``"yes"``/``"1"``, or omitted entirely — so a bare
    ``bool(value)`` would read the string ``"false"`` as True. Mirrors the
    strict-coercion convention coding_team's tech_lead_agent uses for the same
    LLM-flag-drift problem: only a real ``True`` or a recognized truthy string
    counts, so an unexpected type (a bare number, a list, ...) is never
    silently treated as true.

    Postconditions:
        - Returns True only for the bool ``True`` or a recognized truthy
          string token (``true``/``yes``/``1``, case-insensitive); False for
          None, any number, an unrecognized string (including ``"false"``/
          ``"no"``), or any other value. Never raises.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "yes", "1"}
    return False


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
        - ``pre_existing`` reflects the LLM's optional per-issue tag (coerced via
          ``_coerce_bool``); it defaults to False when the field is absent, so a
          reviewer/gate that never emits it is unaffected.
        - An item whose ``suggestion`` is, in its entirety, a no-op phrasing
          (e.g. "No changes needed.") is dropped (see ``is_no_op_suggestion``):
          the reviewer's own suggested fix says there is nothing to do, so it
          is not a reportable issue.
        - Every returned issue's ``title`` is non-blank whenever ``description``
          is (the LLM's own title when given, otherwise
          ``derive_issue_title(description)``), so every finding that reaches
          a PR comment has a title to display.
    """
    seg_by_path = {seg.path: seg for seg in chunk.segments}
    issues: List[CodeReviewIssue] = []
    for item in raw_issues:
        if not isinstance(item, dict):
            continue
        description = _clean_str(item.get("description"), "")
        if not description:
            continue
        suggestion = _clean_str(item.get("suggestion"), "")
        if is_no_op_suggestion(suggestion):
            continue
        path = _normalize_issue_path(_clean_str(item.get("file_path"), ""), chunk)
        seg = seg_by_path.get(path)
        severity = _clean_str(item.get("severity"), "high").lower()
        if severity not in _VALID_SEVERITIES:
            severity = "high"
        category = _clean_str(item.get("category"), "general").lower()
        if category not in _VALID_CATEGORIES:
            category = "general"
        title = _clean_str(item.get("title"), "") or derive_issue_title(description)
        issues.append(
            CodeReviewIssue(
                severity=severity,
                category=category,
                file_path=path,
                line=_validate_line(coerce_line(item.get("line")), seg),
                start_line=_validate_line(coerce_line(item.get("start_line")), seg),
                title=title,
                description=description,
                suggestion=suggestion,
                pre_existing=_coerce_bool(item.get("pre_existing")),
            )
        )
    return issues


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


def _chunk_ranges(chunk: ReviewChunk) -> List[str]:
    """Name every original-file line range the chunk covers.

    Postconditions:
        - Returns one human-readable label per segment, in segment order, via
          ``_segment_range_label`` — used to name the ranges left unreviewed
          when a chunk fails.
    """
    return [_segment_range_label(seg) for seg in chunk.segments]
