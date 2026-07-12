"""Unit tests for the neutral pipeline core.

Exercises the five per-step specialist functions (each wrapping a typed agent
class, driven through its ``llm=None`` injection seam), the ``run_pipeline``
chaining, and the RUNNING/COMPLETED/FAILED job-store bookkeeping. A fake callable
model stands in for the Strands agent, so no LLM, job service, or Postgres is
needed for the step tests.
"""

from __future__ import annotations

import json

import pytest

from road_trip_planning_team import pipeline as rtp_pipeline
from road_trip_planning_team.models import (
    LogisticsPlan,
    RoutePlan,
    RouteStop,
    StopActivities,
    TravelerGroupProfile,
    TripItinerary,
    TripRequest,
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


def test_profile_travelers_falls_back_on_schema_invalid_json(sample_plan_request):
    # Syntactically valid JSON with a schema-invalid field (a string where
    # age_groups_present expects a list) must also fall back — a pydantic
    # ValidationError from model_validate must not bypass the fallback.
    llm = _FakeLLM('{"group_description": "family", "age_groups_present": "not-a-list"}')
    profile = rtp_pipeline.profile_travelers(sample_plan_request.trip, llm=llm)
    assert isinstance(profile, TravelerGroupProfile)
    # Fallback derives interests from the travelers (Alice → hiking), not the
    # LLM's (invalid) response.
    assert "hiking" in profile.combined_interests


def test_plan_route_parses_llm_json(sample_plan_request):
    # The LLM route must cover the start, the required stop (Yosemite), and the
    # end, or the coverage guard substitutes the fallback.
    llm = _FakeLLM(
        '{"ordered_stops": [{"location": "San Francisco, CA", "stop_type": "start"},'
        ' {"location": "Yosemite", "stop_type": "destination"},'
        ' {"location": "Los Angeles, CA", "stop_type": "end"}],'
        ' "route_summary": "coastal", "suggested_total_days": 3}'
    )
    route = rtp_pipeline.plan_route(sample_plan_request.trip, TravelerGroupProfile(), llm=llm)
    assert isinstance(route, RoutePlan)
    assert route.route_summary == "coastal"
    assert route.ordered_stops[0].location == "San Francisco, CA"


def test_plan_route_falls_back_on_bad_json(sample_plan_request):
    route = rtp_pipeline.plan_route(
        sample_plan_request.trip, TravelerGroupProfile(), llm=_FakeLLM("x")
    )
    assert isinstance(route, RoutePlan)
    locations = [s.location for s in route.ordered_stops]
    assert "San Francisco, CA" in locations
    assert "Yosemite" in locations


def test_plan_route_falls_back_on_schema_invalid_stop(sample_plan_request):
    # Syntactically valid JSON with a schema-invalid stop (wrong field type) must
    # also fall back — a pydantic ValidationError here must not bypass the
    # documented fallback contract and crash the step.
    llm = _FakeLLM(
        '{"ordered_stops": [{"location": "SF", "recommended_nights": "not-a-number"}],'
        ' "route_summary": "coastal"}'
    )
    route = rtp_pipeline.plan_route(sample_plan_request.trip, TravelerGroupProfile(), llm=llm)
    assert isinstance(route, RoutePlan)
    locations = [s.location for s in route.ordered_stops]
    assert "San Francisco, CA" in locations
    assert "Yosemite" in locations
    assert route.route_summary == ""  # fallback route, not the LLM's "coastal" summary


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


def test_plan_route_falls_back_when_endpoint_missing(sample_plan_request):
    # A route covering every required stop but truncating the actual start or end
    # location must also be replaced by the fallback — the origin/destination and
    # driving legs can't silently disappear even when required_stops is satisfied.
    llm = _FakeLLM(
        '{"ordered_stops": [{"location": "Yosemite", "stop_type": "destination"}],'
        ' "route_summary": "truncated", "suggested_total_days": 2}'
    )
    route = rtp_pipeline.plan_route(sample_plan_request.trip, TravelerGroupProfile(), llm=llm)
    locations = [s.location for s in route.ordered_stops]
    assert "San Francisco, CA" in locations  # fallback restored the start
    assert "Los Angeles, CA" in locations  # fallback restored the end
    assert route.route_summary == ""  # fallback route, not the LLM's "truncated" summary


def test_plan_route_rejects_short_substring_false_positive():
    # A required stop "LA" must not be considered "covered" just because the
    # bare substring "la" appears mid-word inside an unrelated planned stop
    # like "Atlanta, GA" (previously: raw substring containment treated this
    # as a match, silently dropping the real required stop from the route).
    trip = TripRequest(
        start_location="Chicago, IL",
        required_stops=["LA"],
        end_location="Chicago, IL",
        travelers=[{"name": "Alice"}],
    )
    llm = _FakeLLM(
        '{"ordered_stops": [{"location": "Chicago, IL", "stop_type": "start"},'
        ' {"location": "Atlanta, GA", "stop_type": "destination"},'
        ' {"location": "Chicago, IL", "stop_type": "end"}],'
        ' "route_summary": "wrong", "suggested_total_days": 3}'
    )
    route = rtp_pipeline.plan_route(trip, TravelerGroupProfile(), llm=llm)
    locations = [s.location for s in route.ordered_stops]
    assert "LA" in locations  # fallback restored the real required stop
    assert route.route_summary == ""  # fallback route, not the LLM's "wrong" summary


def test_plan_route_accepts_initials_abbreviation():
    # A required stop "SF" must be accepted against a planned "San Francisco, CA"
    # via initials matching, without unnecessarily discarding a valid LLM route.
    trip = TripRequest(
        start_location="LA",
        required_stops=["SF"],
        end_location="LA",
        travelers=[{"name": "Alice"}],
    )
    llm = _FakeLLM(
        '{"ordered_stops": [{"location": "LA", "stop_type": "start"},'
        ' {"location": "San Francisco, CA", "stop_type": "destination"},'
        ' {"location": "LA", "stop_type": "end"}],'
        ' "route_summary": "coastal", "suggested_total_days": 3}'
    )
    route = rtp_pipeline.plan_route(trip, TravelerGroupProfile(), llm=llm)
    assert route.route_summary == "coastal"  # LLM route accepted, no fallback
    locations = [s.location for s in route.ordered_stops]
    assert "San Francisco, CA" in locations


def test_plan_route_ignores_blank_required_stop(sample_plan_request):
    # A blank entry in required_stops carries no location to verify — it must
    # be skipped rather than change whether the route is accepted or falls back.
    trip = TripRequest(
        start_location="San Francisco, CA",
        required_stops=["Yosemite", ""],
        end_location="Los Angeles, CA",
        travelers=[{"name": "Alice"}],
    )
    llm = _FakeLLM(
        '{"ordered_stops": [{"location": "San Francisco, CA", "stop_type": "start"},'
        ' {"location": "Yosemite", "stop_type": "destination"},'
        ' {"location": "Los Angeles, CA", "stop_type": "end"}],'
        ' "route_summary": "coastal", "suggested_total_days": 3}'
    )
    route = rtp_pipeline.plan_route(trip, TravelerGroupProfile(), llm=llm)
    assert route.route_summary == "coastal"  # accepted despite the blank entry


def test_plan_route_normalizes_zero_suggested_total_days(sample_plan_request):
    # An LLM response with an explicit suggested_total_days: 0 must be
    # normalized to >= 1 — dict.get's default only applies when the key is
    # missing, not when it's present-but-falsy.
    llm = _FakeLLM(
        '{"ordered_stops": [{"location": "San Francisco, CA", "stop_type": "start"},'
        ' {"location": "Yosemite", "stop_type": "destination"},'
        ' {"location": "Los Angeles, CA", "stop_type": "end"}],'
        ' "route_summary": "coastal", "suggested_total_days": 0}'
    )
    route = rtp_pipeline.plan_route(sample_plan_request.trip, TravelerGroupProfile(), llm=llm)
    assert route.suggested_total_days >= 1


def test_plan_route_normalizes_numeric_string_suggested_total_days(sample_plan_request):
    # An LLM response with suggested_total_days as a numeric string (e.g. "3")
    # must not crash — max() compares raw types and raises TypeError on int
    # vs str, even though pydantic would happily coerce the same string for an
    # int field. Must parse it rather than pass it through unguarded.
    llm = _FakeLLM(
        '{"ordered_stops": [{"location": "San Francisco, CA", "stop_type": "start"},'
        ' {"location": "Yosemite", "stop_type": "destination"},'
        ' {"location": "Los Angeles, CA", "stop_type": "end"}],'
        ' "route_summary": "coastal", "suggested_total_days": "3"}'
    )
    route = rtp_pipeline.plan_route(sample_plan_request.trip, TravelerGroupProfile(), llm=llm)
    assert route.suggested_total_days == 3
    assert route.route_summary == "coastal"  # accepted, not the fallback route


def test_plan_route_falls_back_to_default_on_non_numeric_suggested_total_days(
    sample_plan_request,
):
    # A non-numeric, non-coercible suggested_total_days (e.g. a stray word)
    # must degrade to the same default dict.get would use for a missing key,
    # not crash the whole route step.
    llm = _FakeLLM(
        '{"ordered_stops": [{"location": "San Francisco, CA", "stop_type": "start"},'
        ' {"location": "Yosemite", "stop_type": "destination"},'
        ' {"location": "Los Angeles, CA", "stop_type": "end"}],'
        ' "route_summary": "coastal", "suggested_total_days": "a few"}'
    )
    route = rtp_pipeline.plan_route(sample_plan_request.trip, TravelerGroupProfile(), llm=llm)
    assert route.suggested_total_days == sample_plan_request.trip.trip_duration_days
    assert route.route_summary == "coastal"  # accepted, not the fallback route


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


def test_fallback_route_never_yields_zero_days():
    # No explicit duration and no required stops (a minimal start→end trip, the
    # only fields the API requires) must not degrade suggested_total_days to 0.
    trip = TripRequest(start_location="San Francisco, CA", travelers=[{"name": "Alice"}])
    route = rtp_pipeline.plan_route(trip, TravelerGroupProfile(), llm=_FakeLLM("x"))
    assert route.suggested_total_days >= 1


def test_run_pipeline_propagates_step_failure(monkeypatch):
    # Once every specialist agent provably degrades internally (never raises),
    # an exception escaping a step is a genuine bug, not a normal degraded-LLM
    # outcome — run_pipeline no longer has an outer except to swallow it (see
    # its own docstring). run_plan_background (thread mode's actual entry
    # point) owns converting this into a FAILED job; see
    # test_run_plan_background_marks_failed_when_a_step_raises.
    trip = TripRequest(start_location="San Francisco, CA", travelers=[{"name": "Alice"}])
    from road_trip_planning_team.models import PlanTripRequest

    def _boom(_trip):
        raise ValueError("boom")

    monkeypatch.setattr(rtp_pipeline, "profile_travelers", _boom)

    with pytest.raises(ValueError, match="boom"):
        rtp_pipeline.run_pipeline(PlanTripRequest(trip=trip))


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


def test_recommend_activities_raises_on_empty_ordered_stops(sample_plan_request):
    # ActivitiesExpertAgent.run's documented precondition (non-empty
    # route.ordered_stops) is enforced with an explicit raise, not silently
    # tolerated — RoutePlannerAgent guarantees this never happens in normal
    # operation, so a violation here is a genuine caller-contract bug.
    with pytest.raises(ValueError, match="non-empty route.ordered_stops"):
        rtp_pipeline.recommend_activities(
            RoutePlan(ordered_stops=[]),
            TravelerGroupProfile(),
            sample_plan_request.trip,
            llm=_FakeLLM("{}"),
        )


def test_recommend_activities_falls_back_on_schema_invalid_json(sample_plan_request):
    # Syntactically valid JSON with a schema-invalid field (a string where tips
    # expects a list) must also fall back — a pydantic ValidationError from
    # StopActivities construction must not bypass the per-stop fallback.
    route = RoutePlan(ordered_stops=[RouteStop(location="Yosemite", recommended_nights=1)])
    llm = _FakeLLM('{"activities": [], "dining": [], "tips": "not-a-list"}')
    result = rtp_pipeline.recommend_activities(
        route, TravelerGroupProfile(), sample_plan_request.trip, llm=llm
    )
    assert result[0].location == "Yosemite"
    assert result[0].activities == []  # degraded fallback, not a raised error


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


def test_plan_logistics_falls_back_on_schema_invalid_json(sample_plan_request):
    # Syntactically valid JSON with a schema-invalid field (a string where
    # packing_suggestions expects a list) must also fall back — a pydantic
    # ValidationError from LogisticsPlan construction must not bypass it.
    llm = _FakeLLM('{"packing_suggestions": "not-a-list", "budget_estimate": "$1"}')
    logistics = rtp_pipeline.plan_logistics(
        RoutePlan(), TravelerGroupProfile(), sample_plan_request.trip, llm=llm
    )
    assert isinstance(logistics, LogisticsPlan)
    assert logistics.packing_suggestions  # degraded fallback list, not "$1" budget
    assert logistics.budget_estimate != "$1"


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


def test_compose_itinerary_falls_back_on_schema_invalid_json(sample_plan_request):
    # Syntactically valid JSON with a schema-invalid field (a string where
    # day_tips expects a list) must also fall back — a pydantic ValidationError
    # from DayPlan construction must not bypass the fallback.
    route = RoutePlan(
        ordered_stops=[RouteStop(location="Yosemite", recommended_nights=1)], suggested_total_days=2
    )
    llm = _FakeLLM(
        json.dumps({"title": "Bad", "days": [{"day_number": 1, "day_tips": "not-a-list"}]})
    )
    itinerary = rtp_pipeline.compose_itinerary(
        sample_plan_request.trip,
        TravelerGroupProfile(),
        route,
        [StopActivities(location="Yosemite")],
        LogisticsPlan(travel_tips=["t"]),
        llm=llm,
    )
    assert isinstance(itinerary, TripItinerary)
    assert itinerary.title == "Road Trip Itinerary"  # fallback title, not the LLM's "Bad"
    assert itinerary.total_days == 2  # derived from route.suggested_total_days


def test_compose_itinerary_synthesizes_a_day_when_llm_returns_empty_days(sample_plan_request):
    # A valid LLM response with an empty "days" array must not slip through as
    # total_days >= 1 with no days to show — _ensure_nonempty_days normalizes
    # this on the success path too, not just the fallback path.
    route = RoutePlan(
        ordered_stops=[RouteStop(location="Yosemite", recommended_nights=1)], suggested_total_days=2
    )
    llm = _FakeLLM(json.dumps({"title": "Trip", "total_days": 2, "days": []}))
    itinerary = rtp_pipeline.compose_itinerary(
        sample_plan_request.trip,
        TravelerGroupProfile(),
        route,
        [StopActivities(location="Yosemite")],
        LogisticsPlan(),
        llm=llm,
    )
    assert itinerary.title == "Trip"  # LLM response otherwise accepted, not a fallback
    assert itinerary.total_days == 2
    assert len(itinerary.days) == 1
    assert itinerary.days[0].location == "Yosemite"


def test_compose_itinerary_aligns_activities_positionally_for_duplicate_locations(
    sample_plan_request,
):
    # Two stops sharing the same location name (e.g. two "Springfield" stops)
    # must each keep their own activities — a location-keyed lookup would
    # misattribute both to whichever StopActivities came first in the list.
    route = RoutePlan(
        ordered_stops=[
            RouteStop(location="Springfield", stop_type="destination", recommended_nights=1),
            RouteStop(location="Springfield", stop_type="destination", recommended_nights=1),
        ],
        suggested_total_days=2,
    )
    activities_per_stop = [
        StopActivities(location="Springfield", activities=[{"name": "First Springfield stop"}]),
        StopActivities(location="Springfield", activities=[{"name": "Second Springfield stop"}]),
    ]
    captured_prompts: list[str] = []

    class _CapturingLLM:
        def __call__(self, prompt: str) -> str:
            captured_prompts.append(prompt)
            return "not json"  # force fallback; only the captured prompt matters

    rtp_pipeline.compose_itinerary(
        sample_plan_request.trip,
        TravelerGroupProfile(),
        route,
        activities_per_stop,
        LogisticsPlan(),
        llm=_CapturingLLM(),
    )
    prompt = captured_prompts[0]
    # A location-keyed lookup would have attributed "First Springfield stop" to
    # both entries and never surfaced "Second Springfield stop" at all.
    first_idx = prompt.index("First Springfield stop")
    second_idx = prompt.index("Second Springfield stop")
    assert first_idx < second_idx


def test_compose_itinerary_fallback_synthesizes_a_day_for_pass_through_only_route(
    sample_plan_request,
):
    # A start-only trip (no required stops) whose route fallback produced only
    # pass-through start/end stops, combined with the composer's own LLM also
    # failing, must not yield days=[] while total_days >= 1.
    route = RoutePlan(
        ordered_stops=[
            RouteStop(location="San Francisco, CA", stop_type="start", recommended_nights=0),
            RouteStop(location="San Francisco, CA", stop_type="end", recommended_nights=0),
        ],
        suggested_total_days=1,
    )
    itinerary = rtp_pipeline.compose_itinerary(
        sample_plan_request.trip,
        TravelerGroupProfile(),
        route,
        [StopActivities(location="San Francisco, CA")],
        LogisticsPlan(),
        llm=_FakeLLM("nope"),
    )
    assert itinerary.total_days == 1
    assert len(itinerary.days) == 1
    assert itinerary.days[0].location == "San Francisco, CA"


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


def test_run_plan_background_marks_failed_when_a_step_raises(monkeypatch, sample_plan_request):
    """A step raising an unexpected error (e.g. a genuine bug, not a normal
    degraded-LLM outcome — every specialist already degrades those internally)
    must still reach a terminal state: it propagates through run_pipeline and
    run_plan_core, and is converted to FAILED at the run_plan_background
    boundary (a daemon thread has no caller to raise to)."""
    create_job("job-step-raises", request={"trip": {}})

    def _boom(_trip):
        raise ValueError("schema-invalid LLM response")

    monkeypatch.setattr(rtp_pipeline, "profile_travelers", _boom)

    rtp_pipeline.run_plan_background("job-step-raises", sample_plan_request)

    job = get_job("job-step-raises")
    assert job["status"] == JOB_STATUS_FAILED
    assert "schema-invalid LLM response" in (job.get("error") or "")


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
