"""Tests for the road-trip per-step Temporal activities + workflow.

The pipeline is decomposed into eight activities the durable ``RoadTripWorkflow``
drives one at a time. These tests pin two contracts:

- Each specialist activity reconstructs its typed inputs from JSON-safe dicts,
  calls the matching neutral ``pipeline`` function, and returns a JSON-safe dict;
  the begin/persist/mark-failed activities own the job-store status writes
  (checked against the in-memory fake job client installed by ``conftest.py``).
- ``RoadTripWorkflow.run`` dispatches begin → the five specialist steps →
  persist in order (threading each result forward) with the right retry policies,
  and routes any failure to ``mark_road_trip_failed_activity`` before re-raising.
"""

from __future__ import annotations

import asyncio
import inspect
import typing

import pytest
from temporalio.testing import ActivityEnvironment

import shared_concurrency
from road_trip_planning_team import pipeline as rtp_pipeline
from road_trip_planning_team.models import (
    LogisticsPlan,
    RoutePlan,
    StopActivities,
    TravelerGroupProfile,
    TripItinerary,
    TripRequest,
)
from road_trip_planning_team.shared.job_store import (
    JOB_STATUS_COMPLETED,
    JOB_STATUS_FAILED,
    JOB_STATUS_RUNNING,
    create_job,
    get_job,
)
from road_trip_planning_team.temporal import activities as acts
from road_trip_planning_team.temporal import workflows as wf


def _run(fn, *args):
    """Run a sync activity inside a lightweight Temporal activity context."""
    return ActivityEnvironment().run(fn, *args)


# ---------------------------------------------------------------------------
# Bookkeeping activities
# ---------------------------------------------------------------------------


def test_begin_activity_marks_running(sample_trip_body):
    create_job("job-begin", request=sample_trip_body)

    out = _run(acts.begin_road_trip_job_activity, "job-begin")

    assert out == {"job_id": "job-begin"}
    assert get_job("job-begin")["status"] == JOB_STATUS_RUNNING


def test_persist_activity_marks_completed_with_result(sample_trip_body):
    create_job("job-persist", request=sample_trip_body)
    itinerary = TripItinerary(title="Done", total_days=1).model_dump()

    out = _run(acts.persist_itinerary_activity, "job-persist", itinerary)

    assert out == {"job_id": "job-persist"}
    job = get_job("job-persist")
    assert job["status"] == JOB_STATUS_COMPLETED
    assert job["result"]["title"] == "Done"


def test_mark_failed_activity_records_failed(sample_trip_body):
    create_job("job-fail", request=sample_trip_body)

    _run(acts.mark_road_trip_failed_activity, "job-fail", "pipeline exploded")

    job = get_job("job-fail")
    assert job["status"] == JOB_STATUS_FAILED
    assert "pipeline exploded" in (job.get("error") or "")


def test_mark_failed_activity_does_not_clobber_completed_job(sample_trip_body):
    # Temporal activities are at-least-once: persist_itinerary_activity may have
    # already durably written COMPLETED even if its completion signal to the
    # workflow was lost, leaving a subsequent retry-exhaustion to reach this
    # handler. It must no-op rather than overwrite the already-successful result.
    create_job("job-already-done", request=sample_trip_body)
    itinerary = TripItinerary(title="Already Done", total_days=1).model_dump()
    _run(acts.persist_itinerary_activity, "job-already-done", itinerary)

    _run(acts.mark_road_trip_failed_activity, "job-already-done", "stale failure")

    job = get_job("job-already-done")
    assert job["status"] == JOB_STATUS_COMPLETED
    assert job["result"]["title"] == "Already Done"


# ---------------------------------------------------------------------------
# Specialist activities — each reconstructs typed inputs and returns a dict
# ---------------------------------------------------------------------------


def test_profile_travelers_activity_returns_profile_dict(monkeypatch, sample_trip_body):
    canned = TravelerGroupProfile(group_description="family of hikers")
    captured: dict = {}

    def _fake(trip, llm=None):
        captured["trip"] = trip
        return canned

    monkeypatch.setattr(rtp_pipeline, "profile_travelers", _fake)

    out = _run(acts.profile_travelers_activity, sample_trip_body)

    assert out == canned.model_dump()
    assert isinstance(captured["trip"], TripRequest)
    assert captured["trip"].start_location == "San Francisco, CA"


