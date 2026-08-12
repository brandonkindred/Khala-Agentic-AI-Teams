"""Architecture-consistency and cross-codebase-redundancy pass for code review.

The map-reduce reviewer (``coordinator.py``) and the false-positive filter both
operate on *this submission's* files: the chunk reviewer sees a bounded slice
of the diff, and the false-positive filter re-checks a finding against the
whole submission (plus, via an optional ``RepoReader``, the rest of the
repository) but only ever *drops* findings — it never adds one. Neither can
answer "does this change contradict the established system architecture?" or
"does this change duplicate a capability that already exists elsewhere in the
repository?" — both questions require reading the architecture document and
searching the wider codebase, which is exactly what this module adds.

This pass runs ONCE PER SUBMISSION (never once per chunk), after the
false-positive filter, and is purely additive: it is given the full
architecture document and the merged submission, with read access to the rest
of the repository via the same tools the false-positive filter uses
(``read_file``, ``read_lines``, ``read_function``, ``list_files``, ``search_codebase``,
``find_function_at_line``, ``find_references``),
and emits new findings in two categories only: ``"architecture"`` (a stated
boundary/pattern/decision the change contradicts) and ``"refactor"`` (a
capability the change re-implements that already exists elsewhere). Every
finding must be tool-verified by the pass's own instructions — the prompt
requires it to confirm a duplicate actually exists, or quote the specific
architecture statement a change contradicts, rather than guess.

Invariants:

    - **Additive-only, fail-safe.** This pass can only ever ADD findings; it
      never removes, mutates, or re-judges anything the map phase or the
      false-positive filter already produced. Any setup or LLM failure is
      swallowed and logged, returning no additional findings — the same
      fail-safe posture the false-positive filter uses for removal, applied
      here to addition: a broken new pass must never affect the review's
      existing accuracy or block the run.

    - **Bounded cost per call, with reactive recovery.** Agent construction,
      context budgeting, proactive file-group chunking, and reactive overflow
      bisect/shrink recovery are all owned by the shared
      :func:`~code_review_agent.submission_pass_runner.run_submission_pass`
      runner; this module supplies only its system prompt, tool set, and
      prompt/parse callbacks. A submission that fits under the budget still
      makes exactly one call, matching the false-positive filter's
      per-submission cost shape.

    - **``CODE_REVIEW`` profile only.** The other :class:`.profiles.ReviewProfile`
      values narrow the engine to a specific checklist whose contract expects
      every issue to map to a specific criterion/requirement (e.g. the
      acceptance profile's per-criterion attribution); an architecture/refactor
      finding never maps to one of those, so this pass never runs under any
      profile but the default.
"""

from __future__ import annotations

import json
import logging
from typing import List, Optional, Tuple

from llm_service import LLMClient
from shared.dev_models.models import SystemArchitecture
from shared.env import env_flag_enabled

from .architecture_context import architecture_evidence_available, render_architecture_context
from .chunking import _coerce_bool
from .false_positive_filter import CodebaseIndex, _build_tools, _code_fence_for
from .models import CodeReviewInput, CodeReviewIssue, coerce_line, is_no_op_suggestion
from .profiles import ReviewProfile
from .prompts import (
    ARCHITECTURE_CONSISTENCY_FORMATTING_INSTRUCTIONS,
    ARCHITECTURE_CONSISTENCY_REASONING_SYSTEM_PROMPT,
)
from .repo_reader import RepoReader
from .side_effect_impact_pass import _effective_pre_numbered
from .submission_pass_runner import FileBatch, SubmissionPassBudgets, run_submission_pass

logger = logging.getLogger(__name__)

# Default-on toggle: an explicit ``CODE_REVIEW_ARCHITECTURE_CONSISTENCY_PASS=false``/``0``/``no``
# disables the pass (see docs/ENV_VARS.md). Any other value (or unset) leaves it enabled.
_PASS_ENV = "CODE_REVIEW_ARCHITECTURE_CONSISTENCY_PASS"

_ALLOWED_CATEGORIES = frozenset({"architecture", "refactor"})
_ALLOWED_SEVERITIES = frozenset({"critical", "high", "medium", "low", "info"})


