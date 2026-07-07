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
after recovery (retry, bisection, and a last-resort thinking-off retry) degrades
gracefully — by default its range is surfaced non-blockingly as
``CodeReviewOutput.not_reviewed_ranges`` (never posted as a PR comment, never
blocking) so the run completes over the chunks that succeeded, because a
reviewer-side hiccup is not a code defect. Setting
``CODE_REVIEW_BLOCK_ON_UNREVIEWED`` restores the legacy fail-closed behavior
where that range becomes a blocking ``high`` "not reviewed" finding and the
merged review is rejected. The run still fails loudly with
``CodeReviewUnavailableError`` for infrastructure failures (rate limit,
unreachable endpoint, auth/config) and when *no* chunk could be reviewed at all;
an unexpected error (a defect in the reviewer code, not a known LLM content
failure) propagates unchanged so it fails closed rather than being masked — the
review never renders an approving verdict on code it did not see.

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
profile + resolved model) records the approved
``CodeReviewOutput`` of each submission, and ``run_coordinator`` returns a deep
clone of it before touching the LLM when the same submission comes back — zero
LLM calls (map, verification, and merge all skipped). Only approved outcomes are
stored: a rejection is left to re-run through the (cheap, mostly cached) map
phase so a fix that reappears identical still gets its findings.

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

import logging
import threading
from collections import OrderedDict
from typing import List, Optional, Tuple

from llm_service import LLMClient, compact_text
from shared_env_config import env_bool
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
    _stable_json_digest,
    _submission_fingerprint,
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
from .repo_reader import RepoReader
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
    "_stable_json_digest",
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
    """Resolve the submission cache capacity from the environment.

    Postconditions:
        - Returns ``CODE_REVIEW_SUBMISSION_CACHE_SIZE`` parsed as an int, clamped
          to a floor of 0 (a negative or garbage value becomes the default, an
          explicit 0 disables the short-circuit). ``0`` is load-bearing: callers
          treat it as "no submission cache", so every review runs in full.
    """
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


def _block_on_unreviewed() -> bool:
    """Whether a chunk that could not be reviewed should block the merged review.

    Postconditions:
        - Returns ``True`` only when ``CODE_REVIEW_BLOCK_ON_UNREVIEWED`` is an
          explicit truthy value (``true``/``1``/``yes``/``on``); unset or
          anything else is ``False``. Default off: an unreviewable chunk degrades
          gracefully (no posted "could not be reviewed" finding, no block) and is
          surfaced only as non-blocking ``CodeReviewOutput.not_reviewed_ranges``.
          Set it to restore the legacy fail-closed behavior where the chunk's code
          is named by a blocking ``high`` finding that rejects the review.
    """
    return env_bool("CODE_REVIEW_BLOCK_ON_UNREVIEWED", default=False)