def test_plan_route_activity_returns_route_dict(monkeypatch, sample_trip_body):
    canned = RoutePlan(route_summary="SF → Yosemite → LA")
    captured: dict = {}

    def _fake(trip, group_profile, llm=None):
        captured["trip"] = trip
        captured["profile"] = group_profile
        return canned

    monkeypatch.setattr(rtp_pipeline, "plan_route", _fake)
    profile = TravelerGroupProfile(group_description="fam").model_dump()

    out = _run(acts.plan_route_activity, sample_trip_body, profile)

    assert out == canned.model_dump()
    assert isinstance(captured["trip"], TripRequest)
    assert isinstance(captured["profile"], TravelerGroupProfile)
    assert captured["profile"].group_description == "fam"


def test_recommend_activities_activity_returns_list_of_dicts(monkeypatch, sample_trip_body):
    canned = [StopActivities(location="Yosemite"), StopActivities(location="Los Angeles, CA")]
    captured: dict = {}

    def _fake(route, group_profile, trip, llm=None):
        captured["route"] = route
        return canned

    monkeypatch.setattr(rtp_pipeline, "recommend_activities", _fake)

    heartbeat_calls: list[tuple] = []

    class _FakeBackgroundHeartbeat:
        def __init__(self, beat, interval_s, *, copy_context=False, **kwargs):
            heartbeat_calls.append((beat, interval_s, copy_context))

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

    monkeypatch.setattr(shared_concurrency, "BackgroundHeartbeat", _FakeBackgroundHeartbeat)
    profile = TravelerGroupProfile().model_dump()
    route = RoutePlan(route_summary="loop").model_dump()

    out = _run(acts.recommend_activities_activity, sample_trip_body, profile, route)

    assert out == [s.model_dump() for s in canned]
    assert isinstance(captured["route"], RoutePlan)
    assert captured["route"].route_summary == "loop"
    # The activity wraps the per-stop LLM loop in a background heartbeat rather
    # than threading a per-stop callback through the business-logic call (the
    # _fake signature above has no on_stop param — a leftover call-site
    # argument would raise a TypeError and fail this test).
    assert len(heartbeat_calls) == 1
    beat, interval_s, copy_context = heartbeat_calls[0]
    assert callable(beat)
    assert interval_s == acts._ACTIVITIES_HEARTBEAT_INTERVAL_S
    assert copy_context is True


def test_plan_logistics_activity_returns_dict(monkeypatch, sample_trip_body):
    canned = LogisticsPlan(budget_estimate="$1000")

    def _fake(route, group_profile, trip, llm=None):
        return canned

    monkeypatch.setattr(rtp_pipeline, "plan_logistics", _fake)
    profile = TravelerGroupProfile().model_dump()
    route = RoutePlan().model_dump()

    out = _run(acts.plan_logistics_activity, sample_trip_body, profile, route)

    assert out == canned.model_dump()
    assert out["budget_estimate"] == "$1000"


def test_compose_itinerary_activity_returns_itinerary_dict(monkeypatch, sample_trip_body):
    canned = TripItinerary(title="SF to LA", total_days=2)
    captured: dict = {}

    def _fake(trip, group_profile, route, activities_per_stop, logistics, llm=None):
        captured["activities"] = activities_per_stop
        captured["logistics"] = logistics
        return canned

    monkeypatch.setattr(rtp_pipeline, "compose_itinerary", _fake)

    out = _run(
        acts.compose_itinerary_activity,
        sample_trip_body,
        TravelerGroupProfile().model_dump(),
        RoutePlan().model_dump(),
        [StopActivities(location="Yosemite").model_dump()],
        LogisticsPlan().model_dump(),
    )

    assert out == canned.model_dump()
    # The list of StopActivities dicts is reconstructed into typed models.
    assert isinstance(captured["activities"][0], StopActivities)
    assert captured["activities"][0].location == "Yosemite"
    assert isinstance(captured["logistics"], LogisticsPlan)