def _flatten_architecture_document(architecture: Optional[SystemArchitecture]) -> str:
    """Flatten ``architecture`` into the document text :func:`_build_prompt` inlines.

    Postconditions:
        - Returns ``""`` when ``architecture`` is None or carries neither a
          document nor renderable overview/components/decisions context.
        - Otherwise returns the document (or the rendered context in its
          place/alongside it) with a blank line between the two when both are
          present. Pure; never raises.
    """
    if architecture is None:
        return ""
    return "\n\n".join(
        p
        for p in (
            (architecture.architecture_document or "").strip(),
            render_architecture_context(architecture),
        )
        if p
    )


def _overflow_manifest_note(omitted: int) -> str:
    """Tool-reachable note for changed paths omitted from the inline manifest."""
    return (
        f"... and {omitted} more changed path(s) not listed; use read_file(path) or "
        "list_files() to reach them."
    )


def _render_manifest(paths: List[str], max_manifest_chars: int) -> List[str]:
    """Render the changed-file path list, truncated to ``max_manifest_chars``.

    A small pass-owned duplicate of
    ``merged_architecture_side_effect_pass._render_manifest`` -- that module
    imports this one, so importing the reverse direction would be circular.

    Postconditions:
        - Always includes the section header.
        - When the full remaining list fits in the budget, renders every
          remaining path (no overflow note) -- never reserves note room that
          would hide paths that already fit.
        - When the full list exceeds the budget, includes as many paths as fit
          and a tool-reachable overflow note for the rest.
    """
    header = f"**Changed files in this submission ({len(paths)}):**"
    lines: List[str] = [header]
    used = len(header) + 1
    shown = 0
    for i, path in enumerate(paths):
        rest_cost = sum(len(p) + 1 for p in paths[i:])
        if used + rest_cost <= max_manifest_chars:
            lines.extend(paths[i:])
            return lines

        line_cost = len(path) + 1
        omitted_after = len(paths) - (shown + 1)
        room_for_note = len(_overflow_manifest_note(omitted_after)) + 1 if omitted_after > 0 else 0
        if used + line_cost + room_for_note > max_manifest_chars and shown > 0:
            lines.append(_overflow_manifest_note(len(paths) - shown))
            break
        if used + line_cost > max_manifest_chars and shown == 0:
            lines.append(_overflow_manifest_note(len(paths)))
            break
        lines.append(path)
        used += line_cost
        shown += 1
    return lines