def _not_reviewed_range_label(issue: CodeReviewIssue) -> str:
    """Render a not-reviewed coverage finding as a concise ``path (lines A-B)`` label.

    Postconditions:
        - Returns ``"<path> (lines <start>-<end>)"`` when the finding carries a
          line range, ``"<path>"`` when it does not, and ``"(unknown)"`` for a
          headerless finding with no path. Pure formatting for the non-blocking
          ``not_reviewed_ranges`` observability list; never raises.
    """
    path = issue.file_path or "(unknown)"
    if issue.start_line is not None and issue.line is not None:
        return f"{path} (lines {issue.start_line}-{issue.line})"
    return path


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
    repo_reader: Optional[RepoReader] = None,
) -> CodeReviewOutput:
    """Map-reduce review entry point: bounded chunks in, merged verdict out.

    Preconditions:
        - ``llm`` implements ``LLMClient`` (context sizing + chunk review calls).
        - ``input_data`` carries the code under review via ``files`` or ``code``.
        - ``progress_callback`` is None or satisfies the
          ``ReviewProgressCallback`` contract (non-raising, accepts
          ``(step, detail, fraction)``).
        - ``repo_reader`` is None or a ``repo_reader.RepoReader`` (read-only,
          thread-safe, fail-safe): whole-repo read access handed to the
          false-positive verifier so it can confirm that a file/module a finding
          claims is missing already exists outside the diff. Passed as an
          argument (never a ``CodeReviewInput`` field) so the live reader object
          can never enter the submission/chunk cache keys.

    Postconditions:
        - Every input file/line range is either reviewed or named: empty files
          get info findings, and a chunk that cannot be reviewed after recovery
          is recorded in ``not_reviewed_ranges`` while the run completes over the
          chunks that succeeded (no covered line is silently dropped). By default
          those ranges are non-blocking (never posted, never affecting
          ``approved``); under ``CODE_REVIEW_BLOCK_ON_UNREVIEWED`` they instead
          appear as blocking ``high`` findings in ``issues`` and reject the merge,
          so unreviewed code cannot pass the gate as approved.
        - ``approved is False`` implies at least one critical/high issue.
        - Every genuine reviewer finding is re-checked against the whole
          submission and dropped only when the verifier confirms it is a false
          positive; when that removes the last critical/high finding the gate
          approves (a chunk-local false positive never blocks the merge). The
          check is fail-safe — any verifier failure keeps the findings — and
          never touches the not-reviewed coverage findings.
        - The code under review is never compacted or truncated; only the
          spec/architecture/existing-codebase excerpts are.
        - A submission byte-identical to one this process already approved *and
          fully reviewed* (same code + context + model; no unreviewed ranges)
          returns the recorded approved output with no LLM call at all.
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
    # Resolve the review model once for the whole run: it feeds both the
    # submission fingerprint here and the map-phase context fingerprint below, and
    # is identical throughout (best-effort identity, never raises).
    model_fingerprint = _review_model_fingerprint(llm)

    # Submission-level short-circuit: an identical submission that was already
    # approved reproduces the same verdict, so return its cached output before any
    # LLM work (map, false-positive verification, and merge all skipped). Keyed on
    # the raw input + model only — no compaction — so the check itself costs no
    # model call. Skipped entirely when disabled (size 0); on a miss the run
    # proceeds and stores its verdict below if approved.
    submission_size = _submission_cache_size()
    submission_key: Optional[str] = None
    if submission_size > 0:
        submission_key = _submission_fingerprint(input_data, model_fingerprint)
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
        "user_decisions": input_data.user_decisions or None,
        "profile": input_data.profile,
    }

    # Fingerprint the shared context + resolved model once per run so unchanged
    # chunks reuse their prior map-phase outcome (see module docstring). Computed
    # here (not per chunk) because it is identical for every chunk in this run.
    context_fp = _context_fingerprint(base_input, model_fingerprint)

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
        verified = filter_false_positives(llm, input_data, genuine_issues, repo_reader=repo_reader)

    notify_review_progress(
        progress_callback, "finalizing", "deduplicating findings and applying approval rules", 0.95
    )
    # A chunk that could not be reviewed after recovery degrades gracefully: by
    # default its "not reviewed" coverage findings are NOT posted and do NOT block
    # (they would otherwise surface as an alarming "[HIGH] ... could not be
    # reviewed automatically" PR comment for a reviewer-side hiccup, not a code
    # defect). They are still surfaced non-blockingly via ``not_reviewed_ranges``
    # below and in the telemetry log. Set CODE_REVIEW_BLOCK_ON_UNREVIEWED to
    # restore the legacy fail-closed behavior where they block the merge.
    not_reviewed_ranges = [_not_reviewed_range_label(i) for i in outcome.not_reviewed_issues]
    if _block_on_unreviewed():
        deduped = _dedupe_issues([*verified, *outcome.not_reviewed_issues, *skipped_issues])
    else:
        if not_reviewed_ranges:
            logger.warning(
                "CodeReview: %s chunk range(s) could not be reviewed; degrading gracefully "
                "(not posting/blocking; ranges=%s)",
                len(not_reviewed_ranges),
                not_reviewed_ranges,
            )
        deduped = _dedupe_issues([*verified, *skipped_issues])
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
        not_reviewed_ranges=not_reviewed_ranges,
        summary=merged_summary,
        spec_compliance_notes=spec_notes,
        suggested_commit_message=commit_message,
    )
    # Record only approved verdicts for the submission-level short-circuit: an
    # identical resubmission returns this output with no LLM work. A rejection is
    # not stored — the fix that follows changes the submission, and if the same
    # rejected bytes reappear the (mostly cached) map phase still surfaces the
    # findings the coding agent needs. A run that left any range unreviewed is
    # also not stored: freezing it would keep serving a partial verdict on later
    # identical cycles instead of re-attempting the chunk that could not be
    # reviewed (a semantic-exhaustion/truncation hiccup may not recur), matching
    # the map-phase rule that degraded chunk outcomes are never cached. Store a
    # clone so a later hit can be mutated freely without corrupting the entry.
    if submission_key is not None and result.approved and not not_reviewed_ranges:
        with _SUBMISSION_OUTCOME_CACHE_LOCK:
            _SUBMISSION_OUTCOME_CACHE[submission_key] = result.model_copy(deep=True)
            _SUBMISSION_OUTCOME_CACHE.move_to_end(submission_key)
            while len(_SUBMISSION_OUTCOME_CACHE) > submission_size:
                _SUBMISSION_OUTCOME_CACHE.popitem(last=False)
    return result
