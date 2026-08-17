"""DbC-scoped chunking: bound a large multi-file code input into chunks.

Self-contained, LLM-free chunk-building for DbcCommentsAgent. Deliberately
does not import anything from ``code_review_agent``: its ``chunking``/
``models`` submodules are that package's own internals, not part of its
public, PEP 562-lazy ``__getattr__`` surface (see
``code_review_agent/__init__.py``'s module docstring), so reaching into them
from another agent bypasses the encapsulation those lazy exports exist to
provide. Only depends on plain string/line arithmetic.

A DbC review doesn't need code-review's "pre_numbered" (PR-diff hunk)
rendering mode or its function/class-boundary-preferring cuts, so this is a
narrower reimplementation, not a port: ``build_dbc_chunks`` groups whole
files up to a char budget and, in the rare case a single file alone exceeds
it, falls back to a plain line-boundary split (never a function/class
boundary-aware one).
"""

from __future__ import annotations

import re
from typing import List, Tuple

from pydantic import BaseModel, Field, model_validator

# "\n\n" between two segments' rendered content in one chunk.
_BLOCK_JOINER_CHARS = 2

# Pattern: a whole line of the form "### path/to/file ###". Anchored to line
# boundaries with a single-line path so header-like fragments inside source
# (markdown headings, "### x" comments, mid-line strings) can never match,
# and a false header can never swallow lines of code the way an unanchored
# DOTALL pattern could.
_FILE_HEADER_PATTERN = re.compile(r"^###[ \t]+(\S[^\n]*?)[ \t]+###[ \t]*\n", re.MULTILINE)


def parse_code_into_file_blocks(code: str) -> List[Tuple[str, str]]:
    """
    Parse concatenated code into (path, content) blocks using ### path ### pattern.
    Returns list of (file_path, content) tuples.

    Only a complete line of the form ``### path ###`` counts as a header, so a
    header can never span source lines. A source line that happens to match
    that exact shape (e.g. inside a docstring) is still read as a header — an
    inherent ambiguity of DbC's concatenated ``code`` transport; callers whose
    content may contain such lines have no alternative today (DbC has no
    per-file ``files=`` input).

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


class DbcFileSegment(BaseModel):
    """A contiguous, 1-based slice of one file's content.

    Invariants:
        - ``start_line`` is 1-based and within the original file.
        - A file's segments, in list order, partition its content exactly:
          concatenating their ``content`` reproduces the file.
    """

    path: str = Field(default="", description="Original file path ('' for headerless code)")
    content: str = Field(description="The segment's slice of the file content")
    start_line: int = Field(
        default=1,
        description="1-based line number of the segment's first line in the original file",
    )
    total_lines: int = Field(default=1, description="Total line count of the original file")

    @property
    def line_count(self) -> int:
        """Number of lines in this segment's content (never 0 for non-empty content)."""
        return len(self.content.splitlines()) or 1

    @property
    def end_line(self) -> int:
        """1-based line number of the segment's last line in the original file."""
        return self.start_line + self.line_count - 1

    @property
    def is_partial(self) -> bool:
        """True when the segment covers only part of its file."""
        return self.start_line > 1 or self.end_line < self.total_lines

    @property
    def prompt_content(self) -> str:
        """The content as rendered into the review prompt.

        Postconditions:
            - A whole-file segment renders verbatim as ``content``.
            - A partial segment is prefixed with its original line numbers
              (``"50: code"``) so a cited line is absolute by construction.
        """
        if not self.is_partial:
            return self.content
        return "\n".join(
            f"{self.start_line + i}: {line}" for i, line in enumerate(self.content.splitlines())
        )

    @model_validator(mode="after")
    def _validate_segment(self) -> "DbcFileSegment":
        """Enforce the geometric invariants documented on this class.

        Postconditions:
            - ``start_line`` is >= 1; ``total_lines`` is at least ``line_count``;
              ``end_line`` does not extend past ``total_lines``.
        """
        if self.start_line < 1:
            raise ValueError("start_line must be 1-based")
        if self.total_lines < self.line_count:
            raise ValueError("total_lines must be at least line_count")
        if self.end_line > self.total_lines:
            raise ValueError("segment extends past end of file")
        return self


class DbcChunk(BaseModel):
    """One LLM-call unit: a group of file segments reviewed together.

    Invariants:
        - No two segments share the same ``path`` (so an insertion's cited
          file resolves to exactly one segment).
    """

    segments: List[DbcFileSegment] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_unique_paths(self) -> "DbcChunk":
        """Reject a chunk holding two segments of the same path.

        Postconditions:
            - Every ``path`` in ``segments`` appears at most once.
        """
        paths = [seg.path for seg in self.segments]
        if len(paths) != len(set(paths)):
            raise ValueError("a chunk cannot hold two segments of the same path")
        return self

    @property
    def content(self) -> str:
        """Rendered ``### path ###`` blocks for the chunk prompt.

        Postconditions:
            - A headerless segment (``path == ''``) renders as bare content.
        """
        parts = []
        for seg in self.segments:
            rendered = seg.prompt_content
            parts.append(f"### {seg.path} ###\n{rendered}" if seg.path else rendered)
        return "\n\n".join(parts)


def _split_block_into_segments(path: str, content: str, max_chars: int) -> List[DbcFileSegment]:
    """Split one file block into line-boundary segments of at most ``max_chars``.

    Preconditions:
        - ``max_chars`` > 0.

    Postconditions:
        - Concatenating segment contents in order reproduces ``content`` exactly.
        - Each segment's rendered size is <= ``max_chars``, except when a
          single line alone exceeds it (line boundaries are never broken).
        - A within-budget block yields exactly one whole-file segment.
    """
    assert max_chars > 0, "max_chars must be positive"
    total_lines = len(content.splitlines()) or 1
    if len(content) <= max_chars:
        return [DbcFileSegment(path=path, content=content, start_line=1, total_lines=total_lines)]
    # Partial segments render with "N: " prefixes; budget each line's
    # rendered size so the prompt stays within max_chars after prefixing.
    prefix_width = len(str(total_lines)) + 2
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
        DbcFileSegment(path=path, content=text, start_line=start, total_lines=total_lines)
        for start, text in pieces
    ]


def build_dbc_chunks(blocks: List[Tuple[str, str]], max_chars: int) -> List[DbcChunk]:
    """Group file blocks into chunks whose rendered content is <= ``max_chars``.

    Preconditions:
        - ``max_chars`` > 0.

    Postconditions:
        - Every input block is fully covered exactly once across the
          returned chunks: no file or line range is dropped or duplicated.
        - No chunk holds two segments of the same path.
        - Each chunk's rendered ``content`` is <= ``max_chars``, except a
          chunk holding a single segment that alone exceeds the budget (a
          single line longer than the cap), which is placed alone.
    """
    assert max_chars > 0, "max_chars must be positive"
    chunks: List[DbcChunk] = []
    current: List[DbcFileSegment] = []
    current_len = 0

    def _rendered_len(seg: DbcFileSegment) -> int:
        header = len(f"### {seg.path} ###\n") if seg.path else 0
        return header + len(seg.prompt_content)

    def _flush() -> None:
        nonlocal current, current_len
        if current:
            chunks.append(DbcChunk(segments=current))
            current = []
            current_len = 0

    for path, content in blocks:
        header_len = len(f"### {path} ###\n") if path else 0
        seg_budget = max(1, max_chars - header_len)
        for seg in _split_block_into_segments(path, content, seg_budget):
            seg_len = _rendered_len(seg)
            if seg_len > max_chars:
                _flush()
                chunks.append(DbcChunk(segments=[seg]))
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