def _build_prompt(
    index: CodebaseIndex,
    architecture: Optional[SystemArchitecture],
    max_inline_chars: int,
    *,
    max_architecture_chars: int,
    max_manifest_chars: int,
    content_items: Optional[List[Tuple[str, str]]] = None,
    batch_index: Optional[int] = None,
    total_batches: Optional[int] = None,
    is_partial: bool = False,
) -> str:
    """Render the user prompt for one submission-pass runner call.

    Preconditions:
        - Budget ints are ``>= 0`` (from :class:`.submission_pass_runner.SubmissionPassBudgets`).
        - ``content_items``, when given, is this call's batch of the changed
          files (a subset of ``index.files.items()``); ``None`` inlines every
          changed file (the pre-batching / single-batch behavior).
        - ``batch_index``/``total_batches`` are both ``None`` (no batch label
          rendered) or both set to this batch's 1-based position and the
          total batch count.
        - ``is_partial`` is True only for a reactive-recovery bisect/shrink
          child batch (:attr:`~code_review_agent.submission_pass_runner.FileBatch.is_partial`).

    Postconditions:
        - When architecture context is present, inlines it truncated to
          ``max_architecture_chars`` (no tool exposes the document itself, so
          an omitted remainder is simply noted, not recoverable).
        - When no formal architecture document/context is present, states
          that explicitly so the model relies on repository structure/patterns.
        - The changed-file path manifest always lists every changed file in
          the submission (from ``index.files``, not ``content_items``),
          truncated to ``max_manifest_chars`` with a tool-reachable overflow
          note when needed.
        - Inlines ``content_items`` (or every changed file when ``None``) up
          to ``max_inline_chars``; any file content beyond that budget is
          named as reachable via the attached tools rather than silently
          dropped. When ``is_partial`` is True, the content section header
          renders a reduced-view recovery banner instead of a "batch N of M"
          claim; otherwise, when ``total_batches`` is set (> 1), it names this
          batch's position.
    """
    parts: List[str] = []

    arch_doc = _flatten_architecture_document(architecture)
    if arch_doc:
        body = arch_doc[:max_architecture_chars]
        doc_fence = _code_fence_for(body)
        parts.append("**Architecture document:**")
        parts.append(doc_fence)
        parts.append(body)
        parts.append(doc_fence)
        if len(body) < len(arch_doc):
            parts.append(
                f"(Only the first {len(body)} characters of the architecture document "
                "are shown above -- the remainder was omitted to fit the model context.)"
            )
    else:
        parts.append("**Architecture document:**")
        parts.append(
            "No formal architecture document was provided for this review. "
            "Derive architecture expectations from the repository's established "
            "structure and patterns via list_files()/read_file(); do not invent "
            "a phantom document."
        )
    parts.append("")

    changed_files = list(index.files.items())
    paths = [path for path, _ in changed_files]
    parts.extend(_render_manifest(paths, max_manifest_chars))
    parts.append("")

    batch_files = content_items if content_items is not None else changed_files
    if is_partial:
        parts.append(
            f"**Content of the changed files shown in this call ({len(batch_files)} of "
            f"{len(changed_files)} changed files in this submission -- a reduced view "
            "produced while recovering from a context-size overflow; any file not shown "
            "here is still listed in the manifest above and reachable via "
            "read_file()/list_files()):**"
        )
    elif total_batches and total_batches > 1:
        parts.append(
            f"**Full content of the changed files (batch {batch_index} of {total_batches} -- "
            f"showing {len(batch_files)} of {len(changed_files)} changed files in this "
            "submission; the rest are listed in the manifest above and reachable via "
            "read_file()/list_files()):**"
        )
    else:
        parts.append("**Full content of the changed files:**")
    remaining = max_inline_chars
    omitted = 0
    for i, (path, content) in enumerate(batch_files):
        if remaining <= 0:
            omitted = len(batch_files) - i
            break
        body = content[:remaining]
        body_fence = _code_fence_for(body)
        parts.append(f"### {path} ###")
        parts.append(body_fence)
        parts.append(body)
        parts.append(body_fence)
        if len(body) < len(content):
            parts.append(
                f"(Only the first {len(body)} characters of `{path}` are shown above; call "
                "read_file to see the rest.)"
            )
        remaining -= len(body)
    if omitted:
        parts.append(
            f"... and {omitted} more changed file(s) not shown above; use read_file(path) or "
            "list_files() to see them."
        )
    parts.append("")

    parts.append(
        "Use list_files()/read_file() to inspect the REST of the repository (files not shown "
        "above) before flagging a cross-codebase duplicate -- search_codebase only searches "
        "this submission's files, not the wider repository. When an architecture document is "
        "present above, use read_file() / the document to confirm contradictions; when none was "
        "provided, confirm contradictions against established repository structure/patterns."
    )
    parts.append(
        "Summarize architecture-consistency findings in structured prose per the system "
        "instructions (severity, category, file_path, line, description, suggestion, "
        "pre_existing). State clearly when you find nothing in either category."
    )
    return "\n".join(parts)


def _coerce_finding(item: object) -> Optional[CodeReviewIssue]:
    """Parse one raw finding dict into a :class:`CodeReviewIssue`, or None.

    Postconditions:
        - Returns None for a non-dict item, an unrecognized/missing
          ``category`` (only ``"architecture"``/``"refactor"`` are accepted —
          this pass's whole purpose is those two axes), a blank
          ``description`` (an unactionable finding is worse than none), or a
          ``suggestion`` that is, in its entirety, a no-op phrasing (e.g. "No
          changes needed.") -- see ``is_no_op_suggestion``. An unrecognized
          ``severity`` defaults to ``"medium"`` rather than being dropped,
          matching this pass's default-severity guidance. Never raises on
          malformed input.
        - ``pre_existing`` reflects the model's optional per-finding tag
          (coerced via ``chunking._coerce_bool``, tolerating string
          encodings), defaulting to ``False`` when absent -- mirrors
          ``side_effect_impact_pass._coerce_finding``'s identical convention,
          used by the PR-review whole-file path to route a finding about a
          field/function/class this submission did NOT add or modify to a
          human-review proposal instead of a blocking PR comment (see
          ``CodeReviewIssue.pre_existing``).
    """
    if not isinstance(item, dict):
        return None
    category = str(item.get("category", "") or "").strip().lower()
    if category not in _ALLOWED_CATEGORIES:
        return None
    description = str(item.get("description", "") or "").strip()
    if not description:
        return None
    suggestion = str(item.get("suggestion", "") or "").strip()
    if is_no_op_suggestion(suggestion):
        return None
    severity = str(item.get("severity", "") or "").strip().lower()
    if severity not in _ALLOWED_SEVERITIES:
        severity = "medium"
    return CodeReviewIssue(
        severity=severity,
        category=category,
        file_path=str(item.get("file_path", "") or "").strip(),
        line=coerce_line(item.get("line")),
        description=description,
        suggestion=suggestion,
        pre_existing=_coerce_bool(item.get("pre_existing")),
    )