# ---------------------------------------------------------------------------
# Workflow orchestration
# ---------------------------------------------------------------------------


def test_workflow_run_signature_takes_job_id_and_request():
    """Regression guard: the dispatcher passes (job_id, request)."""
    sig = inspect.signature(wf.RoadTripWorkflow.run)
    params = list(sig.parameters.values())
    assert [p.name for p in params] == ["self", "job_id", "request"]
    # ``from __future__ import annotations`` stringizes annotations; resolve
    # them via get_type_hints() rather than comparing string forms, which
    # would be fragile to reformatting or typing-module changes.
    hints = typing.get_type_hints(wf.RoadTripWorkflow.run)
    assert hints["job_id"] is str
    assert hints["request"] == dict[str, typing.Any]


def test_workflow_dispatches_begin_five_steps_persist(monkeypatch, sample_trip_body):
    calls: list[dict] = []

    async def _fake_execute_activity(fn, *args, **kwargs):
        calls.append(
            {
                "name": fn.__name__,
                "args": kwargs.get("args"),
                "retry": kwargs.get("retry_policy"),
                "timeout": kwargs.get("start_to_close_timeout"),
                "heartbeat": kwargs.get("heartbeat_timeout"),
                # kwargs.get("heartbeat_timeout") alone can't distinguish "the
                # kwarg was never passed" from "it was passed as None" — both
                # return None. Track presence explicitly so the no-heartbeat
                # assertion below actually verifies the kwarg was omitted.
                "heartbeat_set": "heartbeat_timeout" in kwargs,
            }
        )
        # Thread an identifiable stub forward so arg-threading is checkable.
        return {"stub": fn.__name__}

    # New executions take the per-step (patched) branch.
    monkeypatch.setattr(wf.workflow, "patched", lambda _patch_id: True)
    monkeypatch.setattr(wf.workflow, "execute_activity", _fake_execute_activity)

    instance = wf.RoadTripWorkflow()
    out = asyncio.run(instance.run("job-wf", sample_trip_body))

    assert out == {"job_id": "job-wf"}

    names = [c["name"] for c in calls]
    # begin → profile → route, then recommend + logistics run concurrently (order
    # between them is not guaranteed), then compose → persist.
    assert names[:3] == [
        "begin_road_trip_job_activity",
        "profile_travelers_activity",
        "plan_route_activity",
    ]
    assert set(names[3:5]) == {"recommend_activities_activity", "plan_logistics_activity"}
    assert names[5:] == ["compose_itinerary_activity", "persist_itinerary_activity"]

    # Arg threading: each step receives the request + upstream stub results.
    by_name = {c["name"]: c for c in calls}
    assert by_name["begin_road_trip_job_activity"]["args"] == ["job-wf"]
    assert by_name["profile_travelers_activity"]["args"] == [sample_trip_body]
    assert by_name["plan_route_activity"]["args"] == [
        sample_trip_body,
        {"stub": "profile_travelers_activity"},
    ]
    assert by_name["compose_itinerary_activity"]["args"] == [
        sample_trip_body,
        {"stub": "profile_travelers_activity"},
        {"stub": "plan_route_activity"},
        {"stub": "recommend_activities_activity"},
        {"stub": "plan_logistics_activity"},
    ]
    assert by_name["persist_itinerary_activity"]["args"] == [
        "job-wf",
        {"stub": "compose_itinerary_activity"},
    ]

    # Retry policy split: specialists get the bounded LLM retry, bookkeeping the
    # deeper idempotent-write retry.
    assert by_name["begin_road_trip_job_activity"]["retry"] is wf._BOOKKEEPING_RETRY
    assert by_name["persist_itinerary_activity"]["retry"] is wf._BOOKKEEPING_RETRY
    for step in (
        "profile_travelers_activity",
        "plan_route_activity",
        "recommend_activities_activity",
        "plan_logistics_activity",
        "compose_itinerary_activity",
    ):
        assert by_name[step]["retry"] is wf._LLM_RETRY

    # Only the per-stop recommend loop carries a heartbeat timeout — checked by
    # kwarg presence, not just value, since an absent kwarg and an explicit
    # heartbeat_timeout=None are otherwise indistinguishable via .get().
    assert by_name["recommend_activities_activity"]["heartbeat"] is wf._STEP_HEARTBEAT_TIMEOUT
    assert by_name["recommend_activities_activity"]["heartbeat_set"] is True
    assert by_name["plan_logistics_activity"]["heartbeat_set"] is False

    # Progress reaches the terminal snapshot.
    assert instance.progress() == {"step": "done", "fraction": 1.0}


