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
- :func:`find_architecture_and_redundancy_activity` — once-per-submission
  architecture-consistency / cross-codebase-redundancy pass (wraps
  ``architecture_consistency_pass.find_architecture_and_redundancy_issues``).
- :func:`find_side_effect_impact_activity` — once-per-submission side-effect /
  blast-radius pass (wraps
  ``side_effect_impact_pass.find_side_effect_impact_issues``). Kept alongside
  :func:`find_architecture_and_side_effect_activity` (below) so in-flight
  workflow histories recorded before the merged pass existed keep replaying.
- :func:`find_architecture_and_side_effect_activity` — once-per-submission
  merged architecture-consistency + side-effect-impact pass, one LLM call
  (wraps
  ``merged_architecture_side_effect_pass.find_architecture_and_side_effect_issues``).
  Current workflow executions use this in place of the two passes above.
- :func:`consolidate_side_effect_issues_activity` — optional merge of related
  ``side-effects`` findings after the three tail passes (wraps
  ``side_effect_consolidation.consolidate_side_effect_issues``; gated by
  ``CODE_REVIEW_SIDE_EFFECT_CONSOLIDATION``, fail-safe on error).
- :func:`combine_findings_activity` — pure (no-LLM) combine of near-duplicate /
  co-located findings across the whole stream before the false-positive filter
  (wraps ``finding_combination.combine_findings``; deterministic, fail-safe on
  error). Generalizes ``consolidate_side_effect_issues_activity`` to every
  category.
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


def _repo_reader_from_input(input_data: Any) -> Optional[Any]:
    """Rebuild a whole-repo reader inside a worker activity, fail-safe.

    A live ``RepoReader`` cannot cross the Temporal serialization boundary, so
    the review carries only ``CodeReviewInput.repo_root`` — a disk checkout path
    that survives ``model_dump(mode="json")``. This reconstructs a
    ``DiskRepoReader`` from it worker-side so the false-positive and
    architecture/redundancy passes regain the off-diff read access they have in
    thread mode.

    Postconditions:
        - Returns a ``DiskRepoReader`` when ``input_data.repo_root`` is a non-blank
          path; returns ``None`` when it is unset/blank, when the path is not
          present on this worker, or the reader cannot be built. A ``None`` reader
          is the pre-existing conservative behavior: the passes then keep more
          findings (fail-safe), never fewer. Never raises.
    """
    from ..repo_reader import disk_repo_reader_from_root

    return disk_repo_reader_from_root(getattr(input_data, "repo_root", None))


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
          ``surface_by_path`` fingerprints, ``single_chunk``, and the review's
          own adaptive map-phase ``fanout_width``
          (``config.resolve_temporal_fanout_width(len(chunks))``).
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

    from ..architecture_context import render_architecture_context
    from ..chunking import _blocks_from_input, build_review_chunks
    from ..mapping import _context_fingerprint, _review_model_fingerprint, _surface_by_path
    from ..models import CodeReviewInput, CodeReviewIssue
    from .config import resolve_temporal_fanout_width
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
            render_architecture_context(input_data.architecture),
            max_arch,
            llm,
            "architecture overview",
        )[:max_arch]
    existing_codebase = compact_text(
        input_data.existing_codebase or "", max_existing, llm, "existing codebase"
    )[:max_existing]

    chunks = build_review_chunks(
        blocks, compute_code_review_map_chunk_chars(llm), input_data.pre_numbered
    )
    fanout_width = resolve_temporal_fanout_width(len(chunks))

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
        fanout_width=fanout_width,
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
    from shared.concurrency import BackgroundHeartbeat

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
          failure keeps the findings). When ``review_input.repo_root`` names a
          disk checkout reachable by this worker, a ``DiskRepoReader`` is rebuilt
          from it so out-of-diff "missing file" confirmations run just as they do
          in thread mode; otherwise the reader is ``None`` and those confirmations
          are skipped — a strictly more conservative (keep-more) behavior.
    """
    from ..coordinator import _dedupe_issues
    from ..false_positive_filter import filter_false_positives
    from ..models import CodeReviewInput, CodeReviewIssue

    input_data = CodeReviewInput.model_validate(review_input)
    genuine = _dedupe_issues([CodeReviewIssue.model_validate(i) for i in issues])
    if skip:
        return [i.model_dump(mode="json") for i in genuine]

    llm = _resolve_llm()
    verified = filter_false_positives(
        llm, input_data, genuine, repo_reader=_repo_reader_from_input(input_data)
    )
    return [i.model_dump(mode="json") for i in verified]


@activity.defn(name="code_review_architecture_consistency")
def find_architecture_and_redundancy_activity(
    review_input: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Once-per-submission architecture-consistency / redundancy pass.

    Preconditions:
        - ``review_input`` is a ``CodeReviewInput.model_dump(mode="json")`` dict
          (the same one every other activity in this module reconstructs from).

    Postconditions:
        - Returns zero or more NEW ``CodeReviewIssue`` dicts (category
          ``"architecture"`` or ``"refactor"``); never mutates or removes any
          finding from the caller's perspective — this activity is purely
          additive, mirroring ``find_architecture_and_redundancy_issues``'s own
          contract. When ``review_input.repo_root`` names a disk checkout
          reachable by this worker, a ``DiskRepoReader`` is rebuilt from it so the
          cross-codebase-redundancy check can search the whole repository as it
          does in thread mode; otherwise the reader is ``None`` and this pass
          confirms redundancy only within the submission's own files plus the
          ``existing_codebase`` excerpt.
        - Never raises: the wrapped function is itself fail-safe (disabled via
          env, no architecture document, or any setup/LLM failure all degrade
          to an empty list) -- and so is this activity as a whole, including
          ``CodeReviewInput.model_validate`` and ``_resolve_llm()``.
          Reconstructing the input and resolving the LLM client both happen
          BEFORE the wrapped function's own env/profile early-return checks
          run, so without this activity's own try/except a validation or
          client-resolution failure (e.g. malformed payload, no LLM provider
          configured) would raise even when this optional, additive pass
          would have no-op'd anyway -- turning an inapplicable pass into a
          failure of the whole durable review. An activity failure here would
          only ever be an unexpected defect, not an expected outcome.
    """
    from ..architecture_consistency_pass import find_architecture_and_redundancy_issues
    from ..models import CodeReviewInput

    try:
        input_data = CodeReviewInput.model_validate(review_input)
        llm = _resolve_llm()
        findings = find_architecture_and_redundancy_issues(
            llm, input_data, repo_reader=_repo_reader_from_input(input_data)
        )
    except Exception as exc:  # noqa: BLE001 - fail-safe: this pass must never break the review
        logger.warning(
            "ArchitectureConsistencyPass: activity failed (%s: %s); returning no additional findings",
            type(exc).__name__,
            exc,
        )
        return []
    return [i.model_dump(mode="json") for i in findings]


