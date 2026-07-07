"""Durable code review workflow: map-reduce review as a Temporal workflow.

``CodeReviewWorkflow`` reproduces ``coordinator.run_coordinator`` as a durable,
resumable computation. It orchestrates the review as a sequence of activities —
prepare → map fan-out → false-positive verify → deterministic gate → (conditional)
narrative synthesis — so a worker restart mid-review re-runs only the unfinished
activities instead of re-reviewing the whole submission.

The verdict is behavior-identical to thread mode because every phase calls the
same underlying coordinator functions (through :mod:`.activities`): the map unit
is ``mapping._cached_review_chunk``, verification is
``false_positive_filter.filter_false_positives``, the gate is
``coordinator._dedupe_issues`` + ``_reconcile_approval``, and the narrative is
``synthesis.synthesize_review_findings`` with the same deterministic-concat
fallback.

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
            - Returns a ``CodeReviewOutput`` dict whose verdict matches what
              ``run_coordinator`` would produce for the same input (the same
              deterministic gate and narrative fallback are applied).
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

        self._advance("reviewing", 0.10)
        # Fan out: one durable activity per chunk, run concurrently. A worker
        # restart re-runs only the chunks that had not completed.
        outcomes = await asyncio.gather(
            *[
                workflow.execute_activity(
                    A.review_chunk_activity,
                    args=[chunk, base_input, context_fp, surface_by_path],
                    task_queue=TASK_QUEUE,
                    start_to_close_timeout=timedelta(hours=1),
                    heartbeat_timeout=timedelta(minutes=5),
                    retry_policy=_LLM_RETRY,
                )
                for chunk in chunks
            ]
        )

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
        verified = await workflow.execute_activity(
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
            review_input, approved, gated_issues, summaries, spec_notes
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
    ) -> tuple[str, str]:
        """Produce the merged ``(summary, spec_compliance_notes)``.

        Mirrors ``coordinator._merge_narrative``: a single sub-review is used
        verbatim (no synthesis call); more than one attempts a synthesis activity
        and falls back to deterministic concatenation on failure.
        """
        if len(summaries) == 1:
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