def test_workflow_dispatches_activities_and_logistics_via_asyncio_gather(
    monkeypatch, sample_trip_body
):
    # test_workflow_dispatches_begin_five_steps_persist only checks that both
    # recommend_activities_activity and plan_logistics_activity appear in
    # positions 3-4 of the call list — that assertion would pass identically
    # if the two were awaited sequentially instead of concurrently, since both
    # produce the same flat call order. Spy on asyncio.gather itself to verify
    # the two are actually dispatched together as a single concurrent join.
    real_gather = asyncio.gather
    gather_calls: list[int] = []

    async def _fake_execute_activity(fn, *args, **kwargs):
        return {"stub": fn.__name__}

    def _spy_gather(*aws, **kwargs):
        gather_calls.append(len(aws))
        return real_gather(*aws, **kwargs)

    monkeypatch.setattr(wf.workflow, "patched", lambda _patch_id: True)
    monkeypatch.setattr(wf.workflow, "execute_activity", _fake_execute_activity)
    monkeypatch.setattr(wf.asyncio, "gather", _spy_gather)

    asyncio.run(wf.RoadTripWorkflow().run("job-gather", sample_trip_body))

    # Exactly one gather call, joining exactly the two concurrent activities.
    assert gather_calls == [2]


def test_workflow_progress_starts_before_run():
    assert wf.RoadTripWorkflow().progress() == {"step": "starting", "fraction": 0.0}


def test_advance_raises_valueerror_on_out_of_range_fraction():
    # An explicit raise (not assert) so the check survives python -O — progress()
    # is a public workflow query, not an internal-only invariant.
    with pytest.raises(ValueError, match=r"out of \[0.0, 1.0\]"):
        wf.RoadTripWorkflow()._advance("bad", 1.5)


def test_workflow_marks_failed_and_reraises_on_step_error(monkeypatch, sample_trip_body):
    calls: list[dict] = []

    async def _fake_execute_activity(fn, *args, **kwargs):
        calls.append({"name": fn.__name__, "args": kwargs.get("args")})
        if fn.__name__ == "plan_route_activity":
            raise RuntimeError("route boom")
        return {"stub": fn.__name__}

    monkeypatch.setattr(wf.workflow, "patched", lambda _patch_id: True)
    monkeypatch.setattr(wf.workflow, "execute_activity", _fake_execute_activity)

    instance = wf.RoadTripWorkflow()
    with pytest.raises(RuntimeError, match="route boom"):
        asyncio.run(instance.run("job-x", sample_trip_body))

    names = [c["name"] for c in calls]
    # Failure short-circuits the remaining steps and routes to mark-failed.
    assert "persist_itinerary_activity" not in names
    fail_call = next(c for c in calls if c["name"] == "mark_road_trip_failed_activity")
    assert fail_call["args"][0] == "job-x"
    assert "route boom" in fail_call["args"][1]

    # Progress must reflect the failure rather than the last successful step's
    # snapshot (plan_route at 0.25) — a caller polling progress() after run()
    # raises needs to distinguish "failed" from "still running."
    assert instance.progress() == {"step": "failed", "fraction": 0.0}


