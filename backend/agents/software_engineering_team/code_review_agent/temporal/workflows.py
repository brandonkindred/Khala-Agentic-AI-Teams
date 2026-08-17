"""Durable code review workflow: map-reduce review as a Temporal workflow.

``CodeReviewWorkflow`` reproduces ``coordinator.run_coordinator`` as a durable,
resumable computation. It orchestrates the review as: prepare → map fan-out
(under ``_ADAPTIVE_FANOUT_PATCH``: chunk activities gathered with
``asyncio.gather(..., return_exceptions=True)``, then a fixed-order scan
re-raises the earliest-index exception; pre-migration histories keep the
original unconstrained ``asyncio.gather`` without ``return_exceptions``) →
tail passes run through a concurrent verify+merged gather (see below) →
optional side-effect consolidation → (under
``_REORDERED_TAIL_PASSES_PATCH``) a sequential combine + re-verify pass →
deterministic gate → (conditional) narrative synthesis — so a worker restart
mid-review re-runs only the unfinished activities instead of re-reviewing the
whole submission. New concurrent fan-outs await every sibling and re-raise in
list order so completion-order races never pick the surfaced exception or
abandon an activity.

For current executions (under ``_MERGED_ARCHITECTURE_SIDE_EFFECT_PASS_PATCH``)
the tail passes are two slots — false-positive verify and a single merged
architecture-consistency + side-effect-impact pass (one LLM call) — run
concurrently. This no longer mirrors thread mode, which moved to a fully
sequential merged → combine → filter pipeline (see
``coordinator._run_tail_passes``); the concurrent gather here is retained
purely as a migration-compatibility artifact for in-flight histories.
``_REORDERED_TAIL_PASSES_PATCH`` (see its own docstring) is what now mirrors
thread mode's sequential order: it combines the merged pass's findings with
the map-phase issues via ``combine_findings_activity`` and re-verifies that
combined set, replacing the concurrent gather's result for any execution new
enough to take that path. Histories recorded before the merged-pass patch
existed keep replaying the original three-slot gather (verify → architecture
→ side-effect, as two independent additive activities); see that patch's
docstring for the deprecation plan. Within the concurrent gather itself, the
tail passes have no cross-pass data dependency (each reads only
``review_input`` and/or the map phase's aggregated ``issues``, never another
pass's output) — that gather predates ``_REORDERED_TAIL_PASSES_PATCH``, which
is precisely why the merged pass's own findings never see false-positive
filtering there.

Every phase calls the same underlying coordinator functions (through
:mod:`.activities`): the map unit is ``mapping._cached_review_chunk``,
verification is ``false_positive_filter.filter_false_positives``, the merged
additive architecture + side-effect pass is
``merged_architecture_side_effect_pass.find_architecture_and_side_effect_issues``
(pre-merged-pass histories instead call the standalone
``architecture_consistency_pass.find_architecture_and_redundancy_issues`` and
``side_effect_impact_pass.find_side_effect_impact_issues`` as two activities),
optional consolidation of related ``side-effects`` findings is
``side_effect_consolidation.consolidate_side_effect_issues`` (between the
tail passes and finalize), the gate is
``coordinator._dedupe_issues`` + ``_reconcile_approval``, and the narrative is
``synthesis.synthesize_review_findings`` with the same deterministic-concat
fallback. The verdict is NOT identical to the default thread-mode
``run_coordinator`` path, though: this workflow always folds
``not_reviewed_issues`` into the approval gate as blocking ``high`` findings
(see ``finalize_review_activity``), matching thread mode only under
``CODE_REVIEW_BLOCK_ON_UNREVIEWED`` -- the thread-mode default instead
surfaces them non-blockingly via ``CodeReviewOutput.not_reviewed_ranges``,
a field this workflow's return dict does not populate at all.

Sandbox note: activity and constant imports are wrapped in
``workflow.unsafe.imports_passed_through()``; the workflow body itself performs
no I/O, time, or randomness — only ``execute_activity`` calls,
``asyncio.gather(..., return_exceptions=True)`` for the adaptive map fan-out
and the concurrent tail passes (with a fixed-order scan over those gathered
results), default ``asyncio.gather`` for pre-migration unconstrained map
histories, and pure list aggregation over JSON-native dicts.
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

# Replay-compatibility gate for inserting the side-effect consolidation activity
# between the three tail passes and finalization (see ``run``). Same rationale as
# ``_ARCHITECTURE_PASS_PATCH``: a history recorded before this activity existed has
# no marker for it, so ``workflow.patched`` returns False on replay and that
# history's original finalize-next sequence is reproduced exactly.
# TODO: Remove this gate (and always run consolidation unconditionally) once
# no pre-migration CodeReviewWorkflow histories remain open (confirm via the
# Temporal UI), then deprecate the marker with
# ``workflow.deprecate_patch(_SIDE_EFFECT_CONSOLIDATION_PATCH)`` before deleting it.
_SIDE_EFFECT_CONSOLIDATION_PATCH = "code-review-side-effect-consolidation"

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

# Replay-compatibility gate for replacing the separate architecture-consistency
# and side-effect-impact activities with a single merged-pass activity within
# the concurrent tail-pass gather (see ``run``), mirroring the in-process
# coordinator's ``_run_tail_passes`` (one merged call instead of two additive
# calls). Only consulted once ``_CONCURRENT_TAIL_PASSES_PATCH`` is already
# True, since the merged pass is a refinement of the concurrent scheduling
# path, not the pre-migration sequential one. A history recorded before this
# activity existed has no marker for it, so ``workflow.patched`` returns False
# on replay and that history's original two-activity gather is reproduced
# exactly; a new execution records the marker and always takes the new,
# single-activity path.
# TODO: Remove this gate (and always call the merged activity unconditionally,
# dropping the two-activity branch and _ARCHITECTURE_PASS_PATCH /
# _SIDE_EFFECT_PASS_PATCH along with it) once no pre-migration
# CodeReviewWorkflow histories remain open (confirm via the Temporal UI), then
# deprecate the marker with
# ``workflow.deprecate_patch(_MERGED_ARCHITECTURE_SIDE_EFFECT_PASS_PATCH)``
# before deleting it.
_MERGED_ARCHITECTURE_SIDE_EFFECT_PASS_PATCH = "code-review-merged-architecture-side-effect-pass"

# Replay-compatibility gate for reordering the tail passes to match the
# in-process coordinator's current sequential pipeline (see
# ``coordinator._run_tail_passes``): merged architecture/side-effect pass ->
# ``combine_findings_activity`` over the FULL stream (map-phase issues plus
# both additive lists) -> ``filter_false_positives_activity`` over that
# combined set -> finalize. This fixes a real bug in the concurrent path
# above: under ``_MERGED_ARCHITECTURE_SIDE_EFFECT_PASS_PATCH``'s True branch,
# the merged pass's architecture/side-effect findings are appended onto
# ``verified`` AFTER false-positive filtering already ran (they were
# scheduled concurrently WITH the filter, not sequenced after it), so those
# findings currently bypass false-positive verification entirely; this gate
# routes them through both ``combine_findings_activity`` and the filter,
# exactly as ``_run_tail_passes`` does in thread mode.
#
# Must be checked strictly AFTER every existing gate above (including
# ``_SIDE_EFFECT_CONSOLIDATION_PATCH``), per this file's append-only
# convention for workflow.patched() call order. Because of that ordering
# requirement, this gate cannot be consulted early enough to skip the
# concurrent verify+merged gather or the consolidation activity above for a
# brand-new execution -- both are unconditionally entered (workflow.patched()
# is always True for a live/non-replaying run), so their results are computed
# and then intentionally discarded here in favor of this gate's own
# ``verified``. This is a real, extra filter_false_positives_activity call
# (an LLM round trip) plus a wasted consolidate_side_effect_issues_activity
# call on every new execution for as long as this gate exists -- an accepted,
# temporary cost of the migration window, the same trade-off this file
# already makes for _ARCHITECTURE_PASS_PATCH/_SIDE_EFFECT_PASS_PATCH staying
# registered and called (though unused by the merged branch) since the
# merged-pass migration.
#
# A history recorded before this gate existed (which is every history
# recorded up to and including this deploy) has no marker for it, so
# ``workflow.patched`` returns False on replay and that history's original
# concurrent-gather-then-consolidate-then-finalize sequence is reproduced
# exactly.
# TODO: Remove this gate (and the now-dead concurrent verify+merged gather /
# consolidation call above, always taking the reordered pipeline
# unconditionally) once no pre-reorder CodeReviewWorkflow histories remain
# open (confirm via the Temporal UI), then deprecate the marker with
# ``workflow.deprecate_patch(_REORDERED_TAIL_PASSES_PATCH)`` before deleting
# it. Owner: code-review team -- given the recurring per-review cost this
# gate carries (see above), this cleanup should not wait for an unrelated
# touch of this file; revisit as soon as the Temporal UI confirms zero
# pre-reorder CodeReviewWorkflow executions in flight, rather than only
# opportunistically.
_REORDERED_TAIL_PASSES_PATCH = "code-review-reordered-tail-passes"


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
            - Returns a ``CodeReviewOutput``-shaped dict (``approved``, ``issues``,
              ``summary``, ``spec_compliance_notes`` -- no ``not_reviewed_ranges``
              key) using the same deterministic gate and narrative fallback as
              ``run_coordinator``. The verdict is intentionally not identical to
              the default thread-mode ``run_coordinator`` path: not-reviewed
              ranges always fold into the gate here as blocking ``high`` findings
              (see module docstring), so the same input can approve under thread
              mode's default and reject here, or vice versa.
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
        fanout_width: int = prep.get("fanout_width") or 1

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

            # Same return_exceptions + fixed-order re-raise idiom as the
            # concurrent tail-pass gather below: await every chunk activity
            # (no abandoned siblings) and surface the earliest-index
            # failure rather than whichever chunk happens to fail first in
            # completion order.
            outcomes = await asyncio.gather(
                *[_review_one_bounded(chunk) for chunk in chunks],
                return_exceptions=True,
            )
            first_chunk_exception = next(
                (r for r in outcomes if isinstance(r, BaseException)),
                None,
            )
            if first_chunk_exception is not None:
                raise first_chunk_exception
        else:
            # Pre-migration history: reproduce the original unconstrained
            # fan-out exactly (see _ADAPTIVE_FANOUT_PATCH) — default
            # ``asyncio.gather`` with no ``return_exceptions``, matching
            # histories recorded before the adaptive-fanout / orphaning
            # fix. Do not add ``return_exceptions=True`` here without its
            # own ``workflow.patched`` gate.
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
                # Matches CODE_REVIEW_VERIFY_TIMEOUT_SECONDS default (60m) so a
                # slow per-group tool-using verifier is not killed by the activity
                # budget before its own fail-safe timeout can fire.
                start_to_close_timeout=timedelta(minutes=60),
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

        def _merged_architecture_side_effect() -> Any:
            return workflow.execute_activity(
                A.find_architecture_and_side_effect_activity,
                args=[review_input],
                task_queue=TASK_QUEUE,
                # Same timeout/retry ceiling as the two standalone activities
                # above: this replaces two LLM calls with one, so neither a
                # longer timeout nor a different retry policy is warranted.
                start_to_close_timeout=timedelta(minutes=30),
                retry_policy=_LLM_RETRY,
            )

        # Architecture-consistency / cross-codebase-redundancy pass and
        # side-effect / blast-radius pass are both additive, once per
        # submission (not once per chunk). Below, they are scheduled
        # concurrently with false-positive verification — a migration
        # artifact that no longer matches thread mode's run_coordinator,
        # which runs the merged pass BEFORE verification and folds these
        # findings through the false-positive filter too (see
        # coordinator.py's _run_tail_passes). _REORDERED_TAIL_PASSES_PATCH
        # further down replicates that corrected ordering for new
        # executions. Each pass here is still gated by its own
        # workflow.patched so a pre-migration history (recorded before that
        # activity existed) replays its original finalize-next sequence
        # exactly.
        has_architecture_findings = False
        has_side_effect_findings = False
        # Populated by the _MERGED_ARCHITECTURE_SIDE_EFFECT_PASS_PATCH True
        # branch below (the only branch reachable when
        # _REORDERED_TAIL_PASSES_PATCH is True, since that gate postdates the
        # merged-pass migration); consumed by that gate's branch further down.
        architecture_result: List[Dict[str, Any]] = []
        side_effect_result: List[Dict[str, Any]] = []

        async def _empty_tail_pass() -> List[Dict[str, Any]]:
            # Stand-in for a disabled pass in the gather below: schedules no
            # activity. Keeps ``asyncio.gather``'s coroutine arity fixed at
            # three (verify / architecture / side-effect slots) regardless of
            # which passes are enabled; the ActivityTaskScheduledEvent count
            # equals the number of enabled passes (1-3), not always three.
            return []

        if workflow.patched(_CONCURRENT_TAIL_PASSES_PATCH):
            # Evaluate _ARCHITECTURE_PASS_PATCH / _SIDE_EFFECT_PASS_PATCH
            # UNCONDITIONALLY, in this exact position, before checking
            # _MERGED_ARCHITECTURE_SIDE_EFFECT_PASS_PATCH below.
            # workflow.patched(...) markers are matched against history IN
            # THE ORDER THEY ARE CALLED, not simply by id lookup: a
            # pre-merged-pass history recorded these two markers immediately
            # after _CONCURRENT_TAIL_PASSES_PATCH's, so the merged-pass check
            # must come strictly after them here too, or replaying that
            # history hits a "non-deprecated patch marker encountered ... but
            # there is no corresponding change command" non-determinism error
            # (the merged-pass check would consume the architecture marker
            # meant for _ARCHITECTURE_PASS_PATCH). A brand-new execution just
            # records two additional (here, unused-by-the-merged-branch)
            # markers alongside the merged-pass one -- harmless.
            run_architecture = workflow.patched(_ARCHITECTURE_PASS_PATCH)
            run_side_effect = workflow.patched(_SIDE_EFFECT_PASS_PATCH)

            if workflow.patched(_MERGED_ARCHITECTURE_SIDE_EFFECT_PASS_PATCH):
                # Architecture-consistency and side-effect-impact are now one
                # merged additive pass (one LLM call), mirroring
                # coordinator._run_tail_passes's "filter" + "merged" scheduling
                # — same return_exceptions + fixed-order re-raise idiom as the
                # three-slot gather below, just with two slots instead of three.
                calls = [_verify(), _merged_architecture_side_effect()]
                results = await asyncio.gather(*calls, return_exceptions=True)
                verify_result, merged_result = results

                first_exception = next((r for r in results if isinstance(r, BaseException)), None)
                if first_exception is not None:
                    raise first_exception

                verified = verify_result
                architecture_result = merged_result.get("architecture_findings") or []
                side_effect_result = merged_result.get("side_effect_findings") or []
                if architecture_result:
                    verified = [*verified, *architecture_result]
                    has_architecture_findings = True
                if side_effect_result:
                    verified = [*verified, *side_effect_result]
                    has_side_effect_findings = True
            else:
                # Pre-merged-pass history: reproduce the original three-slot
                # concurrent gather exactly (see
                # _MERGED_ARCHITECTURE_SIDE_EFFECT_PASS_PATCH) — a history
                # recorded under the old code has no marker for the merged
                # pass, so this branch's command sequence (two separate
                # activities, not one) must stay unchanged.
                #
                # None of the three tail passes reads another's output (see
                # coordinator._run_tail_passes), so they can be scheduled as
                # concurrent activities instead of three sequential round-trips —
                # same "create the coroutine, gather later" idiom as the map
                # fan-out above.
                calls = [
                    _verify(),
                    _architecture() if run_architecture else _empty_tail_pass(),
                    _side_effect() if run_side_effect else _empty_tail_pass(),
                ]
                # return_exceptions=True: wait for every tail pass to finish
                # (success or failure) instead of asyncio.gather's default of
                # raising as soon as the first exception surfaces and leaving
                # the others un-awaited -- that default would leave a sibling
                # activity un-awaited from the workflow's point of view (an
                # orphaned command Temporal's workflow sandbox does not
                # tolerate) and pick whichever exception happens to resolve
                # first in real time, which is not guaranteed to match on
                # replay. Unpack each slot into its own local (fixed
                # verify/architecture/side-effect order) BEFORE checking for
                # exceptions, so a failed slot's exception object is read once
                # as itself and never mistaken for (and spread as) a findings
                # list; only once every local holds its plain result value do
                # we re-raise the first exception found, in that same fixed
                # order -- reproducing sequential execution's deterministic
                # error precedence (verify's failure, if any, always wins) and
                # total-failure semantics (a tail-pass failure aborts the whole
                # review before any result is used, exactly as it would if the
                # later passes were never reached). The gather always has a
                # uniform three-coroutine shape via ``_empty_tail_pass`` so
                # disabled passes do not change command count.
                results = await asyncio.gather(*calls, return_exceptions=True)
                verify_result, architecture_result, side_effect_result = results

                first_exception = next((r for r in results if isinstance(r, BaseException)), None)
                if first_exception is not None:
                    raise first_exception

                verified = verify_result
                if architecture_result:
                    verified = [*verified, *architecture_result]
                    has_architecture_findings = True
                if side_effect_result:
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

        # Side-effect / blast-radius consolidation: merge near-duplicate findings
        # that share the same root cause (same enclosing construct or a path:line
        # cited inside another finding's construct). Pure source analysis, no LLM
        # calls, gated independently of the side-effect pass itself. Runs after
        # the concurrent tail-pass gather (it needs the merged verified list),
        # so it is intentionally sequential — not part of that gather.
        if workflow.patched(_SIDE_EFFECT_CONSOLIDATION_PATCH):
            verified = await workflow.execute_activity(
                A.consolidate_side_effect_issues_activity,
                args=[review_input, verified],
                task_queue=TASK_QUEUE,
                start_to_close_timeout=timedelta(minutes=5),
                retry_policy=_DEFAULT_RETRY,
            )

        if workflow.patched(_REORDERED_TAIL_PASSES_PATCH):
            # New executions: replace the concurrently-computed (and now
            # discarded) `verified`/consolidated result above with the
            # coordinator._run_tail_passes-matching sequential order. The
            # merged pass already ran in the branch above (the only branch
            # reachable when this gate is True) -- reuse its
            # architecture_result/side_effect_result and the untouched raw
            # map-phase `issues` rather than re-invoking the merged-pass
            # activity. combine_findings_activity then
            # filter_false_positives_activity run over that combined stream
            # so the additive findings are deduped AND false-positive-checked
            # (see this gate's comment above for the bug this fixes and why
            # the gather above can't be skipped instead).
            #
            # Precondition, enforced rather than left comment-only:
            # architecture_result/side_effect_result are only ever populated
            # by the _MERGED_ARCHITECTURE_SIDE_EFFECT_PASS_PATCH True branch
            # above -- the pre-merged-pass legacy branches (both the 3-slot
            # gather and the fully-sequential pre-_CONCURRENT_TAIL_PASSES_PATCH
            # path) assign differently-named locals and never touch these two.
            # This gate is brand-new (no history has ever recorded its
            # marker), so it only evaluates True for a live execution, and
            # every live execution also takes the merged-pass branch -- but
            # that coupling is otherwise implicit, and a future edit that
            # decouples these two gates could silently reintroduce this PR's
            # bug (architecture/side-effect findings dropped, not just
            # unfiltered). workflow.patched() results are memoized per patch
            # id, so re-checking the same id here is a cache read, not a new
            # replay-order-sensitive event. A plain ``assert`` would be wrong
            # here: it is compiled away under ``-O``/``-OO``, which would
            # silently defeat this exact guard, so this is an explicit,
            # always-evaluated raise instead (mirrors the internal-invariant
            # convention in ``transcript.py``'s ``_note_overflow``).
            if not workflow.patched(_MERGED_ARCHITECTURE_SIDE_EFFECT_PASS_PATCH):
                raise RuntimeError(
                    "_REORDERED_TAIL_PASSES_PATCH requires the merged-pass branch "
                    "to have populated architecture_result/side_effect_result"
                )
            combined = await workflow.execute_activity(
                A.combine_findings_activity,
                args=[review_input, [*issues, *architecture_result, *side_effect_result]],
                task_queue=TASK_QUEUE,
                start_to_close_timeout=timedelta(minutes=5),
                retry_policy=_DEFAULT_RETRY,
            )
            verified = await workflow.execute_activity(
                A.filter_false_positives_activity,
                args=[
                    review_input,
                    combined,
                    bool(review_input.get("skip_false_positive_filter", False)),
                ],
                task_queue=TASK_QUEUE,
                start_to_close_timeout=timedelta(minutes=60),
                retry_policy=_LLM_RETRY,
            )

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
        activity so the narrative can reflect those findings too. When the
        activity returns ``None`` (soft synthesis failure — see
        ``synthesize_findings_activity``), falls back to deterministic
        concatenation of only the per-chunk ``summaries``/``spec_notes`` — the
        architecture/redundancy and side-effect/blast-radius passes contribute
        findings via ``issues``, not summaries, so their findings can be absent
        from this concatenated narrative text on that path (they are never
        absent from the returned ``issues`` list itself, only from this prose
        summary). An exhausted activity retry / infrastructure failure from
        ``execute_activity`` still propagates and fails the workflow.
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
