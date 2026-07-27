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
(``read_file``, ``list_files``, ``search_codebase``, ``find_function_at_line``),
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

    - **Bounded cost.** Exactly one LLM call per submission (not per chunk),
      matching the false-positive filter's per-submission cost shape.

    - **``CODE_REVIEW`` profile only.** The other :class:`.profiles.ReviewProfile`
      values narrow the engine to a specific checklist whose contract expects
      every issue to map to a specific criterion/requirement (e.g. the
      acceptance profile's per-criterion attribution); an architecture/refactor
      finding never maps to one of those, so this pass never runs under any
      profile but the default.
"""

from __future__ import annotations

import logging
from typing import List, Optional

from strands import Agent

from llm_service import LLMClient
from shared.env import env_flag_enabled
from shared.llm_recovery import extract_json_object
from software_engineering_team.shared.context_sizing import (
    compute_code_review_arch_overview_chars,
    compute_code_review_map_chunk_chars,
    parse_env_int,
)
from software_engineering_team.shared.models import SystemArchitecture

from .architecture_context import render_architecture_context
from .false_positive_filter import CodebaseIndex, _build_tools, _code_fence_for
from .model_resolution import resolve_code_review_model
from .models import CodeReviewInput, CodeReviewIssue, coerce_line, is_no_op_suggestion
from .profiles import ReviewProfile
from .prompts import ARCHITECTURE_CONSISTENCY_PROMPT
from .repo_reader import RepoReader

logger = logging.getLogger(__name__)

# Default-on toggle: an explicit ``CODE_REVIEW_ARCHITECTURE_CONSISTENCY_PASS=false``/``0``/``no``
# disables the pass (see docs/ENV_VARS.md). Any other value (or unset) leaves it enabled.
_PASS_ENV = "CODE_REVIEW_ARCHITECTURE_CONSISTENCY_PASS"

# Cap on the architecture document inlined into the single prompt this pass sends.
# Generous relative to the per-chunk excerpt cap (``CODE_REVIEW_ARCH_OVERVIEW_CHARS``)
# because this pass pays the cost once per submission, not once per chunk.
_ARCH_DOC_ABS_CHARS = 40_000  # CODE_REVIEW_ARCH_DOC_CHARS, floor 2_000

_ALLOWED_CATEGORIES = frozenset({"architecture", "refactor"})
_ALLOWED_SEVERITIES = frozenset({"critical", "high", "medium", "low", "info"})


def _build_prompt(
    index: CodebaseIndex,
    architecture: SystemArchitecture,
    max_inline_chars: int,
    max_arch_doc_chars: int,
) -> str:
    """Render the single user prompt for this pass.

    Postconditions:
        - Inlines the architecture document up to ``max_arch_doc_chars``
          (folding in the rendered ``overview``/``components``/``decisions``
          alongside it, or in its place when no full document is set), then the
          submission's changed files up to a combined ``max_inline_chars``
          budget. A changed-file omission is identified as reachable via the
          attached tools (``read_file``/``list_files``, which cover the
          submission and repository); an architecture-document omission is
          identified as unavailable, since no tool exposes the document
          itself -- neither is silently dropped.
    """
    parts: List[str] = []

    arch_doc = "\n\n".join(
        p
        for p in (
            (architecture.architecture_document or "").strip(),
            render_architecture_context(architecture),
        )
        if p
    )
    inlined_doc = arch_doc[:max_arch_doc_chars]
    doc_fence = _code_fence_for(inlined_doc)
    parts.append("**Architecture document:**")
    parts.append(doc_fence)
    parts.append(inlined_doc or "(no architecture document provided)")
    parts.append(doc_fence)
    if len(arch_doc) > len(inlined_doc):
        parts.append(
            f"(Only the first {len(inlined_doc)} characters of the architecture document "
            "are shown above; the rest is not available through the attached tools -- do not "
            "flag a contradiction with content beyond this cutoff.)"
        )
    parts.append("")

    changed_files = list(index.files.items())
    manifest = [path for path, _ in changed_files]
    parts.append(f"**Changed files in this submission ({len(manifest)}):**")
    parts.extend(manifest)
    parts.append("")

    parts.append("**Full content of the changed files:**")
    remaining = max_inline_chars
    omitted = 0
    for i, (path, content) in enumerate(changed_files):
        if remaining <= 0:
            omitted = len(changed_files) - i
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
        "above) before flagging a cross-codebase duplicate -- search_codebase only searches the "
        "files shown in this prompt, not the wider repository. Use read_file() to confirm any "
        "architecture contradiction against the document above."
    )
    parts.append(
        'Return a single JSON object with a "findings" array as instructed. Return '
        '{"findings": []} if you find nothing in either category.'
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
          is satisfied), when ``input_data.architecture`` is absent or
          carries none of an ``architecture_document``, ``overview``,
          ``components``, or ``decisions`` (nothing to check a contradiction
          against). Also returns ``[]`` (still with no LLM call) when the
          submission has no readable files — checked inside :func:`_run_pass`
          rather than here, once the index has been built.
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
    architecture = input_data.architecture
    if architecture is None or not (
        (architecture.architecture_document or "").strip()
        or render_architecture_context(architecture).strip()
    ):
        return []
    try:
        return _run_pass(llm, input_data, architecture, repo_reader, index)
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
    architecture: SystemArchitecture,
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
          minus the env-toggle/no-architecture-document early returns the
          caller already handled.
    """
    if index is None:
        index = CodebaseIndex.from_input(input_data, repo_reader=repo_reader)
    if not index.files:
        # No readable submission files: there is nothing to check for
        # architecture fit or redundancy against the architecture document.
        return []

    model = resolve_code_review_model(llm)
    configured_arch_doc_chars = parse_env_int(
        "CODE_REVIEW_ARCH_DOC_CHARS", _ARCH_DOC_ABS_CHARS, 2_000
    )
    # ``compute_code_review_map_chunk_chars`` plus ``compute_code_review_arch_overview_chars``
    # approximates the total content this model's context can carry for a code+architecture
    # prompt of this shape (the two already sum to a context-derived ceiling for the map-phase
    # prompt this pass's own prompt closely resembles). Split that total between the architecture
    # document and the inlined code -- at most half to the document -- rather than inlining the
    # full (env-configurable, not context-aware) document cap unconditionally: on a smaller-context
    # model a generous CODE_REVIEW_ARCH_DOC_CHARS could otherwise consume the whole budget by
    # itself and push the combined prompt past the model's real context regardless of how much the
    # code side shrinks.
    available_total = compute_code_review_map_chunk_chars(
        llm
    ) + compute_code_review_arch_overview_chars(llm)
    max_arch_doc_chars = min(configured_arch_doc_chars, available_total // 2)
    max_inline_chars = max(available_total - max_arch_doc_chars, 0)

    prompt = _build_prompt(index, architecture, max_inline_chars, max_arch_doc_chars)
    agent = Agent(
        model=model,
        system_prompt=ARCHITECTURE_CONSISTENCY_PROMPT,
        tools=_build_tools(index),
    )
    raw = str(agent(prompt)).strip()
    parsed = extract_json_object(raw, required_keys=["findings"])
    if parsed is None:
        logger.warning(
            "ArchitectureConsistencyPass: could not parse a JSON object from the LLM reply "
            "(%d chars); treating as no new findings",
            len(raw),
        )
    data = parsed or {}
    findings = _parse_findings(data)
    if findings:
        findings = _validate_findings(index, findings, pre_numbered=input_data.pre_numbered)
        logger.info(
            "ArchitectureConsistencyPass: found %s new finding(s) (architecture/refactor)",
            len(findings),
        )
    return findings
