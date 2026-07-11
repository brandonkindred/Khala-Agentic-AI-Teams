"""Unit tests for the neutral pipeline core.

Exercises the five per-step specialist functions (each wrapping a typed agent
class, driven through its ``llm=None`` injection seam), the ``run_pipeline``
chaining, and the RUNNING/COMPLETED/FAILED job-store bookkeeping. A fake callable
model stands in for the Strands agent, so no LLM, job service, or Postgres is
needed for the step tests.
"""

from __future__ import annotations

import json

from road_trip_planning_team import pipeline as rtp_pipeline
from road_trip_planning_team.models import (
    LogisticsPlan,
    RoutePlan,
    RouteStop,
    StopActivities,
    TravelerGroupProfile,
    TripItinerary,
)
from road_trip_planning_team.shared.job_store import (
    JOB_STATUS_COMPLETED,
    JOB_STATUS_FAILED,
    create_job,
    get_job,
)


class _FakeLLM:
    """Callable stand-in for a Strands agent: returns a fixed response string.

    Matches the ``self._agent(prompt)`` call the specialist agents make, so a
    test can drive the real parsing/fallback logic without an LLM.
    """

    def __init__(self, response: str) -> None:
        self._response = response

    def __call__(self, prompt: str) -> str:
        return self._response


# ---------------------------------------------------------------------------
# Per-step functions — happy path (LLM JSON parsed) + fallback (bad JSON)
# ---------------------------------------------------------------------------


def test_profile_travelers_parses_llm_json(sample_plan_request):
    llm = _FakeLLM('{"group_description": "family of hikers", "activity_pace": "active"}')
    profile = rtp_pipeline.profile_travelers(sample_plan_request.trip, llm=llm)
    assert isinstance(profile, TravelerGroupProfile)
    assert profile.group_description == "family of hikers"
    assert profile.activity_pace == "active"


def test_profile_travelers_falls_back_on_bad_json(sample_plan_request):
    profile = rtp_pipeline.profile_travelers(sample_plan_request.trip, llm=_FakeLLM("not json"))
    assert isinstance(profile, TravelerGroupProfile)
    # Fallback derives interests from the travelers (Alice → hiking).
    assert "hiking" in profile.combined_interests


def test_plan_route_parses_llm_json(sample_plan_request):
    # The LLM route must cover the required stop (Yosemite) or the coverage guard
    # substitutes the fallback.
    llm = _FakeLLM(
        '{"ordered_stops": [{"location": "SF", "stop_type": "start"},'
        ' {"location": "Yosemite", "stop_type": "destination"}],'
        ' "route_summary": "coastal", "suggested_total_days": 3}'
    )
    route = rtp_pipeline.plan_route(sample_plan_request.trip, TravelerGroupProfile(), llm=llm)
    assert isinstance(route, RoutePlan)
    assert route.route_summary == "coastal"
    assert route.ordered_stops[0].location == "SF"


def test_plan_route_falls_back_on_bad_json(sample_plan_request):
    route = rtp_pipeline.plan_route(
        sample_plan_request.trip, TravelerGroupProfile(), llm=_FakeLLM("x")
    )
    assert isinstance(route, RoutePlan)
    locations = [s.location for s in route.ordered_stops]
    assert "San Francisco, CA" in locations
    assert "Yosemite" in locations


def test_plan_route_falls_back_when_json_omits_stops(sample_plan_request):
    # Valid JSON that omits ordered_stops (e.g. a `{}` refusal) must still yield a
    # route covering start → required stops → end, not an empty route that would
    # compose into an itinerary with no days.
    route = rtp_pipeline.plan_route(
        sample_plan_request.trip, TravelerGroupProfile(), llm=_FakeLLM("{}")
    )
    assert isinstance(route, RoutePlan)
    locations = [s.location for s in route.ordered_stops]
    assert "San Francisco, CA" in locations
    assert "Yosemite" in locations
    assert "Los Angeles, CA" in locations


def test_plan_route_falls_back_when_required_stop_missing(sample_plan_request):
    # A non-empty route that drops a required stop (Yosemite) must be replaced by
    # the fallback so a must-visit stop can't silently vanish from the itinerary.
    llm = _FakeLLM(
        '{"ordered_stops": [{"location": "Reno, NV", "stop_type": "destination"}],'
        ' "route_summary": "wrong", "suggested_total_days": 3}'
    )
    route = rtp_pipeline.plan_route(sample_plan_request.trip, TravelerGroupProfile(), llm=llm)
    locations = [s.location for s in route.ordered_stops]
    assert "Yosemite" in locations  # fallback restored the required stop
    assert route.route_summary == ""  # fallback route, not the LLM's "wrong" summary


