"""Temporal activities for the code review agent's map-reduce pipeline.

Each activity wraps an existing pure function from the in-process coordinator so
the durable workflow (:mod:`.workflows`) can drive the same review, phase by
phase, with independent retries and resumability:

- :func:`prepare_review_activity` — compact shared context + split into bounded
  chunks (wraps ``coordinator`` prep + ``chunking.build_review_chunks``).
- :func:`review_chunk_activity` — review ONE chunk with recovery + the map-phase
  cache (wraps ``mapping._cached_review_chunk``); this is the fan-out unit.
- :func:`filter_false_positives_activity` — re-check findings against the whole
  submission (wraps ``false_positive_filter.filter_false_positives``).
- :func:`finalize_review_activity` — deterministic reduce gate: dedupe +
  approval reconciliation (wraps ``coordinator._dedupe_issues`` /
  ``_reconcile_approval``).
- :func:`synthesize_findings_activity` — merge multi-chunk narratives (wraps
  ``synthesis.synthesize_review_findings``).

Each activity is a plain **sync** function (run in the worker's thread-pool
executor) whose heavy imports live inside the body, keeping module import — which
the workflow sandbox replays — cheap and side-effect free. All payloads cross the
boundary as ``model_dump(mode="json")`` dicts and are reconstructed with
``model_validate``.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from temporalio import activity

logger = logging.getLogger(__name__)

# Background-heartbeat cadence (seconds) for the (potentially long) per-chunk map
# call, kept well under the activity's ``heartbeat_timeout`` so a live review is
# never mistaken for a stalled worker.
_MAP_HEARTBEAT_INTERVAL_S = 30.0


def _resolve_llm() -> Any:
    """Resolve the shared code-review LLM client inside a worker activity.

    Postconditions:
        - Returns the same client ``CodeReviewAgent`` uses in thread mode
          (``get_client("code_review")``), so a Temporal review resolves models
          and providers identically to an in-process one.
    """
    from llm_service import get_client

    return get_client("code_review")


@activity.defn(name="code_review_prepare")
def prepare_review_activity(review_input: Dict[str, Any]) -> Dict[str, Any]:
    """Prepare a submission for the map phase: compact context + build chunks.

    Preconditions:
        - ``review_input`` is a ``CodeReviewInput.model_dump(mode="json")`` dict.

    Postconditions:
        - Returns a ``ReviewPrepDTO`` dict. When the submission carries no
          reviewable code, ``no_code`` is True and ``skipped_issues`` names any
          empty files (the workflow then returns an approved empty verdict).
          Otherwise it carries the bounded ``chunks``, the shared ``base_input``
          (profile normalized to its ``.value``), the ``context_fp`` and
          ``surface_by_path`` fingerprints, and ``single_chunk``.
        - The code under review is never compacted or truncated; only the
          spec/architecture/existing-codebase excerpts are (identical to
          ``run_coordinator``'s prep).
    """
    from llm_service import compact_text
    from software_engineering_team.shared.context_sizing import (
        compute_code_review_arch_overview_chars,
        compute_code_review_existing_codebase_chars,
        compute_code_review_map_chunk_chars,
        compute_code_review_spec_excerpt_chars,
    )

    from ..chunking import _blocks_from_input, build_review_chunks
    from ..mapping import _context_fingerprint, _review_model_fingerprint, _surface_by_path
    from ..models import CodeReviewInput, CodeReviewIssue
    from .phase_models import ReviewPrepDTO

    llm = _resolve_llm()
    input_data = CodeReviewInput.model_validate(review_input)

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
        return ReviewPrepDTO(no_code=True, skipped_issues=skipped_issues).model_dump(mode="json")

    max_spec = compute_code_review_spec_excerpt_chars(llm)
    max_arch = compute_code_review_arch_overview_chars(llm)
    max_existing = compute_code_review_existing_codebase_chars(llm)
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

    base_input = {
        "language": input_data.language or "",
        "task_description": input_data.task_description or "",
        "task_requirements": input_data.task_requirements or "",
        "acceptance_criteria": input_data.acceptance_criteria or [],
        "spec_excerpt": spec_content,
        "architecture_overview": arch_overview,
        "existing_codebase_excerpt": existing_codebase or None,
        "user_decisions": input_data.user_decisions or None,
        # Normalize the enum to its value so the DTO stays JSON-native; the map
        # activity rebuilds a ``ChunkReviewInput`` from it and pydantic coerces
        # the string back to ``ReviewProfile``.
        "profile": input_data.profile.value,
    }
    model_fp = _review_model_fingerprint(llm)
    context_fp = _context_fingerprint(base_input, model_fp)
    surface_by_path = _surface_by_path(blocks)

    return ReviewPrepDTO(
        no_code=False,
        skipped_issues=skipped_issues,
        chunks=chunks,
        base_input=base_input,
        context_fp=context_fp,
        surface_by_path=surface_by_path,
        single_chunk=len(chunks) == 1,
    ).model_dump(mode="json")


@activity.defn(name="code_review_map_chunk")
def review_chunk_activity(
    chunk: Dict[str, Any],
    base_input: Dict[str, Any],
    context_fp: str,
    surface_by_path: Dict[str, List[str]],
) -> Dict[str, Any]:
    """Review exactly one chunk — the durable fan-out unit of the map phase.

    Preconditions:
        - ``chunk`` is a ``ReviewChunk.model_dump()`` dict, ``base_input`` the
          shared ``ChunkReviewInput`` fields, ``context_fp`` the run's context
          fingerprint, and ``surface_by_path`` the whole submission's surface.

    Postconditions:
        - Returns a ``ChunkOutcomeDTO`` dict covering every line of the chunk —
          each line reviewed or named by a blocking ``high`` "not reviewed"
          finding — reusing ``mapping._cached_review_chunk`` so its recovery,
          bisection, and process-global map cache behave exactly as in thread
          mode.
        - Emits background heartbeats so a long single-chunk review is not
          mistaken for a stalled worker (paired with the activity's
          ``heartbeat_timeout``).

    Raises:
        - An infrastructure failure (rate limit / unreachable / auth) surfaces as
          ``CodeReviewUnavailableError`` and an unexpected reviewer defect
          propagates unchanged (fail closed) — the workflow's ``RetryPolicy`` then
          governs re-attempts.
    """
    from shared_concurrency import BackgroundHeartbeat

    from ..chunk_reviewer import ChunkReviewAgent
    from ..mapping import _cached_review_chunk, _sibling_surface
    from ..models import ReviewChunk
    from .phase_models import ChunkOutcomeDTO

    llm = _resolve_llm()
    reviewer = ChunkReviewAgent(llm)
    review_chunk_obj = ReviewChunk.model_validate(chunk)
    sibling_surface = _sibling_surface(review_chunk_obj, surface_by_path)

    with BackgroundHeartbeat(activity.heartbeat, _MAP_HEARTBEAT_INTERVAL_S, copy_context=True):
        outcome = _cached_review_chunk(
            reviewer,
            review_chunk_obj,
            base_input,
            context_fp,
            sibling_surface,
            surface_by_path,
        )
    return ChunkOutcomeDTO.from_outcome(outcome).model_dump(mode="json")


@activity.defn(name="code_review_verify_false_positives")
def filter_false_positives_activity(
    review_input: Dict[str, Any],
    issues: List[Dict[str, Any]],
    skip: bool,
) -> List[Dict[str, Any]]:
    """Dedupe genuine findings and drop confirmed false positives.

    Preconditions:
        - ``issues`` are the aggregated genuine reviewer findings (dicts);
          coverage/safety findings must NOT be included (the workflow keeps them
          separate).

    Postconditions:
        - The findings are first deduped (``coordinator._dedupe_issues``), exactly
          as ``run_coordinator`` does before verification.
        - When ``skip`` is True (the caller opted out via
          ``skip_false_positive_filter``) the deduped genuine findings are
          returned unchanged — skipping can only keep more findings.
        - Otherwise each finding is re-checked against the whole submission and
          confirmed false positives are dropped; the pass is fail-safe (any
          failure keeps the findings). ``repo_reader`` is not available across the
          Temporal boundary, so out-of-diff "missing file" confirmations are not
          performed here — a strictly more conservative (keep-more) behavior.
    """
    from ..coordinator import _dedupe_issues
    from ..false_positive_filter import filter_false_positives
    from ..models import CodeReviewInput, CodeReviewIssue

    input_data = CodeReviewInput.model_validate(review_input)
    genuine = _dedupe_issues([CodeReviewIssue.model_validate(i) for i in issues])
    if skip:
        return [i.model_dump(mode="json") for i in genuine]

    llm = _resolve_llm()
    verified = filter_false_positives(llm, input_data, genuine, repo_reader=None)
    return [i.model_dump(mode="json") for i in verified]


@activity.defn(name="code_review_finalize")
def finalize_review_activity(
    verified_issues: List[Dict[str, Any]],
    not_reviewed_issues: List[Dict[str, Any]],
    skipped_issues: List[Dict[str, Any]],
    approved_flags: List[bool],
) -> Dict[str, Any]:
    """Deterministic reduce gate: dedupe the merged findings and reconcile approval.

    Preconditions:
        - ``verified_issues`` are the post-false-positive genuine findings,
          ``not_reviewed_issues`` the coverage findings, ``skipped_issues`` the
          empty-file info findings, and ``approved_flags`` one bool per successful
          sub-review.

    Postconditions:
        - Returns ``{"approved": bool, "issues": [issue dicts]}`` computed by
          ``coordinator._dedupe_issues`` over the merged findings and
          ``coordinator._reconcile_approval`` with the anti-loop safety nets — the
          identical deterministic gate ``run_coordinator`` applies, so the verdict
          matches thread mode.
    """
    from ..coordinator import _dedupe_issues, _reconcile_approval
    from ..models import CodeReviewIssue

    verified = [CodeReviewIssue.model_validate(i) for i in verified_issues]
    not_reviewed = [CodeReviewIssue.model_validate(i) for i in not_reviewed_issues]
    skipped = [CodeReviewIssue.model_validate(i) for i in skipped_issues]

    deduped = _dedupe_issues([*verified, *not_reviewed, *skipped])
    all_llm_approved = bool(approved_flags) and all(approved_flags)
    approved, deduped = _reconcile_approval(all_llm_approved, deduped)
    return {"approved": approved, "issues": [i.model_dump(mode="json") for i in deduped]}


@activity.defn(name="code_review_synthesize")
def synthesize_findings_activity(
    review_input: Dict[str, Any],
    approved: bool,
    issues: List[Dict[str, Any]],
    chunk_summaries: List[str],
    chunk_spec_notes: List[str],
) -> Optional[Dict[str, str]]:
    """Merge multi-chunk findings into one narrative (summary + spec notes).

    Preconditions:
        - Called only when more than one sub-review produced a summary.
        - ``approved`` is the deterministic verdict (context only; never
          recomputed here).

    Postconditions:
        - Returns ``{"summary", "spec_compliance_notes"}`` on success, or ``None``
          on any failure (so the workflow falls back to deterministic
          concatenation). Wraps ``synthesis.synthesize_review_findings``, which
          never raises and never touches the verdict.
    """
    from ..models import CodeReviewInput, CodeReviewIssue
    from ..synthesis import synthesize_review_findings

    input_data = CodeReviewInput.model_validate(review_input)
    result = synthesize_review_findings(
        _resolve_llm(),
        input_data=input_data,
        approved=approved,
        issues=[CodeReviewIssue.model_validate(i) for i in issues],
        chunk_summaries=chunk_summaries,
        chunk_spec_notes=chunk_spec_notes,
    )
    if result is None:
        return None
    return {"summary": result.summary, "spec_compliance_notes": result.spec_compliance_notes}