def test_workflow_reraises_original_error_even_if_mark_failed_fails(monkeypatch, sample_trip_body):
    """A failure in the best-effort mark-failed write must not mask the cause."""

    calls: list[str] = []

    async def _fake_execute_activity(fn, *args, **kwargs):
        calls.append(fn.__name__)
        if fn.__name__ == "begin_road_trip_job_activity":
            raise RuntimeError("begin boom")
        if fn.__name__ == "mark_road_trip_failed_activity":
            raise RuntimeError("mark boom")
        return {}

    monkeypatch.setattr(wf.workflow, "patched", lambda _patch_id: True)
    monkeypatch.setattr(wf.workflow, "execute_activity", _fake_execute_activity)

    with pytest.raises(RuntimeError, match="begin boom"):
        asyncio.run(wf.RoadTripWorkflow().run("job-y", sample_trip_body))

    # The mark-failed write must actually be attempted (and itself fail) before
    # the original error is re-raised — not silently skipped, which this test
    # would otherwise fail to catch since only the re-raised exception is checked.
    assert "mark_road_trip_failed_activity" in calls


def test_workflow_legacy_branch_dispatches_single_activity(monkeypatch, sample_trip_body):
    """A pre-patch history replays the legacy single-activity path deterministically.

    ``workflow.patched`` returns False when replaying a workflow started before the
    per-step patch, so ``run`` must dispatch exactly one ``run_pipeline_activity``
    (the old contract: 2h timeout, single attempt) and none of the per-step
    bookkeeping activities.
    """
    # A list, not a single overwritten dict — a dict would silently keep only
    # the last call's info, hiding an extra (e.g. bookkeeping) dispatch behind
    # a same-keyed overwrite instead of surfacing it as a second entry.
    calls: list[dict] = []

    async def _fake_execute_activity(fn, *args, **kwargs):
        calls.append(
            {
                "fn": fn,
                "args": kwargs.get("args"),
                "retry": kwargs.get("retry_policy"),
                "timeout": kwargs.get("start_to_close_timeout"),
            }
        )
        return {"job_id": "job-legacy"}

    monkeypatch.setattr(wf.workflow, "patched", lambda _patch_id: False)
    monkeypatch.setattr(wf.workflow, "execute_activity", _fake_execute_activity)

    out = asyncio.run(wf.RoadTripWorkflow().run("job-legacy", sample_trip_body))

    assert out == {"job_id": "job-legacy"}
    # Exactly one activity dispatched — none of the per-step bookkeeping
    # activities (begin/persist/mark-failed) run in the legacy branch, which
    # owns its bookkeeping internally via run_pipeline_activity.
    assert len(calls) == 1
    captured = calls[0]
    assert captured["fn"] is wf.run_pipeline_activity
    assert captured["args"] == ["job-legacy", sample_trip_body]
    assert captured["retry"] is wf.NO_RETRY
    assert captured["timeout"] == wf.PIPELINE_TIMEOUT


def test_run_pipeline_activity_marks_completed(monkeypatch, sample_trip_body):
    """The retained legacy activity keeps its RUNNING → COMPLETED bookkeeping."""
    from road_trip_planning_team import pipeline as rtp_pipeline

    canned = TripItinerary(title="Legacy Trip", overview="ok", total_days=3)
    monkeypatch.setattr(rtp_pipeline, "run_pipeline", lambda body: canned)
    create_job("job-legacy-ok", request=sample_trip_body)

    result = wf.run_pipeline_activity("job-legacy-ok", sample_trip_body)

    assert result == {"job_id": "job-legacy-ok"}
    job = get_job("job-legacy-ok")
    assert job["status"] == JOB_STATUS_COMPLETED
    assert job["result"]["title"] == "Legacy Trip"


def test_run_pipeline_activity_marks_failed_and_reraises(monkeypatch, sample_trip_body):
    from road_trip_planning_team import pipeline as rtp_pipeline

    def _boom(_body):
        raise RuntimeError("legacy pipeline exploded")

    monkeypatch.setattr(rtp_pipeline, "run_pipeline", _boom)
    create_job("job-legacy-boom", request=sample_trip_body)

    with pytest.raises(RuntimeError, match="legacy pipeline exploded"):
        wf.run_pipeline_activity("job-legacy-boom", sample_trip_body)

    job = get_job("job-legacy-boom")
    assert job["status"] == JOB_STATUS_FAILED
    assert "legacy pipeline exploded" in (job.get("error") or "")