def test_fallback_route_endpoints_are_pass_through(sample_plan_request):
    # Fallback endpoints must be pass-through (recommended_nights=0) so the
    # activities/composer steps don't turn the start and end into extra days.
    route = rtp_pipeline.plan_route(
        sample_plan_request.trip, TravelerGroupProfile(), llm=_FakeLLM("x")
    )
    by_type = {s.stop_type: s for s in route.ordered_stops}
    assert by_type["start"].recommended_nights == 0
    assert by_type["end"].recommended_nights == 0
    dests = [s for s in route.ordered_stops if s.stop_type == "destination"]
    assert dests and all(s.recommended_nights == 1 for s in dests)


def test_recommend_activities_parses_and_skips_passthrough(sample_plan_request):
    route = RoutePlan(
        ordered_stops=[
            RouteStop(location="SF", stop_type="start", recommended_nights=0),
            RouteStop(location="Yosemite", stop_type="destination", recommended_nights=1),
        ]
    )
    llm = _FakeLLM('{"activities": [{"name": "Hike"}], "dining": [], "tips": ["bring water"]}')
    result = rtp_pipeline.recommend_activities(
        route, TravelerGroupProfile(), sample_plan_request.trip, llm=llm
    )
    assert [r.location for r in result] == ["SF", "Yosemite"]
    # SF is a pass-through start (0 nights) → empty entry, no LLM call recorded.
    assert result[0].activities == []
    assert result[1].activities == [{"name": "Hike"}]
    assert result[1].tips == ["bring water"]


def test_recommend_activities_invokes_on_stop_per_stop(sample_plan_request):
    route = RoutePlan(
        ordered_stops=[
            RouteStop(location="SF", stop_type="start", recommended_nights=0),
            RouteStop(location="Yosemite", stop_type="destination", recommended_nights=1),
            RouteStop(location="LA", stop_type="end", recommended_nights=0),
        ]
    )
    llm = _FakeLLM('{"activities": [], "dining": [], "tips": []}')
    beats: list[int] = []
    rtp_pipeline.recommend_activities(
        route,
        TravelerGroupProfile(),
        sample_plan_request.trip,
        llm=llm,
        on_stop=lambda: beats.append(1),
    )
    # on_stop fires once per stop, including the pass-through start/end — this is
    # what the Temporal activity uses to heartbeat during the per-stop loop.
    assert len(beats) == 3


def test_recommend_activities_heartbeats_before_stop_work(sample_plan_request):
    # The heartbeat for a stop must be emitted BEFORE its (potentially slow) LLM
    # call, so the heartbeat timer covers the call rather than firing only after.
    route = RoutePlan(
        ordered_stops=[
            RouteStop(location="Yosemite", stop_type="destination", recommended_nights=1)
        ]
    )
    events: list[str] = []

    class _RecordingLLM:
        def __call__(self, prompt: str) -> str:
            events.append("llm")
            return '{"activities": [], "dining": [], "tips": []}'

    rtp_pipeline.recommend_activities(
        route,
        TravelerGroupProfile(),
        sample_plan_request.trip,
        llm=_RecordingLLM(),
        on_stop=lambda: events.append("beat"),
    )
    assert events[0] == "beat"
    assert "llm" in events


def test_plan_logistics_parses_llm_json(sample_plan_request):
    route = RoutePlan(ordered_stops=[RouteStop(location="Yosemite", recommended_nights=1)])
    llm = _FakeLLM(
        '{"stop_logistics": [], "packing_suggestions": ["boots"],'
        ' "travel_tips": ["start early"], "budget_estimate": "$800"}'
    )
    logistics = rtp_pipeline.plan_logistics(
        route, TravelerGroupProfile(), sample_plan_request.trip, llm=llm
    )
    assert isinstance(logistics, LogisticsPlan)
    assert logistics.packing_suggestions == ["boots"]
    assert logistics.budget_estimate == "$800"


def test_plan_logistics_falls_back_on_bad_json(sample_plan_request):
    logistics = rtp_pipeline.plan_logistics(
        RoutePlan(), TravelerGroupProfile(), sample_plan_request.trip, llm=_FakeLLM("boom")
    )
    assert isinstance(logistics, LogisticsPlan)
    assert logistics.packing_suggestions  # non-empty fallback list


