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

Kept in its own module (separate from the package ``__init__``) so the
temporalio workflow sandbox can re-import the workflow class without executing
any non-deterministic top-level code (e.g. ``os.getenv``, worker bootstrap).
Sandbox note: activity and constant imports are wrapped in
``workflow.unsafe.imports_passed_through()``; the workflow body performs no I/O,
time, or randomness — only ``execute_activity`` calls and progress bookkeeping
over the returned dicts.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Dict

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from road_trip_planning_team.temporal import activities as _activities
    from road_trip_planning_team.temporal.constants import TASK_QUEUE

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
_DEFAULT_RETRY = RetryPolicy(
    maximum_attempts=3,
    initial_interval=timedelta(seconds=10),
    maximum_interval=timedelta(minutes=1),
    backoff_coefficient=2.0,
)

_SHORT_TIMEOUT = timedelta(minutes=5)
_STEP_TIMEOUT = timedelta(minutes=30)


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
        # Progress is exposed via the ``progress`` query; it is not required for a
        # run to complete.
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
        self._step = step
        self._fraction = fraction

    @workflow.run
    async def run(self, job_id: str, request: dict[str, Any]) -> dict[str, Any]:
        """Durable entrypoint: run the road-trip pipeline for ``job_id``.

        Preconditions:
            - ``job_id`` refers to a job already created in the job store.
            - ``request`` is the serialized ``PlanTripRequest`` (``body.model_dump()``).

        Postconditions:
            - Drives begin → the five specialist steps → persist; the persist
              activity owns the COMPLETED transition and the workflow returns
              ``{"job_id": job_id}``. Any step/bookkeeping failure records a
              FAILED row (best-effort) and re-raises so the workflow reflects the
              failure rather than completing silently.
        """
        try:
            self._advance("starting", 0.0)
            await workflow.execute_activity(
                _activities.begin_road_trip_job_activity,
                args=[job_id],
                task_queue=TASK_QUEUE,
                start_to_close_timeout=_SHORT_TIMEOUT,
                retry_policy=_DEFAULT_RETRY,
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

            self._advance("recommend_activities", 0.45)
            activities = await workflow.execute_activity(
                _activities.recommend_activities_activity,
                args=[request, profile, route],
                task_queue=TASK_QUEUE,
                start_to_close_timeout=_STEP_TIMEOUT,
                retry_policy=_LLM_RETRY,
            )

            self._advance("plan_logistics", 0.65)
            logistics = await workflow.execute_activity(
                _activities.plan_logistics_activity,
                args=[request, profile, route],
                task_queue=TASK_QUEUE,
                start_to_close_timeout=_STEP_TIMEOUT,
                retry_policy=_LLM_RETRY,
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
                retry_policy=_DEFAULT_RETRY,
            )
            self._advance("done", 1.0)
            return {"job_id": job_id}
        except Exception as exc:  # noqa: BLE001 — record the failure, then re-raise
            # Best-effort FAILED write (its own failure must not mask the original
            # cause), then re-raise so the workflow reflects the failure.
            try:
                await workflow.execute_activity(
                    _activities.mark_road_trip_failed_activity,
                    args=[job_id, str(exc)],
                    task_queue=TASK_QUEUE,
                    start_to_close_timeout=_SHORT_TIMEOUT,
                    retry_policy=_DEFAULT_RETRY,
                )
            except Exception:  # noqa: BLE001 — never mask the original pipeline error
                pass
            raise
