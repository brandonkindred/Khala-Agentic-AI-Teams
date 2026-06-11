"""Models for the Code Review agent."""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, model_validator

from software_engineering_team.shared.models import SystemArchitecture


class CodeReviewUnavailableError(RuntimeError):
    """Raised when the review could not be completed, so no verdict exists.

    Distinguishes "the reviewer infrastructure failed" from "the code was
    rejected": callers must treat this as a failed review run (retry, mark the
    job failed), never as review feedback for the coding agent.

    Invariants:
        - ``unreviewed`` names the file/line ranges that received no review
          (empty when the failure happened before any chunk was attempted).
    """

    def __init__(self, message: str, unreviewed: Optional[List[str]] = None) -> None:
        super().__init__(message)
        self.unreviewed: List[str] = list(unreviewed or [])


def coerce_line(value: Any) -> Optional[int]:
    """Parse an LLM-provided line number into a positive int, or None.

    Postconditions:
        - Returns a positive ``int`` when ``value`` is a valid positive number;
          returns None for None, non-numeric, zero, or negative values (so a bad
          line never becomes an invalid inline-comment anchor).
    """
    if value is None:
        return None
    try:
        line = int(value)
    except (TypeError, ValueError):
        return None
    return line if line > 0 else None


class FileSegment(BaseModel):
    """A contiguous slice of one file under review.

    Invariants:
        - ``start_line`` is 1-based and within the original file.
        - A file's segments, in list order, partition its content exactly:
          concatenating their ``content`` reproduces the file.
        - ``pre_numbered`` segments carry original line numbers as ``N: `` prefixes
          (the coding team's PR-diff hunks), so they need no re-anchoring.
    """

    path: str = Field(default="", description="Original file path ('' for headerless code)")
    content: str = Field(description="The segment's slice of the file content")
    start_line: int = Field(
        default=1,
        description="1-based line number of the segment's first line in the original file",
    )
    total_lines: int = Field(default=1, description="Total line count of the original file")
    pre_numbered: bool = Field(
        default=False,
        description="True when content lines carry original line-number prefixes (e.g. '123: code')",
    )

    @property
    def line_count(self) -> int:
        """Number of lines in this segment's content."""
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
    def line_offset(self) -> int:
        """Offset to add to a snippet-relative line number to recover the original line.

        Postconditions:
            - Returns 0 for ``pre_numbered`` segments (their cited numbers are
              already original lines), else ``start_line - 1``.
        """
        return 0 if self.pre_numbered else self.start_line - 1


class ReviewChunk(BaseModel):
    """One map-call unit: a group of file segments reviewed in a single LLM call.

    Invariants:
        - No two segments share the same ``path`` (so ``offset_by_path`` is unambiguous).
    """

    segments: List[FileSegment] = Field(default_factory=list)

    @property
    def content(self) -> str:
        """Rendered ``### path ###`` blocks for the chunk prompt.

        Postconditions:
            - Headerless segments (``path == ''``) render as bare content.
        """
        parts = []
        for seg in self.segments:
            parts.append(f"### {seg.path} ###\n{seg.content}" if seg.path else seg.content)
        return "\n\n".join(parts)

    @property
    def paths_label(self) -> str:
        """Human-readable label of the chunk's files, marking partial segments.

        Postconditions:
            - Partial segments render as ``path (lines A-B of N)``; whole files as ``path``.
        """
        labels = []
        for seg in self.segments:
            name = seg.path or "(unknown)"
            if seg.is_partial:
                labels.append(
                    f"{name} (lines {seg.start_line}-{seg.end_line} of {seg.total_lines})"
                )
            else:
                labels.append(name)
        return ", ".join(labels)

    @property
    def offset_by_path(self) -> Dict[str, int]:
        """Map of segment path to its ``line_offset`` for issue re-anchoring."""
        return {seg.path: seg.line_offset for seg in self.segments}


class ChunkReviewInput(BaseModel):
    """Input for reviewing one chunk of code (used by ChunkReviewAgent)."""

    code_chunk: str = Field(
        description="Code to review (one or more files, sized per model context)"
    )
    file_path_or_label: str = Field(
        default="",
        description="File path(s) in this chunk for issue reporting",
    )
    segment_note: str = Field(
        default="",
        description="Reviewer guidance for split or pre-numbered segments (prepended to the prompt)",
    )
    language: str = Field(
        default="",
        description="Primary language of the code under review ('' lets the reviewer guess)",
    )
    task_description: str = Field(default="", description="Task the coding agent was working on")
    task_requirements: str = Field(default="", description="Detailed task requirements")
    acceptance_criteria: List[str] = Field(
        default_factory=list,
        description="Acceptance criteria the code must meet",
    )
    spec_excerpt: str = Field(default="", description="Spec excerpt (capped ~8K)")
    architecture_overview: str = Field(default="", description="Architecture overview (capped ~2K)")
    existing_codebase_excerpt: Optional[str] = Field(
        default=None,
        description="Existing codebase excerpt (capped ~4K)",
    )