def test_compose_itinerary_parses_llm_json(sample_plan_request):
    route = RoutePlan(
        ordered_stops=[RouteStop(location="Yosemite", recommended_nights=1)], suggested_total_days=2
    )
    llm = _FakeLLM(
        json.dumps(
            {
                "title": "SF to LA",
                "overview": "coastal cruise",
                "total_days": 2,
                "days": [
                    {
                        "day_number": 1,
                        "location": "Yosemite",
                        "morning_activities": [{"name": "Hike"}],
                    }
                ],
            }
        )
    )
    itinerary = rtp_pipeline.compose_itinerary(
        sample_plan_request.trip,
        TravelerGroupProfile(),
        route,
        [StopActivities(location="Yosemite")],
        LogisticsPlan(),
        llm=llm,
    )
    assert isinstance(itinerary, TripItinerary)
    assert itinerary.title == "SF to LA"
    assert itinerary.days[0].morning_activities[0].name == "Hike"


def test_compose_itinerary_falls_back_on_bad_json(sample_plan_request):
    route = RoutePlan(
        ordered_stops=[RouteStop(location="Yosemite", recommended_nights=1)], suggested_total_days=2
    )
    itinerary = rtp_pipeline.compose_itinerary(
        sample_plan_request.trip,
        TravelerGroupProfile(),
        route,
        [StopActivities(location="Yosemite")],
        LogisticsPlan(travel_tips=["t"]),
        llm=_FakeLLM("nope"),
    )
    assert isinstance(itinerary, TripItinerary)
    assert itinerary.total_days == 2  # derived from route.suggested_total_days


# ---------------------------------------------------------------------------
# run_pipeline chaining + job-store bookkeeping
# ---------------------------------------------------------------------------


def test_run_pipeline_chains_all_steps_in_order(monkeypatch, sample_plan_request):
    calls: list[str] = []
    profile = TravelerGroupProfile(group_description="fam")
    route = RoutePlan(route_summary="loop")
    activities = [StopActivities(location="Yosemite")]
    logistics = LogisticsPlan(budget_estimate="$900")
    final = TripItinerary(title="Final", total_days=2)
    captured: dict = {}

    def _profile(trip):
        calls.append("profile")
        return profile

    def _route(trip, p):
        calls.append("route")
        return route

    def _activities(r, p, trip):
        calls.append("activities")
        return activities

    def _logistics(r, p, trip):
        calls.append("logistics")
        return logistics

    def _compose(trip, p, r, a, lg):
        calls.append("compose")
        captured.update(profile=p, route=r, activities=a, logistics=lg)
        return final

    monkeypatch.setattr(rtp_pipeline, "profile_travelers", _profile)
    monkeypatch.setattr(rtp_pipeline, "plan_route", _route)
    monkeypatch.setattr(rtp_pipeline, "recommend_activities", _activities)
    monkeypatch.setattr(rtp_pipeline, "plan_logistics", _logistics)
    monkeypatch.setattr(rtp_pipeline, "compose_itinerary", _compose)

    out = rtp_pipeline.run_pipeline(sample_plan_request)

    assert out is final
    assert calls == ["profile", "route", "activities", "logistics", "compose"]
    # The composer receives every upstream step's typed output.
    assert captured["profile"] is profile
    assert captured["route"] is route
    assert captured["activities"] is activities
    assert captured["logistics"] is logistics


def test_run_pipeline_degrades_to_fallback_on_step_failure(monkeypatch, sample_plan_request):
    """A step raising an unexpected error (e.g. a schema-invalid LLM response)
    must not crash thread mode — run_pipeline returns a minimal fallback so the
    job still reaches a terminal COMPLETED state."""

    def _boom(_trip):
        raise ValueError("schema-invalid LLM response")

    monkeypatch.setattr(rtp_pipeline, "profile_travelers", _boom)

    itinerary = rtp_pipeline.run_pipeline(sample_plan_request)

    assert isinstance(itinerary, TripItinerary)
    assert itinerary.title == "Road Trip: San Francisco, CA to Los Angeles, CA"
    assert "failed" in itinerary.overview
    assert itinerary.total_days == 2  # from trip_duration_days


def test_run_plan_core_writes_running_then_completed(monkeypatch, sample_plan_request):
    create_job("job-core", request={"trip": {}})
    canned = TripItinerary(title="Core", total_days=1)
    monkeypatch.setattr(rtp_pipeline, "run_pipeline", lambda body: canned)

    rtp_pipeline.run_plan_core("job-core", sample_plan_request)

    job = get_job("job-core")
    assert job["status"] == JOB_STATUS_COMPLETED
    assert job["result"]["title"] == "Core"


def test_run_plan_background_marks_failed_on_error(monkeypatch, sample_plan_request):
    create_job("job-bg", request={"trip": {}})

    def _boom(_body):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(rtp_pipeline, "run_pipeline", _boom)

    # A daemon-thread runner has no caller to raise to — it must swallow + record.
    rtp_pipeline.run_plan_background("job-bg", sample_plan_request)

    job = get_job("job-bg")
    assert job["status"] == JOB_STATUS_FAILED
    assert "kaboom" in (job.get("error") or "")