@activity.defn(name="code_review_side_effect_impact")
def find_side_effect_impact_activity(
    review_input: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Once-per-submission side-effect / blast-radius pass.

    Preconditions:
        - ``review_input`` is a ``CodeReviewInput.model_dump(mode="json")`` dict
          (the same one every other activity in this module reconstructs from).

    Postconditions:
        - Returns zero or more NEW ``CodeReviewIssue`` dicts (category
          ``"side-effects"`` for a caller-breaking side effect, or
          ``"documentation"`` for a docstring/implementation mismatch); never
          mutates or removes any finding from the
          caller's perspective — this activity is purely additive, mirroring
          ``find_side_effect_impact_issues``'s own contract. When
          ``review_input.repo_root`` names a disk checkout reachable by this
          worker, a ``DiskRepoReader`` is rebuilt from it (via
          ``_repo_reader_from_input``, the same helper the false-positive and
          architecture activities use) so ``search_repository`` can find
          out-of-diff callers exactly as it does in thread mode; otherwise the
          reader is ``None`` and this pass can only see callers within the
          submission's own files plus the ``existing_codebase`` excerpt.
        - A live ``GitHubRepoReader`` (the PR-review flow) cannot cross this
          boundary at all — it holds a per-request auth token, not a
          reconstructible field — so that caller forces the in-process
          coordinator instead of Temporal dispatch whenever it supplies a
          reader (``coding_engine_provider.py``); this activity is then simply
          never invoked for that review, and ``find_side_effect_impact_issues``
          receives the live reader directly.
        - Never raises: the wrapped function is itself fail-safe (disabled via
          env, wrong profile, or any setup/LLM failure all degrade to an empty
          list) -- and so is this activity as a whole, including
          ``CodeReviewInput.model_validate`` and ``_resolve_llm()``.
          Reconstructing the input and resolving the LLM client both happen
          BEFORE the wrapped function's own env/profile/``pre_numbered``
          early-return checks run, so without this activity's own try/except a
          validation or client-resolution failure (e.g. malformed payload, no
          LLM provider configured) would raise even when this optional,
          additive pass would have no-op'd anyway
          (``CODE_REVIEW_SIDE_EFFECT_IMPACT_PASS=false``, a non-default
          profile, or hunk-mode input) -- turning an inapplicable pass into a
          failure of the whole durable review. An activity failure here would
          only ever be an unexpected defect, not an expected outcome.
    """
    from ..models import CodeReviewInput
    from ..side_effect_impact_pass import find_side_effect_impact_issues

    try:
        input_data = CodeReviewInput.model_validate(review_input)
        llm = _resolve_llm()
        findings = find_side_effect_impact_issues(
            llm, input_data, repo_reader=_repo_reader_from_input(input_data)
        )
    except Exception as exc:  # noqa: BLE001 - fail-safe: this pass must never break the review
        logger.warning(
            "SideEffectImpactPass: activity failed (%s: %s); returning no additional findings",
            type(exc).__name__,
            exc,
        )
        return []
    return [i.model_dump(mode="json") for i in findings]


@activity.defn(name="code_review_merged_architecture_side_effect")
def find_architecture_and_side_effect_activity(
    review_input: Dict[str, Any],
) -> Dict[str, List[Dict[str, Any]]]:
    """Once-per-submission merged architecture-consistency + side-effect pass.

    Preconditions:
        - ``review_input`` is a ``CodeReviewInput.model_dump(mode="json")`` dict
          (the same one every other activity in this module reconstructs from).

    Postconditions:
        - Returns ``{"architecture_findings": [...], "side_effect_findings": [...]}``,
          each a list of zero or more NEW ``CodeReviewIssue`` dicts in the same
          shapes :func:`find_architecture_and_redundancy_activity` /
          :func:`find_side_effect_impact_activity` return; never mutates or
          removes any finding from the caller's perspective — this activity is
          purely additive, mirroring
          ``find_architecture_and_side_effect_issues``'s own contract (one LLM
          call covering both checks instead of two). When
          ``review_input.repo_root`` names a disk checkout reachable by this
          worker, a ``DiskRepoReader`` is rebuilt from it (via
          ``_repo_reader_from_input``, the same helper the other tail-pass
          activities use) so both halves regain the off-diff read access they
          have in thread mode; otherwise the reader is ``None`` and each half
          degrades exactly as its standalone counterpart does.
        - Never raises: the wrapped function is itself fail-safe (disabled via
          env, wrong profile, or any setup/LLM failure all degrade to
          ``([], [])``) -- and so is this activity as a whole, including
          ``CodeReviewInput.model_validate`` and ``_resolve_llm()``.
          Reconstructing the input and resolving the LLM client both happen
          BEFORE the wrapped function's own env/profile early-return checks
          run, so without this activity's own try/except a validation or
          client-resolution failure (e.g. malformed payload, no LLM provider
          configured) would raise even when this optional, additive pass
          would have no-op'd anyway -- turning an inapplicable pass into a
          failure of the whole durable review. An activity failure here would
          only ever be an unexpected defect, not an expected outcome.
    """
    from ..merged_architecture_side_effect_pass import find_architecture_and_side_effect_issues
    from ..models import CodeReviewInput

    try:
        input_data = CodeReviewInput.model_validate(review_input)
        llm = _resolve_llm()
        architecture_findings, side_effect_findings = find_architecture_and_side_effect_issues(
            llm, input_data, repo_reader=_repo_reader_from_input(input_data)
        )
    except Exception as exc:  # noqa: BLE001 - fail-safe: this pass must never break the review
        logger.warning(
            "MergedArchitectureSideEffectPass: activity failed (%s: %s); "
            "returning no additional findings",
            type(exc).__name__,
            exc,
        )
        return {"architecture_findings": [], "side_effect_findings": []}
    return {
        "architecture_findings": [i.model_dump(mode="json") for i in architecture_findings],
        "side_effect_findings": [i.model_dump(mode="json") for i in side_effect_findings],
    }


@activity.defn(name="code_review_side_effect_consolidation")
def consolidate_side_effect_issues_activity(
    review_input: Dict[str, Any],
    issues: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Merge related ``side-effects`` findings before the final dedupe/gate.

    Called after the false-positive, architecture, and side-effect tail passes
    have contributed their findings, and before ``finalize_review_activity``
    applies the exact-match dedupe and approval reconciliation.

    Preconditions:
        - ``review_input`` is a ``CodeReviewInput.model_dump(mode="json")`` dict.
        - ``issues`` is the post-tail-pass issue list (each a
          ``CodeReviewIssue.model_dump(mode="json")`` dict), already including
          any architecture / side-effect additions.

    Postconditions:
        - When ``CODE_REVIEW_SIDE_EFFECT_CONSOLIDATION`` is disabled, returns
          ``issues`` unchanged (same length and content).
        - When enabled, returns the consolidated issue list from
          ``consolidate_side_effect_issues`` (related ``side-effects`` findings
          merged; every other category passes through untouched), serialized as
          ``model_dump(mode="json")`` dicts.
        - Never raises: any reconstruction / index / consolidation failure
          logs a warning and returns the original ``issues`` unchanged
          (fail-safe), matching the in-process coordinator's try/except around
          the same step.
    """
    from shared.env import env_flag_enabled

    from ..false_positive_filter import CodebaseIndex
    from ..models import CodeReviewInput, CodeReviewIssue
    from ..side_effect_consolidation import (
        SIDE_EFFECT_CONSOLIDATION_ENV,
        consolidate_side_effect_issues,
    )

    if not env_flag_enabled(SIDE_EFFECT_CONSOLIDATION_ENV):
        return issues

    try:
        input_data = CodeReviewInput.model_validate(review_input)
        parsed_issues = [CodeReviewIssue.model_validate(i) for i in issues]
        index = CodebaseIndex.from_input(
            input_data, repo_reader=_repo_reader_from_input(input_data)
        )
        consolidated = consolidate_side_effect_issues(parsed_issues, index)
    except Exception as exc:  # noqa: BLE001 - fail-safe: keep unconsolidated issues
        logger.warning(
            "SideEffectConsolidation: activity failed (%s: %s); using unconsolidated issues",
            type(exc).__name__,
            exc,
        )
        return issues
    return [i.model_dump(mode="json") for i in consolidated]