class ChunkReviewOutput(BaseModel):
    """Output from reviewing one chunk (approved, issues, summary for this chunk)."""

    approved: bool = Field(default=False, description="True if chunk has no critical/high issues")
    issues: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Issues found (each with severity, category, file_path, description, suggestion)",
    )
    summary: str = Field(default="", description="Review summary for this chunk")
    spec_compliance_notes: str = Field(
        default="",
        description="Notes on how well the chunk meets the spec and acceptance criteria",
    )
    suggested_commit_message: str = Field(
        default="",
        description="Optional commit message suggestion from the reviewer",
    )


class CodeReviewIssue(BaseModel):
    """A single issue found during code review."""

    severity: str = Field(
        default="high",
        description="Severity: critical, high, medium, low, or info",
    )
    category: str = Field(
        default="general",
        description="Category: naming, structure, logic, spec-compliance, standards, integration, testing",
    )
    file_path: str = Field(
        default="",
        description="File path where the issue was found",
    )
    line: Optional[int] = Field(
        default=None,
        description="1-based line number in the NEW version of file_path where the issue occurs, "
        "when the issue is tied to a specific line. None for file-wide/structural issues.",
    )
    start_line: Optional[int] = Field(
        default=None,
        description="Optional start line for a multi-line issue; `line` is then the end line.",
    )
    description: str = Field(
        default="",
        description="Clear description of the issue",
    )
    suggestion: str = Field(
        default="",
        description="Concrete suggestion for how to fix the issue",
    )


class CodeReviewInput(BaseModel):
    """Input for the Code Review agent.

    Preconditions (enforced at construction):
        - The code under review is provided either via ``files`` (non-empty
          mapping) or via an explicitly passed ``code`` string. Constructing
          the input with neither, or with ``files={}``, raises ``ValueError``
          so a caller bug never silently becomes an approved empty review.
    """

    code: str = Field(
        default="",
        description="Legacy input: code to review, concatenated with ### path ### file headers. "
        "Ignored when ``files`` is provided.",
    )
    files: Optional[Dict[str, str]] = Field(
        default=None,
        description="Preferred input: mapping of file path to file content. "
        "When set, ``code`` is ignored and no header parsing happens.",
    )
    pre_numbered: bool = Field(
        default=False,
        description="True when every content line already carries its original line number "
        "as an 'N: ' prefix (the coding team's PR-diff hunks); issue lines are then "
        "reported verbatim instead of re-anchored.",
    )
    spec_content: str = Field(
        default="",
        description="Full project specification to check code against",
    )
    task_description: str = Field(
        default="",
        description="The task the coding agent was working on",
    )
    task_requirements: str = Field(
        default="",
        description="Detailed requirements for the task",
    )
    acceptance_criteria: List[str] = Field(
        default_factory=list,
        description="Acceptance criteria the code must meet",
    )
    language: str = Field(
        default="typescript",
        description="Primary language: typescript (React/Angular/Vue) or python (FastAPI)",
    )
    architecture: Optional[SystemArchitecture] = None
    existing_codebase: Optional[str] = Field(
        default=None,
        description="Existing code in the repo before the agent's changes",
    )

    @model_validator(mode="after")
    def _require_code_or_files(self) -> "CodeReviewInput":
        """Reject inputs that carry no code source at all.

        ``files={}`` and a fully-defaulted ``code`` are caller bugs (e.g. a glob
        miss or a dropped kwarg), not empty reviews; per DbC they must raise here
        rather than fail open downstream. An explicitly passed empty ``code``
        string remains valid (the review then reports nothing to review).
        """
        if self.files is not None:
            if not self.files:
                raise ValueError("CodeReviewInput.files must be a non-empty mapping when provided")
            return self
        if "code" not in self.model_fields_set:
            raise ValueError("CodeReviewInput requires either 'files' or an explicit 'code' value")
        return self


class CodeReviewOutput(BaseModel):
    """Output from the Code Review agent."""

    approved: bool = Field(
        default=False,
        description="True when code passes review (no critical or high issues). Only approve when code is production-ready.",
    )
    issues: List[CodeReviewIssue] = Field(
        default_factory=list,
        description="List of issues found during code review",
    )
    summary: str = Field(
        default="",
        description="Overall summary of the code review",
    )
    spec_compliance_notes: str = Field(
        default="",
        description="Notes on how well the code meets the specification and acceptance criteria",
    )
    suggested_commit_message: str = Field(
        default="",
        description="Conventional Commits format, if reviewer wants to suggest a better message",
    )
