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

# Cheap/deterministic job-store bookkeeping (begin / persist / mark-failed): a
# slightly deeper bounded retry is safe because each write is idempotent.
_BOOKKEEPING_RETRY = RetryPolicy(
    maximum_attempts=3,
    initial_interval=timedelta(seconds=10),
    maximum_interval=timedelta(minutes=1),
    backoff_coefficient=2.0,
)

_SHORT_TIMEOUT = timedelta(minutes=5)
_STEP_TIMEOUT = timedelta(minutes=30)
# recommend_activities loops one LLM call per stop; the activity emits a
# background heartbeat every 30s for the duration of the loop (see
# _ACTIVITIES_HEARTBEAT_INTERVAL_S in temporal/activities.py), so a stalled
# worker is caught within this window rather than only at start-to-close.
# Sized to comfortably exceed a single (possibly slow) per-stop LLM call so a
# legitimately-long call is never mistaken for a stall, while still detecting a
# genuine hang well before the 30-minute start-to-close budget.
_STEP_HEARTBEAT_TIMEOUT = timedelta(minutes=10)

# Legacy single-activity path (the pre-decomposition contract): the whole pipeline
# ran as one long, non-idempotent activity, capped at a single attempt because the
# llm_service layer already fails over on transient provider errors. Retained only
# so pre-patch workflow histories can replay/drain — not used by new executions.
PIPELINE_TIMEOUT = timedelta(hours=2)
NO_RETRY = RetryPolicy(maximum_attempts=1)


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
        """
        # An explicit raise (not ``assert``) so the check isn't stripped under
        # ``-O`` — ``progress`` is a public workflow query, not an internal-only path.
        if not (0.0 <= fraction <= 1.0):
            raise ValueError(f"progress fraction {fraction} out of [0.0, 1.0]")
        self._step = step
        self._fraction = fraction

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
                start_to_close_timeout=_SHORT_TIMEOUT,
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
            # since neither step consumes the other's output. The other activity
            # execution isn't cancelled and keeps running to completion with its
            # result discarded; the outer except below still routes the failure
            # to mark_road_trip_failed_activity, so the run always ends in a
            # definitive FAILED state. Same trade-off branding_team's
            # BrandingWorkflow accepts for its own asyncio.gather of integrations.
            self._advance("recommend_activities_and_logistics", 0.45)
            activities, logistics = await asyncio.gather(
                workflow.execute_activity(
                    _activities.recommend_activities_activity,
                    args=[request, profile, route],
                    task_queue=TASK_QUEUE,
                    start_to_close_timeout=_STEP_TIMEOUT,
                    heartbeat_timeout=_STEP_HEARTBEAT_TIMEOUT,
                    retry_policy=_LLM_RETRY,
                ),
                workflow.execute_activity(
                    _activities.plan_logistics_activity,
                    args=[request, profile, route],
                    task_queue=TASK_QUEUE,
                    start_to_close_timeout=_STEP_TIMEOUT,
                    retry_policy=_LLM_RETRY,
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
                start_to_close_timeout=_SHORT_TIMEOUT,
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
            # cause), then re-raise so the workflow reflects the failure.
            try:
                await workflow.execute_activity(
                    _activities.mark_road_trip_failed_activity,
                    args=[job_id, str(exc)],
                    task_queue=TASK_QUEUE,
                    start_to_close_timeout=_SHORT_TIMEOUT,
                    retry_policy=_BOOKKEEPING_RETRY,
                )
            except Exception:  # noqa: BLE001 — never mask the original pipeline error
                pass
            raise