def _parse_findings(data: object) -> List[CodeReviewIssue]:
    """Map the pass's raw JSON reply to a list of new findings.

    Postconditions:
        - Returns ``[]`` for a non-dict reply or one without a list
          ``findings`` (the pass found nothing to add, or replied off
          contract — both degrade to "nothing new"). Otherwise returns one
          ``CodeReviewIssue`` per item :func:`_coerce_finding` can parse, in
          order; unparseable items are skipped, not raised on.
    """
    if not isinstance(data, dict):
        return []
    raw = data.get("findings")
    if not isinstance(raw, list):
        return []
    return [parsed for item in raw if (parsed := _coerce_finding(item)) is not None]


def parse_findings(data: object) -> List[CodeReviewIssue]:
    """Public wrapper for :func:`_parse_findings`.

    Exposed so other passes (e.g. the merged architecture/side-effect pass)
    can reuse the same parsing contract without depending on private helper
    names.
    """
    return _parse_findings(data)


def _validate_finding_line(
    index: CodebaseIndex, file_path: str, line: Optional[int], pre_numbered: bool = False
) -> Optional[int]:
    """Validate a cited line number against the real file it names, or None.

    Mirrors ``chunking._validate_line``'s guarantee for chunk-review findings:
    a hallucinated citation can never anchor a finding to the wrong (or
    nonexistent) line. This pass has no ``FileSegment`` to bound against (it
    works over whole files), so it bounds against the resolved file's actual
    line count instead -- except under ``pre_numbered`` inputs (e.g. PR hunk
    review mode), where the submission's file content is only the shown hunk
    lines, each already prefixed with its ORIGINAL absolute line number (as
    text, e.g. ``"4242: ..."``); the physical line count of that hunk bears no
    relation to the cited absolute number, so (mirroring
    ``chunking._validate_line``'s identical ``seg.pre_numbered`` branch) the
    citation is trusted as-is rather than bounds-checked against it.

    Postconditions:
        - Under ``pre_numbered``, returns ``line`` unchanged (or None if
          ``line`` is None) without reading the file at all.
        - Returns None when ``line`` is None, ``file_path`` does not resolve to
          a readable file, or the file's content cannot be read (per
          ``index.read_file_or_none``).
        - Returns ``line`` unchanged when it falls within ``[1, total_lines]``
          of the resolved file's content; otherwise returns None. Never raises.
    """
    if line is None:
        return None
    if pre_numbered:
        return line
    resolved = index.resolve_path(file_path)
    if resolved is None:
        return None
    content = index.read_file_or_none(resolved)
    if content is None:
        return None
    total_lines = len(content.splitlines())
    if total_lines == 0:
        return None
    return line if 1 <= line <= total_lines else None


def _is_changed_file(index: CodebaseIndex, file_path: str) -> bool:
    """True when ``file_path`` is one of the submission's own changed files.

    ``index.files`` is exactly the submitted diff (see ``CodebaseIndex``'s
    invariants) -- it never includes ``repo_reader``-backed files (the rest of
    the repository) or the ``<existing codebase>`` pseudo-path, both of which
    ``resolve_path``/``read_file`` CAN reach for verification but which are not
    part of this change.

    Postconditions: returns ``False`` for an empty/blank path or one that
    resolves only via ``repo_reader``/the existing-codebase excerpt; ``True``
    iff ``file_path`` resolves (via ``CodebaseIndex.resolve_path``, which also
    accepts a unique basename/suffix alias such as ``main.py`` for
    ``app/main.py``) to a key of ``index.files``. Pure; never raises.
    """
    if not file_path:
        return False
    resolved = index.resolve_path(file_path)
    return resolved is not None and resolved in index.files


