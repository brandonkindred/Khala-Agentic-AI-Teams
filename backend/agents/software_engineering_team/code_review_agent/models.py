"""Models for the Code Review agent."""

import logging
import re
from typing import Any, Callable, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, StrictBool, model_validator

from software_engineering_team.shared.models import SystemArchitecture

from .profiles import ReviewProfile

ReviewProgressCallback = Callable[[str, str, float], None]
"""Progress callback signature: (step, detail, fraction in [0.0, 1.0]).

Steps: ``preparing | reviewing | waiting_retry | parsing | verifying | finalizing | done``.

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


# Whole-string (not substring) match so a real, substantive suggestion that
# merely mentions "no changes"/"no action" in passing is never dropped -- only
# a suggestion that IS, in its entirety, an admission that nothing needs to
# change.
_NO_OP_SUGGESTION_RE = re.compile(
    r"^(?:no (?:code )?(?:changes?|fix(?:es)?|action) (?:is |are )?"
    r"(?:needed|required|necessary)|nothing to (?:change|fix|do))\.?$",
    re.IGNORECASE,
)


def is_no_op_suggestion(suggestion: str) -> bool:
    """True when a suggestion states, in full, that no code change is needed.

    A finding whose own suggested fix says "no change(s) needed/required" (or
    an equivalent no-op phrasing) is the reviewer's own admission that there
    is nothing to do -- it is not an actionable issue and must not be
    reported as one.

    Postconditions:
        - Returns True only when ``suggestion``, stripped, matches a known
          no-op phrasing in its entirety (case-insensitive); a suggestion
          that contains such wording alongside other content is NOT a match.
        - A blank/None suggestion returns False -- no suggestion given is not
          the same as an explicit "no change needed".
    """
    text = (suggestion or "").strip()
    if not text:
        return False
    return bool(_NO_OP_SUGGESTION_RE.match(text))


_TITLE_MAX_LEN = 80


def derive_issue_title(description: str, max_len: int = _TITLE_MAX_LEN) -> str:
    """Derive a short, descriptive title from an issue's description.

    Fallback for a finding that reaches a PR comment with no explicit title
    (e.g. from an auxiliary pass that constructs a ``CodeReviewIssue``
    directly rather than through the LLM-authored chunk-review schema):
    every finding posted as a PR comment must display a title, so this
    guarantees one exists from data the finding already carries.

    Preconditions:
        - ``max_len`` is a positive integer.

    Postconditions:
        - Returns the description's first line, trimmed to at most
          ``max_len`` characters TOTAL (including a trailing "…" when
          truncated); prefers breaking at a word boundary, but falls back to a
          hard character boundary when the first word is longer than the limit.
          Returns "" only when ``description`` is blank.
    """
    assert max_len > 0, "max_len must be positive"
    stripped = (description or "").strip()
    if not stripped:
        return ""
    text = stripped.splitlines()[0].strip()
    if not text or len(text) <= max_len:
        return text
    limit = max_len - 1  # reserve one character for the trailing ellipsis
    prefix = text[:limit]
    truncated = prefix.rsplit(" ", 1)[0].rstrip(",.;:—-") or prefix.rstrip()
    return f"{truncated}…"


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
        """Number of lines in this segment's content.

        A trailing ``"\\n"`` ends the last line rather than starting a phantom
        empty one (``splitlines()`` semantics), matching how ``total_lines`` is
        computed for the same content in ``chunking.split_block_into_segments``
        — the two always agree, so a whole-file segment is never misclassified
        as partial by a trailing-newline off-by-one.
        """
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

    @model_validator(mode="after")
    def _validate_segment(self) -> "FileSegment":
        """Enforce the geometric invariants documented on this class.

        Postconditions:
            - ``start_line`` is ≥ 1.
            - ``total_lines`` is at least ``line_count``.
            - ``end_line`` does not extend past ``total_lines``.
        """
        if self.start_line < 1:
            raise ValueError("start_line must be 1-based")
        if self.total_lines < self.line_count:
            raise ValueError("total_lines must be at least line_count")
        if self.end_line > self.total_lines:
            raise ValueError("segment extends past end of file")
        return self


class ReviewChunk(BaseModel):
    """One map-call unit: a group of file segments reviewed in a single LLM call.

    Invariants:
        - No two segments share the same ``path`` (so an issue's cited path
          resolves to exactly one segment for line validation).
    """

    segments: List[FileSegment] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_unique_paths(self) -> "ReviewChunk":
        """Reject chunks where two segments share a path (including empty).

        Postconditions:
            - Every ``path`` in ``segments`` appears at most once.
        """
        paths = [seg.path for seg in self.segments]
        if len(paths) != len(set(paths)):
            raise ValueError("ReviewChunk segments must have unique paths")
        return self

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
    spec_compliance_single_pass: bool = Field(
        default=False,
        description="When True, a dedicated single-pass spec/acceptance-criteria compliance check "
        "(synthesize_spec_compliance) runs once post-dedupe instead of including "
        "acceptance_criteria/spec_excerpt in this chunk's prompt. Computed once per run from "
        "CODE_REVIEW_SPEC_COMPLIANCE_PASS, never re-read per chunk.",
    )
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
    profile: ReviewProfile = Field(
        default=ReviewProfile.CODE_REVIEW,
        description="Role/criteria profile selecting which reviewer persona and checklist the "
        "chunk is judged against. Defaults to the standard code review.",
    )
    sibling_surface: str = Field(
        default="",
        description="Top-level symbols (functions/classes/exports) defined by the OTHER changed "
        "files in this submission that are not in this chunk, one 'path: names' line per file "
        "(capped). Lets the reviewer flag this chunk's references to a sibling symbol that was "
        "renamed or removed, which a bounded single-chunk view could otherwise miss.",
    )


class ChunkReviewOutput(BaseModel):
    """Output from reviewing one chunk (approved, issues, summary for this chunk)."""

    approved: bool = Field(default=False, description="True if chunk has no critical/high issues")
    issues: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Issues found (each with severity, category, file_path, line, start_line, "
        "title, description, suggestion, pre_existing). Items are already validated against "
        "ChunkReviewIssueLLM's typed schema at the LLM response boundary in "
        "chunk_reviewer._run_chunk_review before being flattened to plain dicts here; "
        "normalization into CodeReviewIssue happens once, downstream, in "
        "chunking._issues_from_chunk_output.",
    )
    summary: str = Field(default="", description="Review summary for this chunk")
    spec_compliance_notes: str = Field(
        default="",
        description="Notes on how well the chunk meets the spec and acceptance criteria",
    )


class CodeReviewIssue(BaseModel):
    """A single issue found during code review."""

    severity: str = Field(
        default="high",
        description="Severity: critical, high, medium, low, or info",
    )
    category: str = Field(
        default="general",
        description=(
            "Category: naming, structure, logic, spec-compliance, standards, integration, "
            "testing, architecture, refactor, maintainability, side-effects, documentation, or "
            "general (no specific category)"
        ),
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
    title: str = Field(
        default="",
        description="A short, descriptive title summarizing the issue (e.g. 'Missing pagination "
        "in UserListComponent'), distinct from `description`, which explains the problem in full. "
        "Every finding posted as a PR comment displays a title; a blank value here is filled in at "
        "render time via `derive_issue_title(description)`.",
    )
    description: str = Field(
        default="",
        description="Clear description of the issue",
    )
    suggestion: str = Field(
        default="",
        description="Concrete suggestion for how to fix the issue",
    )
    pre_existing: bool = Field(
        default=False,
        description="True when this issue is a bug in code the change under review did NOT add or "
        "modify — a pre-existing defect in unrelated, unchanged code — rather than a defect the "
        "change introduced. Documented in every profile's output schema, but only the PR-review "
        "flow (api/pr_review.py) actually acts on it: a finding tagged true is routed to a "
        "GitHub-issue proposal for a human to review instead of being posted as a PR comment, "
        "unless the finding's own file/line proves it sits on a line this PR actually added (that "
        "override, plus a finding naming a file outside this PR's diff entirely, are handled "
        "deterministically — see api/pr_review.py::_partition_review_issues for why a missing "
        "file/module is deliberately NOT treated as pre-existing). Default False.",
    )


# Public: the canonical severity vocabulary for this engine. Other agents that
# delegate review work here (e.g. devops_team.change_review_agent) reference
# this type directly rather than hand-copying its values, so a change here is
# never silently missed downstream.
CodeReviewIssueSeverity = Literal["critical", "high", "medium", "low", "info"]


def _normalized_severity(severity: Optional[str]) -> str:
    """Fold a severity token for rank / blocking comparisons.

    Preconditions:
        - ``severity`` is ``None`` or a string (may be empty / padded / mixed-case).

    Postconditions:
        - Returns ``(severity or "").strip().lower()``.
        - Never raises; empty / ``None`` → ``""``.
        - Pure; no side effects.
    """
    return (severity or "").strip().lower()


_ChunkReviewIssueCategory = Literal[
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
]


class ChunkReviewIssueLLM(BaseModel):
    """Narrow LLM-authored shape for one issue in a chunk-review response.

    Schema for ``chunk_reviewer._run_chunk_review`` to validate replies via
    ``llm_service.complete_validated`` (see ``llm_service``'s README, "When to use which
    entrypoint"). This is the raw per-issue shape the model is asked to
    emit — distinct from the persisted :class:`CodeReviewIssue`, which
    additionally range-validates ``line``/``start_line`` against the cited
    file segment and resolves ``file_path`` against the chunk. That
    normalization stays downstream, in ``chunking._issues_from_chunk_output``,
    unaffected by this schema.

    ``severity``/``category`` are typed as the exact enumerated sets the
    review prompt asks for (mirrors ``chunking._VALID_SEVERITIES``/
    ``_VALID_CATEGORIES``) instead of a free string with silent fallback
    coercion: an out-of-set value now fails schema validation and drives
    ``complete_validated``'s one correction retry, rather than being
    silently rewritten to "high"/"general" as today's hand-rolled parsing
    does in ``chunking._issues_from_chunk_output``.

    ``pre_existing`` is ``StrictBool``, not plain ``bool``: Pydantic's
    default lax coercion would accept a numeric ``1``/``0`` (or "yes"/"no",
    etc.) and silently turn it into a real ``True``/``False`` at validation
    time. That would erase the distinction ``chunking._coerce_bool``
    deliberately preserves downstream — only a real bool or a recognized
    truthy string counts there, and a bare number is always false, to stop
    a stray numeric value from being misread as an affirmative flag.
    ``StrictBool`` keeps that policy intact by rejecting non-bool input
    outright (driving ``complete_validated``'s corrective retry) instead of
    silently coercing it before it ever reaches that downstream check.
    """

    severity: CodeReviewIssueSeverity = Field(
        default="high",
        description="Severity: critical, high, medium, low, or info",
    )
    category: _ChunkReviewIssueCategory = Field(
        default="general",
        description=(
            "Category: naming, structure, logic, spec-compliance, standards, integration, "
            "testing, architecture, refactor, maintainability, side-effects, documentation, or "
            "general (no specific category)"
        ),
    )
    file_path: str = Field(
        default="",
        description="File path where the issue was found. Blank when the chunk has a single "
        "segment or the issue is not tied to one specific file.",
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
    title: str = Field(
        default="",
        description="A short, descriptive title summarizing the issue, distinct from "
        "`description` (which explains the problem in full). Blank when the model omits it; the "
        "downstream normalizer fills in a fallback derived from `description`.",
    )
    description: str = Field(
        default="",
        description="Clear description of the issue",
    )
    suggestion: str = Field(
        default="",
        description="Concrete suggestion for how to fix the issue",
    )
    pre_existing: StrictBool = Field(
        default=False,
        description="True when this issue is a bug in code the change under review did NOT add or "
        "modify — a pre-existing defect in unrelated, unchanged code — rather than a defect the "
        "change introduced. Default False.",
    )


class ChunkReviewLLMResponse(BaseModel):
    """Narrow LLM-authored shape for one chunk-review call's response.

    ``chunk_reviewer._run_chunk_review`` validates every chunk-review reply
    against this model via ``llm_service.complete_validated``, replacing the
    hand-rolled ``.get()``/``str()``/``bool()`` coercions the reviewer used to
    apply to a raw ``complete_json_with_continuation`` reply.

    All four fields are required, not defaulted: the chunk-review prompt's
    own output-contract reminder (``FINAL_OUTPUT_CONTRACT_NOTE`` in
    chunk_reviewer.py) explicitly tells the model to always emit exactly
    these four keys, so a reply missing one is a truncated/malformed
    response, not a legitimately empty field. Defaulting them here would
    reproduce the hand-parser's permissive ``.get(..., default)`` fallbacks
    in the one place meant to demonstrate the opposite — a missing field
    must fail validation and drive ``complete_validated``'s corrective
    retry, not silently look like a clean, empty-issue approval.

    ``approved`` must agree with whether the issues list carries an
    actionable critical/high finding, in both directions -- exactly the
    consistency check ``coordinator._reconcile_approval`` applies downstream
    (its own ``critical_or_high`` computation): a rejection with no such
    issue and an approval that carries one are both malformed per the
    review prompt's contract (``profiles.py``: "APPROVE... No critical or
    high issues... REJECT... Any critical or high issue present"). Today
    the coordinator's gate silently repairs either case (flipping a
    baseless rejection to an approval, or a contradictory approval to a
    rejection) rather than letting a malformed LLM reply be a schema
    failure. Enforcing the same rule here means that reply instead fails
    validation and drives ``complete_validated``'s corrective retry — giving
    the model a chance to correct itself — rather than always being
    silently absorbed by the coordinator's safety net.
    """

    approved: bool = Field(
        description="True if chunk has no critical/high issues",
    )
    issues: List[ChunkReviewIssueLLM] = Field(
        description="Issues found in this chunk",
    )
    summary: str = Field(
        description="Review summary for this chunk",
    )
    spec_compliance_notes: str = Field(
        description="Notes on how well the chunk meets the spec and acceptance criteria",
    )

    @model_validator(mode="after")
    def _require_approval_consistent_with_issues(self) -> "ChunkReviewLLMResponse":
        """Reject a verdict that contradicts its own issues list.

        Computes the same "actionable critical/high issue" predicate
        ``coordinator._reconcile_approval`` derives from its ``issues``
        parameter (severity in critical/high), narrowed by the two
        conditions ``chunking._issues_from_chunk_output`` uses to drop an
        issue before it ever reaches that gate: a blank description, or a
        suggestion that is, in its entirety, a no-op admission
        (``is_no_op_suggestion``, e.g. "No changes needed."). An issue
        matching either is not "populated" no matter its severity, so it
        never counts on either side of this check -- matching what
        ``_reconcile_approval`` actually sees once that filtering has run.

        ``approved`` must then agree: ``True`` requires no actionable
        critical/high issue, ``False`` requires at least one.
        """
        has_actionable_critical_or_high = any(
            issue.severity in ("critical", "high")
            and issue.description.strip()
            and not is_no_op_suggestion(issue.suggestion)
            for issue in self.issues
        )
        if self.approved and has_actionable_critical_or_high:
            raise ValueError(
                "approved=True is invalid when the issues list contains an actionable "
                "critical/high issue (non-blank description, non-no-op suggestion)"
            )
        if not self.approved and not has_actionable_critical_or_high:
            raise ValueError(
                "approved=False requires at least one issue with severity 'critical' or "
                "'high', a non-blank description, and a non-no-op suggestion"
            )
        return self


_ArchitectureFindingCategory = Literal["architecture", "refactor"]
_SideEffectFindingCategory = Literal["side-effects", "documentation"]


class ArchitectureConsistencyFindingLLM(BaseModel):
    """Narrow LLM-authored shape for one Part 1 finding of the merged
    architecture-consistency + side-effect-impact prompt
    (``prompts.MERGED_ARCHITECTURE_SIDE_EFFECT_PROMPT``).

    Mirrors the fields ``architecture_consistency_pass._coerce_finding``
    actually populates on ``CodeReviewIssue`` — severity, category,
    file_path, line, description, suggestion — typed as an enumerated
    schema in the same style as :class:`ChunkReviewIssueLLM`. Intentionally
    omits ``start_line`` and ``pre_existing``: that pass's own
    ``_coerce_finding`` never populates either (its prompt's output format
    has no multi-line-anchor field, and only the side-effect pass emits
    ``pre_existing``). This is the intended shape for the merged prompt's
    Part 1 output contract. The in-process merged pass currently reuses the
    standalone architecture pass's parsing/validation helpers to coerce and
    validate per-half findings, rather than model-validating this class
    directly.
    """

    severity: CodeReviewIssueSeverity = Field(
        default="medium",
        description="Severity: critical, high, medium, low, or info",
    )
    category: _ArchitectureFindingCategory = Field(
        description="architecture (a stated architecture boundary/pattern/decision the change "
        "contradicts) or refactor (a capability the change re-implements that already exists "
        "elsewhere in the repository)",
    )
    file_path: str = Field(
        default="",
        description="The changed file the finding is about",
    )
    line: Optional[int] = Field(
        default=None,
        description="1-based line number the finding is tied to, or None for a file-wide finding",
    )
    description: str = Field(
        default="",
        description="The specific contradiction or duplication, citing the architecture statement "
        "or existing code verified",
    )
    suggestion: str = Field(
        default="",
        description="A concrete fix (e.g. which existing helper/module to reuse instead, or how to "
        "align with the stated boundary)",
    )


class SideEffectImpactFindingLLM(BaseModel):
    """Narrow LLM-authored shape for one Part 2 finding of the merged
    architecture-consistency + side-effect-impact prompt
    (``prompts.MERGED_ARCHITECTURE_SIDE_EFFECT_PROMPT``).

    Mirrors the fields ``side_effect_impact_pass._coerce_finding`` actually
    populates on ``CodeReviewIssue`` — severity, category, file_path, line,
    description, suggestion, and ``pre_existing``. ``pre_existing`` is
    ``StrictBool`` here (matching :class:`ChunkReviewIssueLLM`'s rationale),
    which is stricter than the current pass's ``chunking._coerce_bool``:
    a bare numeric ``1``/``0`` or a string token is rejected at validation
    time and drives ``complete_validated``'s corrective retry, rather than
    being silently coerced to ``False`` (or a truthy string to ``True``) as
    ``_coerce_bool`` does today. Intentionally omits ``start_line``: that
    pass's own ``_coerce_finding`` never populates it either (its prompt's
    output format has no multi-line-anchor field). This is the intended shape for the
    merged prompt's Part 2 output contract. The in-process merged pass currently
    reuses the standalone side-effect pass's parsing/validation helpers to coerce
    and validate per-half findings, rather than model-validating this class directly.
    """

    severity: CodeReviewIssueSeverity = Field(
        default="medium",
        description="Severity: critical, high, medium, low, or info",
    )
    category: _SideEffectFindingCategory = Field(
        description="side-effects (a real caller-breaking side effect) or documentation (a "
        "docstring/comment that no longer matches the implementation)",
    )
    file_path: str = Field(
        default="",
        description="The changed file whose behavior the finding is about",
    )
    line: Optional[int] = Field(
        default=None,
        description="1-based line number the finding is tied to, or None for a file-wide finding",
    )
    description: str = Field(
        default="",
        description="For side-effects: the function's current behavior and the specific caller "
        "file/line and assumption that breaks. For documentation: the exact discrepancy between "
        "the docstring/comment and what the code actually does",
    )
    suggestion: str = Field(
        default="",
        description="A concrete fix (e.g. update the caller, or correct the docstring to match the "
        "implementation)",
    )
    pre_existing: StrictBool = Field(
        default=False,
        description="True when this issue is a bug in code the change under review did NOT add "
        "or modify — a pre-existing defect in unrelated, unchanged code — rather than a defect "
        "the change introduced (mirrors CodeReviewIssue.pre_existing's canonical wording). Per "
        "the merged prompt's Part 2 tagging guidance, that means the function(s) the finding is "
        "about look untouched by this submission's actual work.",
    )


class MergedArchitectureSideEffectResponse(BaseModel):
    """Merged LLM-authored output schema for
    ``prompts.MERGED_ARCHITECTURE_SIDE_EFFECT_PROMPT`` — the target reply
    shape for consolidating the architecture-consistency pass
    (``architecture_consistency_pass.py``) and the side-effect-impact pass
    (``side_effect_impact_pass.py``) into a single LLM call.

    The two passes' findings are kept in separate, explicitly-named arrays
    rather than one merged list disambiguated only by ``category`` — so
    downstream code can attribute each finding to its originating pass (and
    convert either array straight into a list of ``CodeReviewIssue``, the
    same way each pass's own ``_coerce_finding`` does today) without having
    to re-derive which pass a given category value came from.

    This is the target reply schema for the merged prompt contract. The
    current in-process merged pass implementation parses the raw JSON and then
    validates/coerces each half independently via the standalone passes'
    parsing/validation helpers; it does not model-validate this response object
    as a whole.

    Both fields are required, not defaulted — mirroring
    :class:`ChunkReviewLLMResponse`'s identical rationale. The merged
    prompt's own output-contract instructs the model to always return both
    keys, so a reply missing one is a truncated/malformed response, not a
    legitimately empty part: defaulting it here would silently reinterpret
    "the model never ran (or truncated) this half of the check" as "the
    model ran this half and found nothing," hiding every finding that half
    would have reported. Requiring both keys instead fails validation on
    omission and drives ``complete_validated``'s corrective retry, giving
    the model a chance to actually complete the omitted half — while an
    explicit empty list for either key remains a valid, expected "found
    nothing" outcome.
    """

    architecture_findings: List[ArchitectureConsistencyFindingLLM] = Field(
        description="Findings from the architecture-consistency / cross-codebase-redundancy check "
        "(Part 1 of the merged prompt). Empty list when that part found nothing; the key itself "
        "is still required.",
    )
    side_effect_findings: List[SideEffectImpactFindingLLM] = Field(
        description="Findings from the side-effect / blast-radius impact check (Part 2 of the "
        "merged prompt). Empty list when that part found nothing; the key itself is still "
        "required.",
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
    architecture: Optional[SystemArchitecture] = Field(
        default=None,
        description="System architecture context (overview, components, decisions), when available",
    )
    existing_codebase: Optional[str] = Field(
        default=None,
        description="Existing code in the repo before the agent's changes",
    )
    user_decisions: Optional[List[str]] = Field(
        default=None,
        description="Product/design questions the user has already answered ('question → answer' "
        "lines); the reviewer treats them as settled facts, not as open issues to flag.",
    )
    profile: ReviewProfile = Field(
        default=ReviewProfile.CODE_REVIEW,
        description="Role/criteria profile selecting which reviewer persona and checklist the "
        "engine applies (the gate calling the engine sets this). Defaults to the standard code "
        "review, reproducing today's behavior for every existing caller.",
    )
    skip_false_positive_filter: bool = Field(
        default=False,
        description="When True, the coordinator skips the whole-codebase false-positive "
        "re-check and stands behind the per-chunk findings as-is. Default False keeps the "
        "filter on for every existing caller; an escape hatch for gates whose findings must "
        "not be silently dropped.",
    )
    skip_tail_passes: bool = Field(
        default=False,
        description="When True, the coordinator skips BOTH tail passes entirely (the "
        "false-positive filter and the merged architecture/side-effect pass) and returns "
        "the per-chunk findings as-is, with no additional LLM calls from those two passes "
        "after the map phase. Default False keeps both passes on for every existing caller. "
        "Intended for a lightweight fallback caller that wants speed over full tail-pass "
        "rigor; implies skip_false_positive_filter's effect (setting both is redundant, not "
        "conflicting). Does NOT affect the separate, independently-gated post-dedupe "
        "spec-compliance synthesis pass: when CODE_REVIEW_SPEC_COMPLIANCE_PASS is enabled "
        "for the CODE_REVIEW profile, that single synthesize_spec_compliance call still runs "
        "even if skip_tail_passes is set. Only honored by the in-process coordinator today — "
        "the Temporal workflow path does not yet thread it through.",
    )
    repo_root: Optional[str] = Field(
        default=None,
        description="Absolute path to a materialized disk checkout of the whole repository, used "
        "to reconstruct a fail-safe ``DiskRepoReader`` for the false-positive and "
        "architecture/redundancy passes. Unlike a live ``RepoReader`` object, this string "
        "survives ``model_dump(mode='json')``, so it is the channel that gives those passes "
        "off-diff read access when the review runs as a durable Temporal workflow. ``None`` (or a "
        "path that no longer exists) means no off-diff read access — the passes then keep more "
        "findings (fail-safe), never fewer. GitHub-backed reviews leave this unset (their reader "
        "cannot be rebuilt from a path); they honor the live reader via the in-process path.",
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
    not_reviewed_ranges: List[str] = Field(
        default_factory=list,
        description="Human-readable file/line ranges the reviewer could not process automatically "
        "(e.g. after the model returned no usable output). Non-blocking observability metadata: "
        "these are never rendered as a PR comment and never affect `approved`. When the "
        "CODE_REVIEW_BLOCK_ON_UNREVIEWED opt-out is set, the same ranges also appear as blocking "
        "high findings in `issues`.",
    )
    summary: str = Field(
        default="",
        description="Brief, high-level review overview: which areas of the code have issues and any "
        "common theme. Does not restate what the PR does, and never praises the code or claims spec "
        "alignment when issues were found.",
    )
    spec_compliance_notes: str = Field(
        default="",
        description="Concrete spec/acceptance-criteria gaps only, or '' when there are none",
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
