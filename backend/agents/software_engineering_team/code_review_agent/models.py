"""Models for the Code Review agent."""

import logging
from typing import Any, Callable, Dict, List, Optional

from pydantic import BaseModel, Field, model_validator

from software_engineering_team.shared.models import SystemArchitecture

ReviewProgressCallback = Callable[[str, str, float], None]
"""Progress callback signature: (step, detail, fraction in [0.0, 1.0]).

Steps: ``preparing | reviewing | waiting_retry | parsing | finalizing | done``.

Preconditions (on the callable a caller provides):
    - Should not raise; review progress is observability, never control flow.
      If it raises anyway, the exception is logged and swallowed at the
      invocation boundary (see notify_review_progress) — it can never change
      a review's result.
    - Must accept (str, str, float) positionally.
"""


def notify_review_progress(
    callback: Optional[ReviewProgressCallback], step: str, detail: str, fraction: float
) -> None:
    """Invoke a review progress callback, or no-op when none is provided.

    A misbehaving callback is a reporter bug, but every consequence stays on the
    observability side: an out-of-range fraction is logged and clamped, and a
    raising callback is logged and swallowed. Letting either propagate would
    abort a healthy review (the call sites' broad ``except`` then silently
    diverts to the lower-fidelity LLM fallback) — a status-store hiccup must
    never change what the reviewer concluded. This mirrors the Tech Lead
    ``_report`` and ``call_llm_with_retries`` ``on_retry`` guards.

    Postconditions:
        - When ``callback`` is None, has no effect.
        - Otherwise ``callback(step, detail, clamped_fraction)`` was attempted
          exactly once; any exception it raised was logged at warning level and
          swallowed. Never raises.
    """
    if callback is None:
        return
    if not 0.0 <= fraction <= 1.0:
        logging.getLogger(__name__).warning(
            "review progress fraction out of range (step=%s): %r; clamping", step, fraction
        )
        fraction = min(max(fraction, 0.0), 1.0)
    try:
        callback(step, detail, fraction)
    except Exception as e:  # noqa: BLE001 — observability must not break the review
        logging.getLogger(__name__).warning(
            "review progress callback failed (ignored; step=%s): %s", step, e
        )


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
        - Cited line numbers are always original-file absolute: ``pre_numbered``
          segments already carry ``N: `` prefixes in their content (the coding
          team's PR-diff hunks), and partial segments are *rendered* with their
          original line numbers prefixed (``prompt_content``), so the reviewer
          never reports snippet-relative numbers that would need re-anchoring.
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
    def prompt_content(self) -> str:
        """The content as rendered into the review prompt.

        Partial segments are prefixed with their original line numbers
        (``"50: code"``) so cited lines are absolute by construction — a
        snippet-relative citation and an absolute one are indistinguishable
        when the segment's absolute range overlaps ``[1, line_count]``, so the
        numbering convention removes the ambiguity instead of guessing.

        Postconditions:
            - Whole files and ``pre_numbered`` segments (which already carry
              prefixes) render verbatim as ``content``.
        """
        if self.pre_numbered or not self.is_partial:
            return self.content
        return "\n".join(
            f"{self.start_line + i}: {line}" for i, line in enumerate(self.content.splitlines())
        )


class ReviewChunk(BaseModel):
    """One map-call unit: a group of file segments reviewed in a single LLM call.

    Invariants:
        - No two segments share the same ``path`` (so an issue's cited path
          resolves to exactly one segment for line validation).
    """

    segments: List[FileSegment] = Field(default_factory=list)

    @property
    def content(self) -> str:
        """Rendered ``### path ###`` blocks for the chunk prompt.

        Postconditions:
            - Headerless segments (``path == ''``) render as bare content.
            - Partial segments render with original line-number prefixes
              (see ``FileSegment.prompt_content``).
        """
        parts = []
        for seg in self.segments:
            rendered = seg.prompt_content
            parts.append(f"### {seg.path} ###\n{rendered}" if seg.path else rendered)
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
    user_decisions: Optional[List[str]] = Field(
        default=None,
        description="Product/design questions the user has already answered ('question → answer' "
        "lines); the reviewer treats them as settled, not as open issues to flag.",
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
    user_decisions: Optional[List[str]] = Field(
        default=None,
        description="Product/design questions the user has already answered ('question → answer' "
        "lines); the reviewer treats them as settled facts, not as open issues to flag.",
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


def build_code_review_input(
    *,
    files: Optional[Dict[str, str]] = None,
    code: Optional[str] = None,
    **fields: Any,
) -> CodeReviewInput:
    """Construct a :class:`CodeReviewInput` passing exactly the source channel given.

    ``files`` (the preferred ``{path: content}`` mapping) and ``code`` (the legacy
    path-headered blob) are forwarded only when not None, so an explicitly-passed
    empty ``code`` still counts as provided and ``files`` takes precedence —
    matching the model's own ``_require_code_or_files`` validation. Callers supply
    the remaining fields (spec_content, task_description, ...) via *fields*.

    Single source of truth for the files/code selection that backend_agent,
    orchestrator, and quality_gate_tools each used to duplicate.
    """
    kwargs: Dict[str, Any] = dict(fields)
    if files is not None:
        kwargs["files"] = files
    if code is not None:
        kwargs["code"] = code
    return CodeReviewInput(**kwargs)