def _validate_findings(
    index: CodebaseIndex, findings: List[CodeReviewIssue], pre_numbered: bool = False
) -> List[CodeReviewIssue]:
    """Bounds-check each finding's file/line anchor against the real submission.

    Preconditions:
        - ``pre_numbered`` mirrors ``CodeReviewInput.pre_numbered``: True only
          when the submission's file content already carries original-file
          absolute line-number prefixes (PR hunk review mode).

    Postconditions:
        - A finding whose ``file_path`` is not one of the submission's changed
          files (e.g. the model anchored a cross-codebase-redundancy finding to
          the existing file it cited as the duplicate, rather than to the
          changed file that should be fixed) has its ``file_path``/``line``
          blanked to ``""``/``None`` — a PR comment cannot attach to a file
          outside the diff, so this degrades to a submission-wide finding
          rather than pointing at the wrong location.
        - Otherwise ``line`` is replaced by ``None`` wherever it does not fall
          within the cited file's actual line range (a file-wide finding is
          still a valid, useful outcome) -- except under ``pre_numbered``,
          where the citation is trusted as-is (see ``_validate_finding_line``).
        - Never drops a finding outright — only its potentially wrong/hallucinated
          location anchor — and never raises.
    """
    validated: List[CodeReviewIssue] = []
    for finding in findings:
        if finding.file_path:
            resolved_path = index.resolve_path(finding.file_path)
            if resolved_path is None or resolved_path not in index.files:
                finding = finding.model_copy(update={"file_path": "", "line": None})
                validated.append(finding)
                continue
            if resolved_path != finding.file_path:
                # Normalize a basename/suffix alias (e.g. "main.py") to the
                # submission's actual key ("app/main.py") so the anchor is
                # exact for PR-comment placement, not just verified.
                finding = finding.model_copy(update={"file_path": resolved_path})
        checked_line = _validate_finding_line(index, finding.file_path, finding.line, pre_numbered)
        if checked_line != finding.line:
            finding = finding.model_copy(update={"line": checked_line})
        validated.append(finding)
    return validated


def validate_findings(
    index: CodebaseIndex, findings: List[CodeReviewIssue], *, pre_numbered: bool = False
) -> List[CodeReviewIssue]:
    """Public wrapper for :func:`_validate_findings`.

    Exposed so other passes (e.g. the merged architecture/side-effect pass)
    can reuse the same validation contract without depending on private
    helper names.
    """
    return _validate_findings(index, findings, pre_numbered=pre_numbered)


def find_architecture_and_redundancy_issues(
    llm: LLMClient,
    input_data: CodeReviewInput,
    repo_reader: Optional[RepoReader] = None,
    index: Optional[CodebaseIndex] = None,
) -> List[CodeReviewIssue]:
    """Run the once-per-submission architecture/redundancy pass.

    Preconditions:
        - ``input_data`` is the same review input the coordinator built for
          this submission (its ``files``/``existing_codebase`` back the
          ``CodebaseIndex`` this pass reads from).
        - ``repo_reader`` is None or a read-only, thread-safe
          ``repo_reader.RepoReader`` giving access to the rest of the
          repository beyond the diff (the same object the false-positive
          filter is given).
        - ``index``, when given, must have been built from this same
          ``input_data``/``repo_reader`` (the coordinator shares one index
          across this pass and the false-positive filter rather than each
          rebuilding it); ``None`` builds a fresh one.

    Postconditions:
        - Returns ``[]`` (no LLM call) when the pass is disabled via
          ``CODE_REVIEW_ARCHITECTURE_CONSISTENCY_PASS``, when
          ``input_data.profile`` is anything other than
          ``ReviewProfile.CODE_REVIEW`` (the other profiles -- ``ACCEPTANCE``,
          ``SPEC_CONFORMANCE``, ... -- have a narrower contract that expects
          every issue to be attributable to a specific criterion/requirement,
          which an architecture/refactor finding never is; e.g.
          ``AcceptanceVerifierAgent`` treats any unattributed issue as an
          unmet criterion, so letting this pass run under that profile could
          spuriously fail acceptance verification even when every criterion
          is satisfied), when there is no architecture payload and no
          ``repo_reader`` / ``existing_codebase`` evidence, or when the
          submission has no readable files.
        - An architecture document is optional when repository evidence exists:
          when the document is absent or empty but a ``repo_reader`` or
          ``existing_codebase`` excerpt is available, the pass still runs and
          the user prompt states that no formal document was provided so the
          model can use established repository structure.
        - Otherwise returns zero or more NEW ``CodeReviewIssue``s in category
          ``"architecture"`` or ``"refactor"`` only, each with its cited
          ``line`` bounds-checked against the real file (a hallucinated
          out-of-range line is nulled to a file-wide finding, never trusted
          verbatim); never mutates or removes any issue the caller already
          has.
        - Never raises: any setup or LLM failure is logged at warning level
          and yields ``[]`` — this pass can only ever add findings, so a
          failure here must never affect the review already computed by the
          map phase and the false-positive filter.
    """
    if not env_flag_enabled(_PASS_ENV):
        return []
    if input_data.profile != ReviewProfile.CODE_REVIEW:
        return []
    if not architecture_evidence_available(input_data, repo_reader, index):
        return []
    try:
        return _run_pass(llm, input_data, input_data.architecture, repo_reader, index)
    except Exception as exc:  # noqa: BLE001 - fail-safe: this pass must never break the review
        logger.warning(
            "ArchitectureConsistencyPass: failed (%s: %s); returning no additional findings",
            type(exc).__name__,
            exc,
        )
        return []


