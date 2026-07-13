"""Durable per-step Road Trip Planning workflow.

``RoadTripWorkflow`` reproduces the 5-agent planning pipeline as a durable,
resumable computation. It orchestrates the run as a sequence of activities —
begin (RUNNING) → profile → route → activities → logistics → compose →
persist (COMPLETED) — threading each step's typed output forward as a JSON-safe
dict, so a worker restart re-runs only the unfinished specialist step instead of
the whole multi-agent pipeline.

Each specialist activity wraps the matching neutral ``pipeline`` function the
thread path also uses, so the two modes stay behavior-equivalent (same
``TripItinerary`` result). Job-store status bookkeeping lives entirely in the
activities (begin / persist / mark-failed), never in the workflow body.

The per-step orchestration is gated behind ``workflow.patched`` so that a
workflow started under the earlier single-activity version replays the retained
legacy path (``run_pipeline_activity``) instead of a mismatched command sequence
— an in-flight run therefore drains cleanly across a rolling deploy. See
``_PER_STEP_PATCH`` for the drain/retirement plan.

Kept in its own module (separate from the package ``__init__``) so the
temporalio workflow sandbox can re-import the workflow class without executing
any non-deterministic top-level code (e.g. ``os.getenv``, worker bootstrap).
Sandbox note: activity and constant imports are wrapped in
``workflow.unsafe.imports_passed_through()``; the workflow body performs no I/O,
time, or randomness — only ``execute_activity`` calls and progress bookkeeping
over the returned dicts.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any, Dict

from temporalio import activity, workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from road_trip_planning_team.temporal import activities as _activities
    from road_trip_planning_team.temporal.constants import TASK_QUEUE

# Patch marker gating the per-step orchestration. New executions take the
# ``workflow.patched`` branch (per-step activities); executions started under the
# pre-decomposition version replay the legacy single-activity branch below, so an
# in-flight run during a rolling deploy drains cleanly instead of hitting a
# non-determinism error. Once all pre-patch runs have drained, swap ``patched``
# for ``deprecate_patch`` and (a release later) delete the legacy branch and
# ``run_pipeline_activity``.
_PER_STEP_PATCH = "road-trip-per-step-v1"

# Each specialist step is one LLM-driven agent; the llm_service layer already
# fails over on transient provider errors, so one bounded retry is enough — and
# because a retry re-runs only a single step (the workflow replays the earlier
# steps' results from history), it never re-runs the whole pipeline the way the
# old single-activity NO_RETRY design guarded against.
_LLM_RETRY = RetryPolicy(
    maximum_attempts=2,
    initial_interval=timedelta(seconds=20),
    maximum_interval=timedelta(minutes=3),
    backoff_coefficient=2.0,
)

# Cheap/deterministic job-store bookkeeping on the happy path (begin / persist):
# a slightly deeper bounded retry is safe because each write is idempotent and
# there's no pending failure whose visibility this could delay.
_BOOKKEEPING_RETRY = RetryPolicy(
    maximum_attempts=3,
    initial_interval=timedelta(seconds=10),
    maximum_interval=timedelta(minutes=1),
    backoff_coefficient=2.0,
)

# The mark-failed compensation write sits on the failure-reporting path, where
# speed matters more than resilience: it's what flips the job_store row from
# the stale RUNNING begin_road_trip_job_activity wrote to FAILED, which is what
# any client polling job status actually observes. Using _BOOKKEEPING_RETRY/
# _BOOKKEEPING_TIMEOUT here (3 attempts, up to 5 minutes each) could delay that
# visibility by many minutes on a transient infra blip, during which the job
# looks like it's still running even though the pipeline already failed. One
# retry still covers a single transient blip without leaving a job stuck at
# RUNNING forever on one dropped connection, and bounds the worst case to
# roughly two minutes instead.
_MARK_FAILED_TIMEOUT = timedelta(minutes=1)
_MARK_FAILED_RETRY = RetryPolicy(
    maximum_attempts=2,
    initial_interval=timedelta(seconds=5),
    maximum_interval=timedelta(seconds=15),
    backoff_coefficient=2.0,
)

_BOOKKEEPING_TIMEOUT = timedelta(minutes=5)
_STEP_TIMEOUT = timedelta(minutes=30)
# recommend_activities loops one LLM call per stop; the activity emits a
# background heartbeat every 30s for the duration of the loop (see
# _ACTIVITIES_HEARTBEAT_INTERVAL_S in temporal/activities.py), so a stalled
# worker is caught within this window rather than only at start-to-close.
# Sized to comfortably exceed a single (possibly slow) per-stop LLM call so a
# legitimately-long call is never mistaken for a stall, while still detecting a
# genuine hang well before the 30-minute start-to-close budget.
_STEP_HEARTBEAT_TIMEOUT = timedelta(minutes=10)

# _STEP_TIMEOUT is a per-attempt start_to_close ceiling sized for a
# single-LLM-call step — but recommend_activities_activity makes one call per
# non-pass-through stop (ActivitiesExpertAgent's per-stop loop), so a route
# with several stops (or a couple of slower calls) can legitimately run past
# 30 minutes even though it's actively heartbeating, not stalled. Temporal's
# start_to_close_timeout is a hard ceiling regardless of heartbeats, so a
# fixed 30-minute budget can retry-then-fail a job the old single 2-hour
# pipeline activity would have completed. Scale the budget by stop count
# instead: floored at _STEP_TIMEOUT (small routes are unaffected) and capped
# at PIPELINE_TIMEOUT (the legacy activity's own ceiling, so a pathological
# stop count still fails within a bounded window).
_PER_STOP_ACTIVITIES_TIMEOUT = timedelta(minutes=4)

# Legacy single-activity path (the pre-decomposition contract): the whole pipeline
# ran as one long, non-idempotent activity, capped at a single attempt because the
# llm_service layer already fails over on transient provider errors. Retained only
# so pre-patch workflow histories can replay/drain — not used by new executions.
PIPELINE_TIMEOUT = timedelta(hours=2)
NO_RETRY = RetryPolicy(maximum_attempts=1)


def _activities_timeout_for_route(route: dict[str, Any]) -> timedelta:
    """Scale ``recommend_activities_activity``'s start_to_close_timeout by stop count.

    Preconditions:
        - ``route`` is a ``RoutePlan.model_dump()``-shaped dict — the
          ``plan_route_activity`` result already threaded through the workflow.

    Postconditions:
        - Returns ``_PER_STOP_ACTIVITIES_TIMEOUT`` times the number of
          non-pass-through stops (a stop with ``stop_type`` in
          ``("start", "end")`` and ``recommended_nights == 0`` is
          pass-through and gets no LLM call — see
          ``ActivitiesExpertAgent.run``), floored at ``_STEP_TIMEOUT`` and
          capped at ``PIPELINE_TIMEOUT``.
    """
    non_pass_through = sum(
        1
        for s in route.get("ordered_stops") or []
        if not (s.get("stop_type") in ("start", "end") and s.get("recommended_nights") == 0)
    )
    scaled = _PER_STOP_ACTIVITIES_TIMEOUT * max(1, non_pass_through)
    return min(PIPELINE_TIMEOUT, max(_STEP_TIMEOUT, scaled))


@activity.defn(name="road_trip_run_pipeline")
def run_pipeline_activity(job_id: str, request: dict[str, Any]) -> dict[str, Any]:
    """Legacy whole-pipeline activity — kept for replay/drain of pre-upgrade runs.

    Preconditions:
        - ``job_id`` refers to a job already created in the job store.
        - ``request`` is the serialized ``PlanTripRequest`` (``body.model_dump()``).

    Postconditions:
        - Runs the full pipeline via ``run_plan_core`` (RUNNING → COMPLETED) and
          returns ``{"job_id": job_id}``; on failure marks the row FAILED and
          re-raises. Only reached when replaying a workflow started before the
          per-step patch; new executions never schedule it.
    """
    from road_trip_planning_team.models import PlanTripRequest
    from road_trip_planning_team.pipeline import run_plan_core
    from road_trip_planning_team.shared.job_store import JOB_STATUS_FAILED, update_job

    body = PlanTripRequest(**request)
    try:
        run_plan_core(job_id, body)
    except Exception as e:
        activity.logger.exception("Road trip planning job %s failed", job_id)
        update_job(job_id, status=JOB_STATUS_FAILED, error=str(e))
        raise
    return {"job_id": job_id}


@workflow.defn(name="RoadTripWorkflow")
class RoadTripWorkflow:
    """Runs one road-trip planning job as a durable sequence of per-step activities.

    Invariants:
        - Job-store status bookkeeping (RUNNING → COMPLETED/FAILED) is owned by
          the begin/persist/mark-failed activities, never the workflow body.
        - Each specialist step's output is accumulated from the activity return
          value (replayed deterministically from history on restart), never read
          back via a store round-trip inside the workflow body.
    """

    def __init__(self) -> None:
        """Initialize the queryable progress snapshot.

        Preconditions:
            - None (Temporal constructs the workflow instance with no arguments).

        Postconditions:
            - ``progress()`` reports ``{"step": "starting", "fraction": 0.0}`` until
              the first ``_advance`` call. Progress is advisory — a run completes
              regardless of whether the ``progress`` query is ever issued.
        """
        self._step: str = "starting"
        self._fraction: float = 0.0

    @workflow.query
    def progress(self) -> Dict[str, Any]:
        """Return the current progress snapshot.

        Preconditions:
            - None (read-only query; must not mutate workflow state).

        Postconditions:
            - Returns ``{step, fraction}`` reflecting the last ``_advance`` call;
              no side effects.
        """
        return {"step": self._step, "fraction": self._fraction}

    def _advance(self, step: str, fraction: float) -> None:
        """Update the queryable progress snapshot.

        Preconditions:
            - ``fraction`` is in ``[0.0, 1.0]``.

        Postconditions:
            - ``progress()`` subsequently reports ``step``/``fraction``.

        Raises:
            - ``ValueError`` if ``fraction`` is not in ``[0.0, 1.0]``.
        """
        # An explicit raise (not ``assert``) so the check isn't stripped under
        # ``-O`` — ``progress`` is a public workflow query, not an internal-only path.
        if not (0.0 <= fraction <= 1.0):
            raise ValueError(f"progress fraction {fraction} out of [0.0, 1.0]")
        self._step = step
        self._fraction = fraction

    @staticmethod
    async def _named_activity(name: str, job_id: str, awaitable: Any) -> Any:
        """Await ``awaitable``, logging ``name`` on failure before re-raising.

        Used to wrap sibling ``execute_activity`` calls run concurrently via
        ``asyncio.gather`` (no ``return_exceptions=True``) — pinpoints which
        named step actually failed without altering gather's fail-fast
        propagation, since the tag-and-reraise happens inline within the same
        coroutine gather is already awaiting rather than via a post-hoc check
        of gather's return value.

        Preconditions:
            - ``awaitable`` is a single ``workflow.execute_activity(...)`` call.

        Postconditions:
            - Returns ``awaitable``'s result unchanged on success. On failure,
              logs a warning naming ``name`` and ``job_id`` before re-raising
              the original exception unchanged.
        """
        try:
            return await awaitable
        except Exception:  # noqa: BLE001 — log which step failed, then re-raise unchanged
            workflow.logger.warning(
                "%s failed for job %s; its concurrent sibling may still be "
                "running to completion with its result (and any LLM cost "
                "incurred) discarded, since it isn't cancelled",
                name,
                job_id,
            )
            raise

    @workflow.run
    async def run(self, job_id: str, request: dict[str, Any]) -> dict[str, Any]:
        """Durable entrypoint: run the road-trip pipeline for ``job_id``.

        Preconditions:
            - ``job_id`` refers to a job already created in the job store.
            - ``request`` is the serialized ``PlanTripRequest`` (``body.model_dump()``).

        Postconditions:
            - New executions (``workflow.patched(_PER_STEP_PATCH)``) drive the
              per-step orchestration in ``_run_per_step``. A workflow started
              before the patch replays the legacy single-activity path instead,
              so an in-flight pre-upgrade run drains without a non-determinism
              error. Both return ``{"job_id": job_id}``.

        Raises:
            - Any exception from the per-step (``_run_per_step``) or legacy
              (``run_pipeline_activity``) execution, after each path's own
              best-effort FAILED recording — see their respective docstrings.
        """
        if workflow.patched(_PER_STEP_PATCH):
            return await self._run_per_step(job_id, request)
        # Legacy branch — reached only when replaying a workflow started before
        # the per-step patch; deterministic for those histories.
        return await workflow.execute_activity(
            run_pipeline_activity,
            args=[job_id, request],
            task_queue=TASK_QUEUE,
            start_to_close_timeout=PIPELINE_TIMEOUT,
            retry_policy=NO_RETRY,
        )

    async def _run_per_step(self, job_id: str, request: dict[str, Any]) -> dict[str, Any]:
        """Per-step orchestration: begin → specialists → persist.

        Postconditions:
            - Drives begin → the five specialist steps → persist; the persist
              activity owns the COMPLETED transition and returns
              ``{"job_id": job_id}``. Any step/bookkeeping failure advances
              ``progress()`` to ``{"step": "failed", "fraction": 0.0}``, records a
              FAILED row (best-effort), and re-raises so the workflow reflects the
              failure rather than completing — or appearing to still be
              running — silently.
        """
        try:
            self._advance("starting", 0.0)
            await workflow.execute_activity(
                _activities.begin_road_trip_job_activity,
                args=[job_id],
                task_queue=TASK_QUEUE,
                start_to_close_timeout=_BOOKKEEPING_TIMEOUT,
                retry_policy=_BOOKKEEPING_RETRY,
            )

            self._advance("profile_travelers", 0.05)
            profile = await workflow.execute_activity(
                _activities.profile_travelers_activity,
                args=[request],
                task_queue=TASK_QUEUE,
                start_to_close_timeout=_STEP_TIMEOUT,
                retry_policy=_LLM_RETRY,
            )

            self._advance("plan_route", 0.25)
            route = await workflow.execute_activity(
                _activities.plan_route_activity,
                args=[request, profile],
                task_queue=TASK_QUEUE,
                start_to_close_timeout=_STEP_TIMEOUT,
                retry_policy=_LLM_RETRY,
            )

            # recommend_activities and plan_logistics both derive only from
            # route + profile + trip and don't consume each other's output, so run
            # them concurrently and join before composing.
            # No return_exceptions: either activity's failure propagates from
            # gather immediately rather than waiting on the sibling — intentional
            # since neither step consumes the other's output. Neither this
            # workflow nor the worker cancels the still-running sibling — it
            # keeps executing (including any in-flight LLM calls) to completion
            # with its result discarded, since neither activity currently
            # checks for cooperative cancellation mid-call; the outer except
            # below still routes the failure to mark_road_trip_failed_activity,
            # so the run always ends in a definitive FAILED state. Same
            # trade-off branding_team's BrandingWorkflow accepts for its own
            # asyncio.gather of integrations. _named_activity below logs which
            # specific step failed (so the *other*, still-running one is
            # identifiable by elimination — there are only the two) without
            # changing this fail-fast propagation: it tags-and-reraises inline
            # from within the same coroutine gather is already awaiting, rather
            # than switching to return_exceptions=True and waiting for both.
            self._advance("recommend_activities_and_logistics", 0.45)
            activities, logistics = await asyncio.gather(
                self._named_activity(
                    "recommend_activities_activity",
                    job_id,
                    workflow.execute_activity(
                        _activities.recommend_activities_activity,
                        args=[request, profile, route],
                        task_queue=TASK_QUEUE,
                        start_to_close_timeout=_activities_timeout_for_route(route),
                        heartbeat_timeout=_STEP_HEARTBEAT_TIMEOUT,
                        retry_policy=_LLM_RETRY,
                    ),
                ),
                self._named_activity(
                    "plan_logistics_activity",
                    job_id,
                    workflow.execute_activity(
                        _activities.plan_logistics_activity,
                        args=[request, profile, route],
                        task_queue=TASK_QUEUE,
                        start_to_close_timeout=_STEP_TIMEOUT,
                        retry_policy=_LLM_RETRY,
                    ),
                ),
            )

            self._advance("compose_itinerary", 0.85)
            itinerary = await workflow.execute_activity(
                _activities.compose_itinerary_activity,
                args=[request, profile, route, activities, logistics],
                task_queue=TASK_QUEUE,
                start_to_close_timeout=_STEP_TIMEOUT,
                retry_policy=_LLM_RETRY,
            )

            self._advance("persisting", 0.97)
            await workflow.execute_activity(
                _activities.persist_itinerary_activity,
                args=[job_id, itinerary],
                task_queue=TASK_QUEUE,
                start_to_close_timeout=_BOOKKEEPING_TIMEOUT,
                retry_policy=_BOOKKEEPING_RETRY,
            )
            self._advance("done", 1.0)
            return {"job_id": job_id}
        except Exception as exc:  # noqa: BLE001 — record the failure, then re-raise
            # A caller polling progress() after this run raises must see "failed"
            # rather than the last successful step's snapshot (e.g. plan_route at
            # 0.25), which would misleadingly read as still in progress.
            self._advance("failed", 0.0)
            # Best-effort FAILED write (its own failure must not mask the original
            # cause), then re-raise so the workflow reflects the failure. Uses the
            # tighter _MARK_FAILED_RETRY/_MARK_FAILED_TIMEOUT, not
            # _BOOKKEEPING_RETRY/_BOOKKEEPING_TIMEOUT — see _MARK_FAILED_RETRY's comment.
            try:
                await workflow.execute_activity(
                    _activities.mark_road_trip_failed_activity,
                    args=[job_id, str(exc)],
                    task_queue=TASK_QUEUE,
                    start_to_close_timeout=_MARK_FAILED_TIMEOUT,
                    retry_policy=_MARK_FAILED_RETRY,
                )
            except Exception as mark_failed_exc:  # noqa: BLE001 — never mask the original error
                # Swallowed (not re-raised) so the original pipeline failure below
                # is what the workflow surfaces — but logged, not silently
                # dropped, so an operator can still see that job_id's job_store
                # row may be stuck at RUNNING because this compensation write
                # itself failed.
                workflow.logger.warning(
                    "mark_road_trip_failed_activity failed for job %s (original error: %s): %s",
                    job_id,
                    exc,
                    mark_failed_exc,
                )
            raise
