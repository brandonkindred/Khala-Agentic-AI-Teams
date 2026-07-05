"""Unit tests for the neutral pipeline core.

Covers the graph-output translation (`_translate_itinerary_keys`) and the
`run_pipeline` parse + fallback paths directly — the rest of the suite stubs
`run_pipeline`, so this is where the key-translation logic (the most bug-prone
part of the module) is exercised. Pure functions + a mocked graph, so no job
service or Postgres is needed.
"""

from __future__ import annotations

import json

from road_trip_planning_team import pipeline as rtp_pipeline
from road_trip_planning_team.models import TripItinerary


def test_translate_top_level_and_route_summary_renames():
    data = {"summary": "A great trip", "packing_list": ["boots"], "route_summary": "SF → LA"}
    out = rtp_pipeline._translate_itinerary_keys(data)
    assert out["overview"] == "A great trip"
    assert "summary" not in out
    assert out["packing_suggestions"] == ["boots"]
    assert out["route_summary"] == ["SF → LA"]  # str coerced to list


def test_translate_does_not_overwrite_model_native_keys():
    data = {"summary": "graph", "overview": "native"}
    out = rtp_pipeline._translate_itinerary_keys(data)
    # Model-native key wins and the graph-native alias is left untouched (the
    # rename is guarded on the target key being absent).
    assert out["overview"] == "native"
    assert out["summary"] == "graph"


def test_translate_per_day_driving_activities_meals_accommodation():
    data = {
        "days": [
            {
                "date_label": "Day 1",
                "day_notes": "scenic",
                "driving": {"from_location": "SF", "miles": 180, "hours": 3.5},
                "activities": [
                    {"time": "morning", "name": "Coffee"},
                    {"time": "afternoon", "name": "Hike"},
                    {"time": "dinner", "name": "Tacos"},
                    {"name": "Unspecified"},
                ],
                "meals": [{"venue": "Diner", "notes": "cheap", "meal_type": "breakfast"}],
                "accommodation": {"type": "hotel", "notes": "book early"},
            },
            "not-a-dict",  # skipped without error
        ]
    }
    out = rtp_pipeline._translate_itinerary_keys(data)
    day = out["days"][0]
    assert day["date"] == "Day 1"
    assert day["day_summary"] == "scenic"
    assert day["driving_from"] == "SF"
    assert day["driving_distance_miles"] == 180
    assert day["driving_time_hours"] == 3.5
    assert [a["name"] for a in day["morning_activities"]] == ["Coffee"]
    assert [a["name"] for a in day["afternoon_activities"]] == ["Hike", "Unspecified"]
    assert [a["name"] for a in day["evening_activities"]] == ["Tacos"]
    assert day["meals"][0] == {
        "name": "Diner",
        "description": "cheap",
        "activity_type": "breakfast",
    }
    assert day["accommodation"]["accommodation_type"] == "hotel"
    assert day["accommodation"]["booking_tips"] == "book early"


def test_run_pipeline_parses_translated_graph_output(monkeypatch, sample_plan_request):
    """run_pipeline extracts + translates a successful graph run into a TripItinerary.

    Mock contract: build_trip_graph/invoke_graph_sync are stubbed to opaque
    objects (their identity is irrelevant), and extract_node_text returns the
    composer node's text — prose wrapping a valid itinerary JSON object — which
    run_pipeline must slice, json.loads, key-translate, and validate.
    """
    composer_json = json.dumps(
        {
            "title": "SF to LA",
            "summary": "Coastal cruise",
            "total_days": 2,
            "days": [
                {"date_label": "Day 1", "activities": [{"time": "morning", "name": "Coffee"}]}
            ],
        }
    )
    monkeypatch.setattr(rtp_pipeline, "build_trip_graph", lambda: object())
    monkeypatch.setattr(rtp_pipeline, "invoke_graph_sync", lambda graph, task: object())
    monkeypatch.setattr(
        rtp_pipeline, "extract_node_text", lambda result, node_id: f"prose... {composer_json}"
    )

    itinerary = rtp_pipeline.run_pipeline(sample_plan_request)

    assert isinstance(itinerary, TripItinerary)
    assert itinerary.title == "SF to LA"
    assert itinerary.overview == "Coastal cruise"
    assert itinerary.days[0].date == "Day 1"
    assert itinerary.days[0].morning_activities[0].name == "Coffee"


def test_run_pipeline_falls_back_when_output_unparseable(monkeypatch, sample_plan_request):
    """run_pipeline returns a minimal fallback itinerary when the graph output
    has no parseable JSON.

    Mock contract: extract_node_text returns text with no ``{`` so the parse
    branch is skipped and the fallback (title/overview/total_days derived from
    the request) is returned instead of raising.
    """
    monkeypatch.setattr(rtp_pipeline, "build_trip_graph", lambda: object())
    monkeypatch.setattr(rtp_pipeline, "invoke_graph_sync", lambda graph, task: object())
    monkeypatch.setattr(rtp_pipeline, "extract_node_text", lambda result, node_id: "no json here")

    itinerary = rtp_pipeline.run_pipeline(sample_plan_request)

    assert isinstance(itinerary, TripItinerary)
    assert itinerary.title == "Road Trip: San Francisco, CA to Los Angeles, CA"
    assert "parsing failed" in itinerary.overview
    assert itinerary.total_days == 2