@activity.defn(name="code_review_combine_findings")
def combine_findings_activity(
    review_input: Dict[str, Any],
    issues: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Combine near-duplicate / co-located findings across the whole stream.

    The durable, no-LLM counterpart of the thread-mode combine step
    (``coordinator._run_tail_passes``): a category-agnostic, deterministic merge
    of near-duplicate (same file + same category + similar description) and
    co-located (same enclosing Python construct) findings, run over the full
    map-phase + additive tail-pass stream before the false-positive filter. It
    subsumes both the exact-match dedupe and the older
    :func:`consolidate_side_effect_issues_activity` (an exact duplicate is just a
    same-file, same-category, Jaccard-1.0 similarity match).

    Preconditions:
        - ``review_input`` is a ``CodeReviewInput.model_dump(mode="json")`` dict.
        - ``issues`` is the merged finding list (each a
          ``CodeReviewIssue.model_dump(mode="json")`` dict), already including any
          architecture / side-effect tail-pass additions.

    Postconditions:
        - Returns the combined issue list from ``combine_findings`` — related
          findings merged into one representative (``severity`` = group max, line
          span on the majority file, exact-deduped descriptions/suggestions,
          ``pre_existing`` = AND across the group), every unrelated finding passed
          through in its original relative position — serialized as
          ``model_dump(mode="json")`` dicts. ``combine_findings`` is itself
          deterministic (see its module Invariants), so the same inputs always
          yield the same output.
        - Mirrors the thread-mode call by passing
          ``consolidate_side_effects=env_flag_enabled(SIDE_EFFECT_CONSOLIDATION_ENV)``
          so the ``CODE_REVIEW_SIDE_EFFECT_CONSOLIDATION`` escape hatch behaves
          identically in Temporal mode.
        - Never raises: any reconstruction / index / combination failure logs a
          warning and returns the original ``issues`` unchanged (fail-safe),
          matching the in-process coordinator's try/except around the same step —
          the subsequent finalize gate still applies the exact-match dedupe, so a
          fail-safe passthrough only ever keeps more findings, never fewer.
    """
    from shared.env import env_flag_enabled

    from ..false_positive_filter import CodebaseIndex
    from ..finding_combination import combine_findings
    from ..models import CodeReviewInput, CodeReviewIssue
    from ..side_effect_consolidation import SIDE_EFFECT_CONSOLIDATION_ENV

    try:
        input_data = CodeReviewInput.model_validate(review_input)
        parsed_issues = [CodeReviewIssue.model_validate(i) for i in issues]
        index = CodebaseIndex.from_input(
            input_data, repo_reader=_repo_reader_from_input(input_data)
        )
        combined = combine_findings(
            parsed_issues,
            index,
            consolidate_side_effects=env_flag_enabled(SIDE_EFFECT_CONSOLIDATION_ENV),
        )
    except Exception as exc:  # noqa: BLE001 - fail-safe: keep uncombined issues
        logger.warning(
            "FindingCombination: activity failed (%s: %s); using uncombined issues",
            type(exc).__name__,
            exc,
        )
        return issues
    return [i.model_dump(mode="json") for i in combined]


@activity.defn(name="code_review_finalize")
def finalize_review_activity(
    verified_issues: List[Dict[str, Any]],
    not_reviewed_issues: List[Dict[str, Any]],
    skipped_issues: List[Dict[str, Any]],
    approved_flags: List[bool],
) -> Dict[str, Any]:
    """Deterministic reduce gate: dedupe, cap, and reconcile approval.

    Preconditions:
        - ``verified_issues`` are the post-false-positive genuine findings,
          ``not_reviewed_issues`` the coverage findings, ``skipped_issues`` the
          empty-file info findings, and ``approved_flags`` one bool per successful
          sub-review.
        - ``approved_flags`` is non-empty: the workflow's total-failure guard
          (``CodeReviewWorkflow.run`` in ``workflows.py``) raises before this
          activity is ever invoked when no chunk produced a verdict.

    Postconditions:
        - Returns ``{"approved": bool, "issues": [issue dicts]}`` computed by
          ``coordinator._dedupe_issues`` over the merged findings,
          ``coordinator._cap_issues`` (severity-first, at most
          ``MAX_CODE_REVIEW_ISSUES``), and ``coordinator._reconcile_approval``
          with the anti-loop safety nets — the identical deterministic gate
          ``run_coordinator`` applies, so the verdict matches thread mode.
    """
    from ..coordinator import _cap_issues, _dedupe_issues, _reconcile_approval
    from ..models import CodeReviewIssue

    verified = [CodeReviewIssue.model_validate(i) for i in verified_issues]
    not_reviewed = [CodeReviewIssue.model_validate(i) for i in not_reviewed_issues]
    skipped = [CodeReviewIssue.model_validate(i) for i in skipped_issues]

    deduped = _dedupe_issues([*verified, *not_reviewed, *skipped])
    deduped = _cap_issues(deduped)
    assert approved_flags, "unreachable: workflow raises before calling this activity when empty"
    all_llm_approved = all(approved_flags)
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
        - Never raises: this activity as a whole is fail-safe, including
          ``CodeReviewInput.model_validate`` and ``_resolve_llm()``.
          Reconstructing the input and resolving the LLM client happen before
          (and outside) the wrapped function's own failure handling, so without
          this activity's own try/except a validation or client-resolution
          failure (e.g. malformed payload, no LLM provider configured) would
          raise instead of returning ``None`` and letting the workflow fall
          back to deterministic concatenation. An activity failure here would
          only ever be an unexpected defect, not an expected outcome.
    """
    from ..models import CodeReviewInput, CodeReviewIssue
    from ..synthesis import synthesize_review_findings

    try:
        input_data = CodeReviewInput.model_validate(review_input)
        result = synthesize_review_findings(
            _resolve_llm(),
            input_data=input_data,
            approved=approved,
            issues=[CodeReviewIssue.model_validate(i) for i in issues],
            chunk_summaries=chunk_summaries,
            chunk_spec_notes=chunk_spec_notes,
        )
    except Exception as exc:  # noqa: BLE001 - fail-safe: synthesis must never break the review
        logger.warning(
            "SynthesizeFindings: activity failed (%s: %s); returning None",
            type(exc).__name__,
            exc,
        )
        return None
    if result is None:
        return None
    return {"summary": result.summary, "spec_compliance_notes": result.spec_compliance_notes}