def _run_pass(
    llm: LLMClient,
    input_data: CodeReviewInput,
    architecture: Optional[SystemArchitecture],
    repo_reader: Optional[RepoReader],
    index: Optional[CodebaseIndex] = None,
) -> List[CodeReviewIssue]:
    """Core of :func:`find_architecture_and_redundancy_issues`; may raise.

    Split out so its sole caller can wrap it in the fail-safe guard.

    Preconditions:
        - ``index``, when given, was built from this same ``input_data``/
          ``repo_reader``.

    Postconditions:
        - Same contract as :func:`find_architecture_and_redundancy_issues`,
          minus the env-toggle early returns the caller already handled.
        - Delegates budgeting, proactive chunking, ``Agent`` construction, and
          reactive overflow bisect/shrink recovery to
          :func:`~code_review_agent.submission_pass_runner.run_submission_pass`,
          which never raises; a batch's findings are folded into the returned
          list in batch order. An empty runner result (context too small, or
          every batch unrecoverable) folds to ``[]`` -- never ``None`` and
          never a raised exception.
    """
    if index is None:
        index = CodebaseIndex.from_input(input_data, repo_reader=repo_reader)
    if not index.files:
        # No readable submission files: there is nothing to check for
        # architecture fit or redundancy.
        return []

    arch_doc = _flatten_architecture_document(architecture)
    pre_numbered = _effective_pre_numbered(input_data, index)
    tools = _build_tools(index)

    def _build_prompt_for_batch(batch: FileBatch, budgets: SubmissionPassBudgets) -> str:
        return _build_prompt(
            index,
            architecture,
            budgets.max_inline_code_chars,
            max_architecture_chars=budgets.max_extra_body_chars,
            max_manifest_chars=budgets.max_manifest_chars,
            content_items=batch.items,
            batch_index=batch.index,
            total_batches=batch.total,
            is_partial=batch.is_partial,
        )

    def _parse_batch_reply(raw: str) -> List[CodeReviewIssue]:
        data = json.loads(raw)
        findings = _parse_findings(data)
        if findings:
            findings = _validate_findings(index, findings, pre_numbered=pre_numbered)
        return findings

    results = run_submission_pass(
        llm,
        changed_files=list(index.files.items()),
        reasoning_system_prompt=ARCHITECTURE_CONSISTENCY_REASONING_SYSTEM_PROMPT,
        formatting_instructions=ARCHITECTURE_CONSISTENCY_FORMATTING_INSTRUCTIONS,
        build_prompt=_build_prompt_for_batch,
        tools=tools,
        parse=_parse_batch_reply,
        extra_reserved_chars=len(arch_doc),
        finding_array_count=1,
        pass_label="ArchitectureConsistencyPass",
    )
    findings = [finding for batch_findings in results for finding in batch_findings]
    if findings:
        logger.info(
            "ArchitectureConsistencyPass: found %s new finding(s) (architecture/refactor)",
            len(findings),
        )
    return findings
