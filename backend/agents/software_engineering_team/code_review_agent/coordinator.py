"""Code Review Coordinator: map-reduce review with bounded per-call prompts.

Pipeline: input → (path, content) blocks → bounded ``FileSegment``s →
``ReviewChunk``s (``chunking``) → per-chunk LLM review with retry/bisect
recovery and the map-phase cache (``mapping``) → false-positive verification
(each genuine finding is re-checked against the *whole* submission, since a
chunk reviewer saw only a slice, and confirmed false positives are dropped — see
``false_positive_filter``) → deterministic merge (dedupe, severity gate, safety
nets). Every LLM call carries at most ``compute_code_review_map_chunk_chars`` of
code regardless of input size, and no input file is ever silently dropped:
empty files are named by info findings, and a chunk that cannot be reviewed
after recovery degrades to a blocking ``high`` "not reviewed" finding naming
its range so the run completes over the chunks that succeeded while the merged
review is rejected — unreviewed code never passes the gate as approved. The run
still fails loudly with ``CodeReviewUnavailableError`` for infrastructure
failures (rate limit, unreachable endpoint, auth/config) and when *no* chunk
could be reviewed at all; an unexpected error (a defect in the reviewer code,
not a known LLM content failure) propagates unchanged so it fails closed rather
than being masked — the review never renders an approving verdict on code it
did not see.

This module owns the orchestration (``run_coordinator``) and the reduce phase
(dedupe, approval gate, narrative merge). The chunking transforms live in
``chunking`` and the map phase (per-chunk review, recovery, cache, sibling
surface) in ``mapping``; both are re-exported here so call sites and tests can
keep importing from ``coordinator``.

Map-phase cache: the review→fix→re-review loop re-invokes the whole coordinator
after every batch fix, but a fix only mutates the files that had issues, so most
chunks are byte-identical to the previous cycle. A process-global, bounded LRU
(``CODE_REVIEW_CHUNK_OUTCOME_CACHE_SIZE``) keyed on the chunk's exact LLM input
(``chunk.content`` + segment notes) plus a context fingerprint (the shared
task/spec/architecture/profile inputs and the resolved review model) reuses the
prior map-phase ``_ChunkOutcome`` for any unchanged chunk, so only chunks the
fix actually touched go back through the LLM. The cache is scoped to the map
phase only: the false-positive *verification* pass always re-runs on the current
submission (a finding can flip because a *different*, changed chunk altered
cross-file context, and verification reads the whole codebase), so no safety
guarantee is weakened. Only fully-reviewed outcomes are cached — degraded "not
reviewed" outcomes are never stored, so a transient failure is retried for real
next cycle. The cache is best-effort: a miss simply recomputes, so correctness
never depends on a hit, and any change to code, context, or model invalidates
the key.

Submission-level short-circuit: the map-phase cache still re-runs the reduce and
the false-positive *verification* pass on every cycle, so re-reviewing a
byte-identical submission that was already approved is not free. A second,
coarser process-global LRU (``CODE_REVIEW_SUBMISSION_CACHE_SIZE``) keyed on the
whole raw ``CodeReviewInput`` (files/code + task/spec/architecture context +
profile + resolved model, but *not* ``changed_files``) records the approved
``CodeReviewOutput`` of each submission, and ``run_coordinator`` returns a deep
clone of it before touching the LLM when the same submission comes back — zero
LLM calls (map, verification, and merge all skipped). Only approved outcomes are
stored: a rejection is left to re-run through the (cheap, mostly cached) map
phase so a fix that reappears identical still gets its findings. The key omits
``changed_files`` so an identical full submission matches regardless of any
changed-files review-scoping hint.

Changed-files scoping: on a fix-pass retry the caller can set
``CodeReviewInput.changed_files`` to just the paths the fix touched. Only those
become primary map chunks, so unchanged files are neither re-chunked nor
re-flagged (cutting their false-positive re-verification), while every file stays
in the whole-submission false-positive index (built from ``input_data.files``),
so cross-file checks still reach them. The *sibling surface* below is still
computed over the full submission, so a changed chunk keeps full visibility of
what its unchanged siblings define.

Cross-file surface: each chunk reviewer is also given the *sibling surface* —
the top-level symbols (Python ``def``/``class``, TS/JS ``export``s) defined by
the other changed files in the submission that are not in this chunk — so it can
flag a reference to a symbol a sibling renamed or removed, a cross-file break a
bounded single-chunk view would otherwise miss. That surface is folded into the
chunk's cache key, so a sibling's *surface* change (a rename/removal) re-runs the
dependent chunk with the new surface, while a body-only sibling edit leaves the
surface unchanged and the chunk stays cached — closing the cross-file gap without
invalidating the whole submission on every fix.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from collections import OrderedDict
from typing import List, Optional, Tuple

from llm_service import LLMClient, compact_text
from software_engineering_team.shared.context_sizing import (
    compute_code_review_arch_overview_chars,
    compute_code_review_existing_codebase_chars,
    compute_code_review_map_chunk_chars,
    compute_code_review_spec_excerpt_chars,
    parse_env_int,
)

from .chunk_reviewer import ChunkReviewAgent
from .chunking import (
    MIN_SPLIT_SEGMENT_CHARS,
    _blocks_from_input,
    _issues_from_chunk_output,
    _map_parallelism,
    _normalize_issue_path,
    _segment_range_label,
    _validate_line,
    build_review_chunks,
    cap_chunk_content,
    cap_review_chunk,
    parse_code_into_file_blocks,
    split_block_into_segments,
)
from .false_positive_filter import filter_false_positives
from .mapping import (
    _cached_review_chunk,
    _chunk_cache_key,
    _ChunkOutcome,
    _context_fingerprint,
    _is_content_failure,
    _is_infra_failure,
    _map_chunks,
    _review_chunk_with_recovery,
    _review_model_fingerprint,
    _sibling_surface,
    _surface_by_path,
    _symbol_surface,
    clear_chunk_outcome_cache,
)
from .models import (
    CodeReviewInput,
    CodeReviewIssue,
    CodeReviewOutput,
    CodeReviewUnavailableError,
    ReviewProgressCallback,
    notify_review_progress,
)
from .synthesis import synthesize_review_findings

logger = logging.getLogger(__name__)

# Names re-exported from ``chunking``/``mapping`` so existing call sites and
# tests can keep importing them from ``coordinator`` after the module split.
# Listing them here also marks the otherwise-unused imports above as public
# re-exports (so linters don't flag them).
__all__ = [
    "run_coordinator",
    "clear_submission_outcome_cache",
    "_submission_fingerprint",
    "_select_changed_blocks",
    "MIN_SPLIT_SEGMENT_CHARS",
    "parse_code_into_file_blocks",
    "split_block_into_segments",
    "build_review_chunks",
    "cap_chunk_content",
    "cap_review_chunk",
    "clear_chunk_outcome_cache",
    "_blocks_from_input",
    "_issues_from_chunk_output",
    "_map_parallelism",
    "_normalize_issue_path",
    "_segment_range_label",
    "_validate_line",
    "_ChunkOutcome",
    "_cached_review_chunk",
    "_chunk_cache_key",
    "_context_fingerprint",
    "_is_content_failure",
    "_is_infra_failure",
    "_map_chunks",
    "_review_chunk_with_recovery",
    "_review_model_fingerprint",
    "_sibling_surface",
    "_surface_by_path",
    "_symbol_surface",
]


# Process-global submission-level short-circuit cache (see module docstring).
# Bounded LRU mapping a whole-submission fingerprint -> the approved
# ``CodeReviewOutput`` it produced, so an identical, previously-approved
# submission returns without any LLM call. Guarded by a lock because reviews run
# concurrently across jobs in one process. ``0`` disables it (every run is a
# guaranteed miss). Coarser and independent of the per-chunk cache in ``mapping``.
DEFAULT_SUBMISSION_CACHE_SIZE = 256  # CODE_REVIEW_SUBMISSION_CACHE_SIZE, floor 0

_SUBMISSION_OUTCOME_CACHE: "OrderedDict[str, CodeReviewOutput]" = OrderedDict()
_SUBMISSION_OUTCOME_CACHE_LOCK = threading.Lock()


def _submission_cache_size() -> int:
    return parse_env_int("CODE_REVIEW_SUBMISSION_CACHE_SIZE", DEFAULT_SUBMISSION_CACHE_SIZE, 0)


def clear_submission_outcome_cache() -> None:
    """Drop every cached approved submission outcome.

    Postconditions:
        - The process-global submission cache is empty; the next review of any
          submission is a guaranteed miss. Intended for tests (the cache persists
          across ``run_coordinator`` calls by design) and for callers that must
          force a cold review.
    """
    with _SUBMISSION_OUTCOME_CACHE_LOCK:
        _SUBMISSION_OUTCOME_CACHE.clear()


def _submission_fingerprint(input_data: CodeReviewInput, model_fingerprint: str) -> str:
    """Hash the whole raw submission plus the resolved model.

    Preconditions:
        - ``input_data`` is a valid ``CodeReviewInput``.
        - ``model_fingerprint`` is ``_review_model_fingerprint(llm)`` for the
          client that would run the review.

    Postconditions:
        - Returns a hex digest that changes whenever any field that could change
          the review verdict changes (the code under review, task/spec/
          architecture context, profile, false-positive toggle, or the model),
          so a cache hit means the review would be byte-for-byte the same work.
        - ``changed_files`` is deliberately excluded: it only narrows which files
          are re-chunked, never what the full submission *is*, so an identical
          submission matches whether or not a changed-files hint is present.
        - Computed from raw fields only (no compaction/LLM), so the short-circuit
          it guards fires before any model call. Deterministic (``sort_keys``),
          so a stored approval survives across coordinator calls in a process.
    """
    architecture = None
    if input_data.architecture is not None:
        architecture = input_data.architecture.model_dump(mode="json")
    normalized = {
        "code": input_data.code or "",
        "files": input_data.files or None,
        "pre_numbered": input_data.pre_numbered,
        "spec_content": input_data.spec_content or "",
        "task_description": input_data.task_description or "",
        "task_requirements": input_data.task_requirements or "",
        "acceptance_criteria": input_data.acceptance_criteria or [],
        "language": input_data.language or "",
        "architecture": architecture,
        "existing_codebase": input_data.existing_codebase or None,
        "user_decisions": input_data.user_decisions or None,
        "profile": getattr(input_data.profile, "value", input_data.profile),
        "skip_false_positive_filter": input_data.skip_false_positive_filter,
        "__model__": model_fingerprint,
    }
    payload = json.dumps(normalized, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _select_changed_blocks(
    blocks: List[Tuple[str, str]], changed_files: Optional[List[str]]
) -> List[Tuple[str, str]]:
    """Narrow the review blocks to the fix's changed files, fail-safe.

    Preconditions:
        - ``blocks`` are the ``(path, content)`` blocks for the whole submission
          (from ``_blocks_from_input``).

    Postconditions:
        - ``changed_files is None`` returns ``blocks`` unchanged (a full review —
          today's behavior and every caller that omits the hint).
        - Otherwise returns the blocks whose path is in ``changed_files``,
          preserving order. Unchanged files are dropped as *primary chunks* only;
          the caller keeps them in ``input_data.files`` for the false-positive
          index, so no changed line goes unreviewed and cross-file checks still
          reach them.
        - Fail-safe: if the filter would drop everything while ``blocks`` is
          non-empty (a stale or mistaken hint naming no current path), the full
          ``blocks`` are returned — the review never silently shrinks to nothing.
    """
    if changed_files is None:
        return blocks
    wanted = set(changed_files)
    selected = [block for block in blocks if block[0] in wanted]
    if not selected and blocks:
        logger.warning(
            "CodeReviewCoordinator: changed_files hint (%d paths) matched no current "
            "review block; falling back to a full review",
            len(wanted),
        )
        return blocks
    return selected


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
            chunk_spec_notes=outcome.spec_notes,
        )
        if synthesized is not None:
            return synthesized.summary, synthesized.spec_compliance_notes

    return concatenated_summary, concatenated_notes


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
          get info findings, and a chunk that cannot be reviewed after recovery
          degrades to a blocking ``high`` "not reviewed" finding naming its
          range while the run completes over the chunks that succeeded. The
          degraded finding rejects the merged review, so unreviewed code never
          passes the gate as approved; no covered line is silently dropped.
        - ``approved is False`` implies at least one critical/high issue.
        - Every genuine reviewer finding is re-checked against the whole
          submission and dropped only when the verifier confirms it is a false
          positive; when that removes the last critical/high finding the gate
          approves (a chunk-local false positive never blocks the merge). The
          check is fail-safe — any verifier failure keeps the findings — and
          never touches the not-reviewed coverage findings.
        - The code under review is never compacted or truncated; only the
          spec/architecture/existing-codebase excerpts are.
        - A submission byte-identical to one this process already approved (same
          code + context + model) returns the recorded approved output with no
          LLM call at all. When ``input_data.changed_files`` is set, only those
          paths are reviewed as primary chunks; the whole submission still
          populates the false-positive index, so no changed line goes unreviewed
          and unchanged files stay reachable for cross-file verification.
        - When ``progress_callback`` is provided, it is invoked with
          non-decreasing fractions ending at 1.0 (step ``done``) on every
          successful return, including per-chunk ``reviewing`` reports.

    Raises:
        CodeReviewUnavailableError: when the review model is unavailable
            (an infrastructure failure: rate limit, unreachable endpoint, or
            auth/config error), or when *no* chunk could be reviewed at all —
            the run never renders a verdict on a submission it did not see.
        Exception: an unexpected reviewer defect (not a known LLM content
            failure) propagates unchanged, failing closed so the bug surfaces
            instead of being masked as a not-reviewed finding.
    """
    # Submission-level short-circuit: an identical submission that was already
    # approved reproduces the same verdict, so return its cached output before any
    # LLM work (map, false-positive verification, and merge all skipped). Keyed on
    # the raw input + model only — no compaction — so the check itself costs no
    # model call. Skipped entirely when disabled (size 0); on a miss the run
    # proceeds and stores its verdict below if approved.
    submission_size = _submission_cache_size()
    submission_key: Optional[str] = None
    if submission_size > 0:
        submission_key = _submission_fingerprint(input_data, _review_model_fingerprint(llm))
        with _SUBMISSION_OUTCOME_CACHE_LOCK:
            cached = _SUBMISSION_OUTCOME_CACHE.get(submission_key)
            if cached is not None:
                _SUBMISSION_OUTCOME_CACHE.move_to_end(submission_key)
        if cached is not None:
            logger.info("CodeReviewCoordinator: submission cache hit; skipping review (approved)")
            notify_review_progress(
                progress_callback, "done", "identical approved submission; review skipped", 1.0
            )
            return cached.model_copy(deep=True)

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

    # On a fix-pass retry the caller may scope the review to just the files the fix
    # changed: only those become primary map chunks. Every file stays in ``blocks``
    # for the sibling surface and in ``input_data.files`` for the false-positive
    # index, so unchanged files remain reachable for cross-file checks — they are
    # simply not re-reviewed as primary chunks (nor re-flagged, cutting their
    # false-positive re-verification). A full review (no hint) is unchanged.
    review_blocks = _select_changed_blocks(blocks, input_data.changed_files)
    chunks = build_review_chunks(
        review_blocks, compute_code_review_map_chunk_chars(llm), input_data.pre_numbered
    )
    logger.info(
        "CodeReviewCoordinator: %s blocks (%s reviewed) -> %s chunks",
        len(blocks),
        len(review_blocks),
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
        "user_decisions": input_data.user_decisions or None,
        "profile": input_data.profile,
    }

    # Fingerprint the shared context + resolved model once per run so unchanged
    # chunks reuse their prior map-phase outcome (see module docstring). Computed
    # here (not per chunk) because it is identical for every chunk in this run.
    context_fp = _context_fingerprint(base_input, _review_model_fingerprint(llm))

    # Top-level symbol surface of every changed file, so each chunk's reviewer can
    # see what its *siblings* define/export and flag references to a symbol a
    # sibling renamed or removed — a cross-file break a bounded single-chunk view
    # would otherwise miss. Folded into each chunk's cache key so a sibling's
    # surface change re-runs the dependent chunk while body-only edits stay cached.
    surface_by_path = _surface_by_path(blocks)

    chunk_reviewer = ChunkReviewAgent(llm)
    outcome = _ChunkOutcome()
    for per_chunk in _map_chunks(
        chunk_reviewer, chunks, base_input, context_fp, surface_by_path, progress_callback
    ):
        outcome.absorb(per_chunk)

    # Total-failure guard: individual chunks degrade gracefully to a
    # not-reviewed finding, but a run in which *no* chunk produced a verdict
    # has reviewed nothing — rendering approved/rejected here would be a
    # verdict on code we never saw. Fail loudly instead, naming what went
    # unreviewed (the degraded not-reviewed findings already record the ranges).
    if not outcome.approved_flags:
        raise CodeReviewUnavailableError(
            "No chunk could be reviewed after recovery; no verdict was produced for this submission.",
            unreviewed=[i.description for i in (outcome.not_reviewed_issues + outcome.issues)],
        )

    # False-positive verification: re-check each genuine reviewer finding against
    # the *whole* submission. Each chunk review saw only a bounded slice, so a
    # finding can be wrong because the resolving code lived in a part of the file
    # (or another file) it never saw. The filter reads the real code and drops
    # only the findings it confirms are false positives. Coverage/safety findings
    # (``not_reviewed_issues``, empty-file notices) are never passed in, so the
    # gate's anti-loop nets stay intact; on any verifier failure the findings are
    # kept (fail-safe).
    genuine_issues = _dedupe_issues(outcome.issues)
    notify_review_progress(
        progress_callback,
        "verifying",
        f"verifying {len(genuine_issues)} findings against the full codebase",
        0.92,
    )
    if input_data.skip_false_positive_filter:
        # The calling gate opted out of the whole-codebase re-check and stands
        # behind its per-chunk findings as-is (e.g. a gate whose findings must
        # never be silently dropped). Skipping is purely a removal of the
        # drop-false-positives step, so it can only ever keep more findings.
        verified = genuine_issues
    else:
        verified = filter_false_positives(llm, input_data, genuine_issues)

    notify_review_progress(
        progress_callback, "finalizing", "deduplicating findings and applying approval rules", 0.95
    )
    deduped = _dedupe_issues([*verified, *outcome.not_reviewed_issues, *skipped_issues])
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
    result = CodeReviewOutput(
        approved=approved,
        issues=deduped,
        summary=merged_summary,
        spec_compliance_notes=spec_notes,
        suggested_commit_message=commit_message,
    )
    # Record only approved verdicts for the submission-level short-circuit: an
    # identical resubmission returns this output with no LLM work. A rejection is
    # not stored — the fix that follows changes the submission, and if the same
    # rejected bytes reappear the (mostly cached) map phase still surfaces the
    # findings the coding agent needs. Store a clone so a later hit can be mutated
    # freely without corrupting the cached entry.
    if submission_key is not None and result.approved:
        with _SUBMISSION_OUTCOME_CACHE_LOCK:
            _SUBMISSION_OUTCOME_CACHE[submission_key] = result.model_copy(deep=True)
            _SUBMISSION_OUTCOME_CACHE.move_to_end(submission_key)
            while len(_SUBMISSION_OUTCOME_CACHE) > submission_size:
                _SUBMISSION_OUTCOME_CACHE.popitem(last=False)
    return result
