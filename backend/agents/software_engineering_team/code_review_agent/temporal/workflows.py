"""Durable code review workflow: map-reduce review as a Temporal workflow.

``CodeReviewWorkflow`` reproduces ``coordinator.run_coordinator`` as a durable,
resumable computation. It orchestrates the review as a sequence of activities —
prepare → map fan-out → false-positive verify → architecture-consistency /
redundancy pass → side-effect / blast-radius pass → deterministic gate →
(conditional) narrative synthesis — so a worker restart mid-review re-runs only
the unfinished activities instead of re-reviewing the whole submission.

Phases call the same underlying coordinator functions (through
:mod:`.activities`): the map unit is ``mapping._cached_review_chunk``,
verification is ``false_positive_filter.filter_false_positives``, the additive
architecture pass is
``architecture_consistency_pass.find_architecture_and_redundancy_issues``, the
additive side-effect pass is
``side_effect_impact_pass.find_side_effect_impact_issues``, the gate is
``coordinator._dedupe_issues`` + ``_reconcile_approval``, and the narrative is
``synthesis.synthesize_review_findings`` with the same deterministic-concat
fallback. One intentional divergence from thread mode: the durable reduce always
folds ``not_reviewed_issues`` into the approval gate as blocking findings and
does not return ``not_reviewed_ranges``, whereas ``run_coordinator`` only blocks
on unreviewed chunks when ``CODE_REVIEW_BLOCK_ON_UNREVIEWED`` is set (see
``agent.py``'s dispatch-mode note and ``activities.finalize_review_activity``).

Sandbox note: activity and constant imports are wrapped in
``workflow.unsafe.imports_passed_through()``; the workflow body itself performs
no I/O, time, or randomness — only ``execute_activity`` calls and pure list
aggregation over JSON-native dicts.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any, Dict, List

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError

with workflow.unsafe.imports_passed_through():
    from software_engineering_team.code_review_agent.temporal import activities as A
    from software_engineering_team.code_review_agent.temporal.config import TASK_QUEUE

# Bounded exponential retry for the deterministic/cheap phases. The review is
# fail-closed, so a phase that keeps failing surfaces rather than being masked.
_DEFAULT_RETRY = RetryPolicy(
    maximum_attempts=3,
    initial_interval=timedelta(seconds=10),
    maximum_interval=timedelta(minutes=1),
    backoff_coefficient=2.0,
)

# The map call and false-positive verify make LLM calls; give them the same
# bounded retry but a longer per-attempt ceiling.
_LLM_RETRY = RetryPolicy(
    maximum_attempts=3,
    initial_interval=timedelta(seconds=15),
    maximum_interval=timedelta(minutes=2),
    backoff_coefficient=2.0,
)

# Marker type carried on the ``ApplicationError`` the total-failure guard raises,
# so the sync dispatcher can translate it back into ``CodeReviewUnavailableError``.
CODE_REVIEW_UNAVAILABLE_TYPE = "CodeReviewUnavailableError"

# Replay-compatibility gate for inserting the architecture-consistency activity
# between verification and finalization (see ``run``). A ``CodeReviewWorkflow``
# history recorded before this activity existed has no marker for it, so
# ``workflow.patched`` returns False on replay and that history's original
# finalize-next sequence is reproduced exactly; a new execution records the
# marker and always takes the new path. Mirrors ``planning_team.temporal.
# workflows._PER_PHASE_PATCH``'s identical rationale.
# TODO: Remove this gate (and always run the architecture pass unconditionally)
# once no pre-migration CodeReviewWorkflow histories remain open (confirm via
# the Temporal UI), then deprecate the marker with
# ``workflow.deprecate_patch(_ARCHITECTURE_PASS_PATCH)`` before deleting it.
_ARCHITECTURE_PASS_PATCH = "code-review-architecture-consistency-pass"

# Replay-compatibility gate for inserting the side-effect / blast-radius activity
# between the architecture pass and finalization (see ``run``). Same rationale as
# ``_ARCHITECTURE_PASS_PATCH``: a history recorded before this activity existed has
# no marker for it, so ``workflow.patched`` returns False on replay and that
# history's original finalize-next sequence is reproduced exactly.
# TODO: Remove this gate (and always run the side-effect pass unconditionally)
# once no pre-migration CodeReviewWorkflow histories remain open (confirm via
# the Temporal UI), then deprecate the marker with
# ``workflow.deprecate_patch(_SIDE_EFFECT_PASS_PATCH)`` before deleting it.
_SIDE_EFFECT_PASS_PATCH = "code-review-side-effect-impact-pass"

# Replay-compatibility gate for bounding the map-phase fan-out by
# ``prep["fanout_width"]`` (see ``run``) instead of scheduling every chunk's
# activity unconditionally. Same rationale as ``_ARCHITECTURE_PASS_PATCH``: a
# history recorded before this existed scheduled every chunk in one batch, so
# ``workflow.patched`` returns False on replay and that history's original
# unconstrained fan-out is reproduced exactly; a new execution records the
# marker and always takes the new, capacity-bounded path.
# TODO: Remove this gate (and always bound the fan-out unconditionally) once
# no pre-migration CodeReviewWorkflow histories remain open (confirm via the
# Temporal UI), then deprecate the marker with
# ``workflow.deprecate_patch(_ADAPTIVE_FANOUT_PATCH)`` before deleting it.
_ADAPTIVE_FANOUT_PATCH = "code-review-adaptive-fanout-width"

# Replay-compatibility gate for scheduling the false-positive-verification,
# architecture-consistency, and side-effect-impact tail-pass activities
# concurrently via ``asyncio.gather`` (see ``run``) instead of one at a time.
# Same rationale as ``_ARCHITECTURE_PASS_PATCH``: a history recorded before
# this existed scheduled the tail passes sequentially, each in its own
# workflow task, so ``workflow.patched`` returns False on replay and that
# history's original sequential command sequence is reproduced exactly; a new
# execution records the marker and always takes the new, concurrent path.
# TODO: Remove this gate (and always schedule the tail passes concurrently)
# once no pre-migration CodeReviewWorkflow histories remain open (confirm via
# the Temporal UI), then deprecate the marker with
# ``workflow.deprecate_patch(_CONCURRENT_TAIL_PASSES_PATCH)`` before deleting it.
_CONCURRENT_TAIL_PASSES_PATCH = "code-review-concurrent-tail-passes"


@workflow.defn(name="CodeReviewWorkflow")
class CodeReviewWorkflow:
    """Durable orchestration of the map-reduce code review.

    Invariants:
        - Every submitted line is reviewed or named by a blocking ``high`` "not
          reviewed" finding; ``approved is False`` implies a critical/high issue.
        - The workflow renders no verdict on a submission no chunk could review:
          the total-failure guard raises instead (mapped to
          ``CodeReviewUnavailableError`` by the caller).
    """

    def __init__(self) -> None:
        # Progress is exposed via the ``progress`` query and cancellation via the
        # ``cancel`` signal; neither is required for a review to complete.
        self._phase: str = "starting"
        self._fraction: float = 0.0
        self._cancel_requested: bool = False

    @workflow.signal
    def cancel(self) -> None:
        """Request cooperative cancellation of the review.

        The map phase checks this flag between the prepare and fan-out steps, so a
        cancel that arrives before the (expensive) fan-out short-circuits the run.
        """
        self._cancel_requested = True

    @workflow.query
    def progress(self) -> Dict[str, Any]:
        """Return the current ``{phase, fraction, cancel_requested}`` snapshot.

        The durable, queryable analogue of the in-process
        ``ReviewProgressCallback``: external observers poll this instead of being
        handed a callback.
        """
        return {
            "phase": self._phase,
            "fraction": self._fraction,
            "cancel_requested": self._cancel_requested,
        }

    def _advance(self, phase: str, fraction: float) -> None:
        self._phase = phase
        self._fraction = fraction

    @workflow.run
    async def run(self, review_input: Dict[str, Any]) -> Dict[str, Any]:
        """Review ``review_input`` and return a ``CodeReviewOutput`` dict.

        Preconditions:
            - ``review_input`` is a ``CodeReviewInput.model_dump(mode="json")`` dict.

        Postconditions:
            - Returns a ``CodeReviewOutput`` dict produced by the same
              deterministic gate and narrative fallback as ``run_coordinator``,
              except for the intentional Temporal-dispatch difference documented
              in ``agent.py``: unreviewable chunks are always merged as blocking
              findings (``finalize_review_activity`` does not honor
              ``CODE_REVIEW_BLOCK_ON_UNREVIEWED``), and ``not_reviewed_ranges`` is
              omitted from the returned dict. With default env settings, the same
              input can therefore yield ``approved=False`` here while
              ``run_coordinator`` returns ``approved=True``.
        """
        self._advance("preparing", 0.05)
        prep = await workflow.execute_activity(
            A.prepare_review_activity,
            args=[review_input],
            task_queue=TASK_QUEUE,
            start_to_close_timeout=timedelta(minutes=30),
            retry_policy=_DEFAULT_RETRY,
        )
        if prep["no_code"]:
            self._advance("done", 1.0)
            return {
                "approved": True,
                "issues": prep["skipped_issues"],
                "summary": "No code to review.",
                "spec_compliance_notes": "",
            }

        if self._cancel_requested:
            raise ApplicationError(
                "Code review cancelled before the map phase.",
                type=CODE_REVIEW_UNAVAILABLE_TYPE,
                non_retryable=True,
            )

        chunks: List[Dict[str, Any]] = prep["chunks"]
        base_input: Dict[str, Any] = prep["base_input"]
        context_fp: str = prep["context_fp"]
        surface_by_path: Dict[str, List[str]] = prep["surface_by_path"]
        fanout_width: int = prep.get("fanout_width", 1) or 1

        self._advance("reviewing", 0.10)

        def _review_one(chunk: Dict[str, Any]) -> Any:
            return workflow.execute_activity(
                A.review_chunk_activity,
                args=[chunk, base_input, context_fp, surface_by_path],
                task_queue=TASK_QUEUE,
                start_to_close_timeout=timedelta(hours=1),
                heartbeat_timeout=timedelta(minutes=5),
                retry_policy=_LLM_RETRY,
            )

        if workflow.patched(_ADAPTIVE_FANOUT_PATCH):
            # Fan out: one durable activity per chunk, at most `fanout_width`
            # in flight at once for THIS review — this review's own adaptive
            # width (chunk count clamped by the worker's validated activity
            # capacity; see config.resolve_temporal_fanout_width), so a small
            # review never over-requests and a large review can never exceed
            # the worker's provisioned capacity. A worker restart re-runs only
            # the chunks that had not completed.
            semaphore = asyncio.Semaphore(fanout_width)

            async def _review_one_bounded(chunk: Dict[str, Any]) -> Any:
                async with semaphore:
                    return await _review_one(chunk)

            outcomes = await asyncio.gather(*[_review_one_bounded(chunk) for chunk in chunks])
        else:
            # Pre-migration history: reproduce the original unconstrained
            # fan-out exactly (see _ADAPTIVE_FANOUT_PATCH).
            outcomes = await asyncio.gather(*[_review_one(chunk) for chunk in chunks])

        issues: List[Dict[str, Any]] = []
        not_reviewed: List[Dict[str, Any]] = []
        summaries: List[str] = []
        spec_notes: List[str] = []
        approved_flags: List[bool] = []
        for outcome in outcomes:
            issues.extend(outcome["issues"])
            not_reviewed.extend(outcome["not_reviewed_issues"])
            summaries.extend(outcome["summaries"])
            spec_notes.extend(outcome["spec_notes"])
            approved_flags.extend(outcome["approved_flags"])

        # Total-failure guard: no chunk produced a verdict means the run reviewed
        # nothing, so it must fail loudly rather than render a verdict on unseen code.
        if not approved_flags:
            raise ApplicationError(
                "No chunk could be reviewed after recovery; "
                "no verdict was produced for this submission.",
                type=CODE_REVIEW_UNAVAILABLE_TYPE,
                non_retryable=True,
            )

        self._advance("verifying", 0.92)

        def _verify() -> Any:
            return workflow.execute_activity(
                A.filter_false_positives_activity,
                args=[
                    review_input,
                    issues,
                    bool(review_input.get("skip_false_positive_filter", False)),
                ],
                task_queue=TASK_QUEUE,
                start_to_close_timeout=timedelta(minutes=30),
                retry_policy=_LLM_RETRY,
            )

        def _architecture() -> Any:
            return workflow.execute_activity(
                A.find_architecture_and_redundancy_activity,
                args=[review_input],
                task_queue=TASK_QUEUE,
                start_to_close_timeout=timedelta(minutes=30),
                retry_policy=_LLM_RETRY,
            )

        def _side_effect() -> Any:
            return workflow.execute_activity(
                A.find_side_effect_impact_activity,
                args=[review_input],
                task_queue=TASK_QUEUE,
                start_to_close_timeout=timedelta(minutes=30),
                retry_policy=_LLM_RETRY,
            )

        # Architecture-consistency / cross-codebase-redundancy pass and
        # side-effect / blast-radius pass are both additive, once per
        # submission (not once per chunk), matching thread mode's
        # run_coordinator (see coordinator.py's identical call ordering —
        # after false-positive verification, before the final dedupe/gate).
        # Each is gated by its own workflow.patched so a pre-migration
        # history (recorded before that activity existed) replays its
        # original finalize-next sequence exactly.
        has_architecture_findings = False
        has_side_effect_findings = False

        if workflow.patched(_CONCURRENT_TAIL_PASSES_PATCH):
            # None of the three tail passes reads another's output (see
            # coordinator._run_tail_passes), so they can be scheduled as
            # concurrent activities instead of three sequential round-trips —
            # same "create the coroutine, gather later" idiom as the map
            # fan-out above. Safe to evaluate _ARCHITECTURE_PASS_PATCH /
            # _SIDE_EFFECT_PASS_PATCH up front here: this branch is only
            # reached by a brand-new execution or one that already recorded
            # the _CONCURRENT_TAIL_PASSES_PATCH marker (and therefore already
            # evaluated those two markers at these same positions).
            run_architecture = workflow.patched(_ARCHITECTURE_PASS_PATCH)
            run_side_effect = workflow.patched(_SIDE_EFFECT_PASS_PATCH)

            calls = [_verify()]
            if run_architecture:
                calls.append(_architecture())
            if run_side_effect:
                calls.append(_side_effect())
            # return_exceptions=True: wait for every tail pass to finish
            # (success or failure) instead of asyncio.gather's default of
            # raising as soon as the first exception surfaces and leaving
            # the others un-awaited -- that default would leave a sibling
            # activity un-awaited from the workflow's point of view (an
            # orphaned command Temporal's workflow sandbox does not
            # tolerate) and pick whichever exception happens to resolve
            # first in real time, which is not guaranteed to match on
            # replay. Extract each call's own result into its own local
            # (in `calls`' fixed verify/architecture/side-effect order)
            # BEFORE checking for exceptions, so a failed slot's exception
            # object is read once as itself and never mistaken for (and
            # spread as) a findings list; only once every local holds its
            # plain result value do we re-raise the first exception found,
            # in that same fixed order -- reproducing sequential
            # execution's deterministic error precedence (verify's failure,
            # if any, always wins) and total-failure semantics (a tail-pass
            # failure aborts the whole review before any result is used,
            # exactly as it would if the later passes were never reached).
            results = await asyncio.gather(*calls, return_exceptions=True)
            results_iter = iter(results)
            verify_result = next(results_iter)
            architecture_result = next(results_iter) if run_architecture else None
            side_effect_result = next(results_iter) if run_side_effect else None

            first_exception = next((r for r in results if isinstance(r, BaseException)), None)
            if first_exception is not None:
                raise first_exception

            verified = verify_result
            if run_architecture and architecture_result:
                verified = [*verified, *architecture_result]
                has_architecture_findings = True
            if run_side_effect and side_effect_result:
                verified = [*verified, *side_effect_result]
                has_side_effect_findings = True
        else:
            # Pre-migration history: reproduce the original sequential
            # scheduling AND the original workflow.patched call positions
            # exactly (see _CONCURRENT_TAIL_PASSES_PATCH) — each patched()
            # call must stay at its original await boundary (after the
            # activity it follows completes), since a history recorded
            # under the old code has the corresponding marker event there,
            # not up front alongside the other two.
            verified = await _verify()
            if workflow.patched(_ARCHITECTURE_PASS_PATCH):
                architecture_findings = await _architecture()
                if architecture_findings:
                    verified = [*verified, *architecture_findings]
                    has_architecture_findings = True
            if workflow.patched(_SIDE_EFFECT_PASS_PATCH):
                side_effect_findings = await _side_effect()
                if side_effect_findings:
                    verified = [*verified, *side_effect_findings]
                    has_side_effect_findings = True

        self._advance("finalizing", 0.95)
        gate = await workflow.execute_activity(
            A.finalize_review_activity,
            args=[verified, not_reviewed, prep["skipped_issues"], approved_flags],
            task_queue=TASK_QUEUE,
            start_to_close_timeout=timedelta(minutes=5),
            retry_policy=_DEFAULT_RETRY,
        )
        approved: bool = gate["approved"]
        gated_issues: List[Dict[str, Any]] = gate["issues"]

        summary, notes = await self._narrative(
            review_input,
            approved,
            gated_issues,
            summaries,
            spec_notes,
            has_architecture_findings or has_side_effect_findings,
        )

        self._advance("done", 1.0)
        return {
            "approved": approved,
            "issues": gated_issues,
            "summary": summary,
            "spec_compliance_notes": notes,
        }

    async def _narrative(
        self,
        review_input: Dict[str, Any],
        approved: bool,
        issues: List[Dict[str, Any]],
        summaries: List[str],
        spec_notes: List[str],
        has_additive_pass_findings: bool = False,
    ) -> tuple[str, str]:
        """Produce the merged ``(summary, spec_compliance_notes)``.

        Mirrors ``coordinator._merge_narrative``: a single sub-review with no
        additive-pass findings (architecture/redundancy or side-effect/blast-radius)
        is used verbatim (no synthesis call); otherwise attempts a synthesis
        activity and falls back to deterministic concatenation on failure, so a
        blocking additive-pass finding is never silently absent from the
        narrative attached to a single-chunk review.
        """
        if len(summaries) == 1 and not has_additive_pass_findings:
            return summaries[0], (spec_notes[0] if spec_notes else "")

        synth = await workflow.execute_activity(
            A.synthesize_findings_activity,
            args=[review_input, approved, issues, summaries, spec_notes],
            task_queue=TASK_QUEUE,
            start_to_close_timeout=timedelta(minutes=15),
            retry_policy=_DEFAULT_RETRY,
        )
        if synth is not None:
            return synth["summary"], synth["spec_compliance_notes"]
        concatenated_summary = "\n\n".join(s for s in summaries if s.strip())
        concatenated_notes = "\n\n".join(n for n in spec_notes if n.strip())
        return concatenated_summary, concatenated_notes
