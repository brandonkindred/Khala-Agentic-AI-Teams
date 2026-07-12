"""Route Planner Agent: plans the optimal road trip route through required stops."""

from __future__ import annotations

import json
import logging
import re

from strands import Agent

from llm_service import get_strands_model

from ...models import RoutePlan, RouteStop, TravelerGroupProfile, TripRequest

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are an expert road trip route planner with deep knowledge of North American geography and highways.
Given a trip request (start, required stops, end, duration, vehicle type), plan the optimal ordered route.

You may add intermediate overnight stops if the driving distances are too long for one day (aim for max 5-6 hours driving per day).

Output JSON with:
- ordered_stops: array of stop objects, each with:
  - location: string (city, state or landmark name)
  - driving_from: string or null (previous location)
  - estimated_driving_miles: number or null
  - estimated_driving_hours: number or null
  - recommended_nights: integer (nights to stay, 0 for pass-through stops)
  - stop_type: string — one of: "start", "destination", "overnight", "landmark", "end"
  - notes: string (why this stop, what makes it special, any route notes)
- total_driving_miles: number or null
- total_driving_hours: number or null
- route_summary: string (brief narrative of the overall route)
- suggested_total_days: integer

Make the route logical geographically. Consider scenic byways if the group prefers scenic routes.
Output only valid JSON."""


class RoutePlannerAgent:
    """Plans the optimal ordered route for a road trip."""

    def __init__(self, llm=None) -> None:
        self._agent = (
            llm
            if llm is not None
            else Agent(
                model=get_strands_model("road_trip_planning"),
                system_prompt=SYSTEM_PROMPT,
            )
        )

    def run(self, trip: TripRequest, group_profile: TravelerGroupProfile) -> RoutePlan:
        """Generate an ordered route plan from the trip request.

        Postconditions:
            - The returned ``RoutePlan`` always has a non-empty ``ordered_stops``
              covering start → required stops → end. If the LLM output can't be
              parsed, or is valid JSON that yields no usable stops (e.g. ``{}``
              from a refusal), a derived fallback route is returned so a request
              with required stops never composes into an empty itinerary.
        """
        end = trip.end_location or trip.start_location
        prompt = (
            f"Start: {trip.start_location}\n"
            f"Required stops (in any order): {', '.join(trip.required_stops) or 'none'}\n"
            f"End: {end}\n"
            f"Trip duration: {trip.trip_duration_days or 'flexible'} days\n"
            f"Vehicle: {trip.vehicle_type}\n"
            f"Group activity pace: {group_profile.activity_pace}\n"
            f"Travel preferences: {', '.join(trip.preferences) or 'none'}\n"
            f"Group needs: {', '.join(group_profile.combined_needs) or 'none'}\n\n"
            "Plan the optimal road trip route. Include all required stops. "
            "Add intermediate overnight stops as needed. Output the route JSON."
        )

        try:
            result = self._agent(prompt)
            raw = str(result).strip()
            data = json.loads(raw)
            raw_stops = data.get("ordered_stops") or []
            stops = [RouteStop.model_validate(s) for s in raw_stops if isinstance(s, dict)]
        except Exception as e:
            # Covers both a malformed LLM response (JSON parse failure) and a
            # syntactically-valid-but-schema-invalid stop (pydantic ValidationError)
            # — either way the postcondition below holds: fall back rather than raise.
            logger.warning("RoutePlannerAgent JSON parse/validation failed: %s", e)
            return self._fallback_route(trip, end)

        # A stop with a blank location is unusable (nothing for the activities/
        # composer steps to call the LLM or build a day for) — drop it before
        # any of the checks below rather than let it ride through to the
        # accepted route just because the *other* stops satisfy coverage.
        stops = [s for s in stops if s.location and s.location.strip()]

        # The prompt asks the LLM to set recommended_nights=0 and
        # stop_type="start"/"end" for the boundary stops, but doesn't enforce
        # either — an LLM response that omits recommended_nights for an
        # endpoint defaults to RouteStop's own default of 1, and downstream
        # endpoint skipping (ActivitiesExpertAgent, _build_fallback_itinerary)
        # requires *both* recommended_nights==0 and stop_type in
        # ("start", "end") to treat a stop as pass-through — normalizing only
        # nights would leave a mislabeled endpoint (e.g. still "destination")
        # generating a real activities LLM call and itinerary day. Normalize
        # both by *position* (the first/last stop), not the existing
        # stop_type label — the LLM can mislabel an interior stop's stop_type
        # as "start"/"end" while the actual boundary stops are elsewhere in
        # the list, and keying off the label would then wrongly zero out a
        # real must-visit stop's nights.
        if stops:
            stops[0].recommended_nights = 0
            stops[0].stop_type = "start"
            stops[-1].recommended_nights = 0
            stops[-1].stop_type = "end"

        if not stops or not self._covers_required_stops(stops, trip, end):
            # Valid JSON that yields no usable stops (e.g. an empty object from a
            # refusal), or that silently drops the start, the end, or a must-visit
            # stop — fall back to a route covering start → required stops → end
            # rather than composing an itinerary that is empty or missing a
            # requested location.
            logger.warning("RoutePlannerAgent route missing required stops; using fallback route")
            return self._fallback_route(trip, end)

        raw_days = data.get("suggested_total_days", trip.trip_duration_days or 7)
        try:
            # dict.get's default only applies when the key is missing, not when
            # it's present-but-non-numeric (e.g. a numeric string like "3", which
            # pydantic would coerce for an int field but max() won't — comparing
            # int and str raises TypeError). Normalize before clamping.
            parsed_days = int(raw_days)
        except (TypeError, ValueError):
            parsed_days = trip.trip_duration_days or 7

        try:
            return RoutePlan(
                ordered_stops=stops,
                total_driving_miles=data.get("total_driving_miles"),
                total_driving_hours=data.get("total_driving_hours"),
                route_summary=data.get("route_summary", ""),
                # max(1, ...): the parsed/defaulted value could still be 0 or negative.
                suggested_total_days=max(1, parsed_days),
            )
        except Exception as e:
            # A schema-invalid top-level field (e.g. route_summary returned as
            # a list instead of a string) raises pydantic.ValidationError here
            # even though ordered_stops itself validated and covers every
            # required stop — fall back rather than let a good route get
            # discarded by a bad sibling field.
            logger.warning("RoutePlannerAgent RoutePlan validation failed: %s", e)
            return self._fallback_route(trip, end)

    def _covers_required_stops(self, stops: list[RouteStop], trip: TripRequest, end: str) -> bool:
        """Return True if the route is bounded by start/end and covers every required stop.

        Matching (see ``_locations_match``) tolerates the LLM naming a stop more
        or less verbosely than the request (e.g. a required ``"Yosemite"``
        matches a planned ``"Yosemite National Park"``), or a short caller
        abbreviation (e.g. ``"SF"`` matching ``"San Francisco, CA"``), without
        the false-positive risk of raw substring containment on short strings
        (e.g. ``"LA"`` is a literal substring of ``"Atlanta, GA"``). Blank
        required-stop entries are skipped rather than trivially "covered".

        The start and end are checked *positionally* — the start must match
        the route's first stop and the end its last — rather than merely
        appearing somewhere in the route. A pure membership check (matching
        every required location anywhere in ``stops``) can't distinguish a
        correctly-bounded route from one that includes the right locations in
        the wrong order, or a round trip (``end == start``) that never
        actually returns to the start. Required stops themselves may appear
        in any order (the prompt allows it), so they're still checked by
        membership.

        Preconditions:
            - ``stops`` is the parsed, non-empty route.
            - ``end`` is the resolved end location (``trip.end_location`` or the
              start location for a round trip).

        Postconditions:
            - Returns True when the first stop matches the start, the last
              stop matches the end, and every non-blank required stop is
              present anywhere in the route.
        """
        planned = [s.location.lower() for s in stops if s.location]
        if not planned:
            return False

        start_r = (trip.start_location or "").strip().lower()
        if start_r and not self._locations_match(start_r, planned[0]):
            return False

        end_r = (end or "").strip().lower()
        if end_r and not self._locations_match(end_r, planned[-1]):
            return False

        for req in trip.required_stops:
            r = (req or "").strip().lower()
            if not r:
                continue  # no location to verify against the planned route
            if not any(self._locations_match(r, p) for p in planned):
                return False
        return True

    @staticmethod
    def _locations_match(a: str, b: str) -> bool:
        """Best-effort, already-lowercased location match — more robust than
        raw substring containment without needing a geocoding/gazetteer lookup.

        Matches when the shorter string appears in the longer one at a word
        boundary (so ``"yosemite"`` matches ``"yosemite national park"`` and
        ``"san francisco"`` matches ``"san francisco, ca"``, but the bare
        substring ``"la"`` no longer wrongly matches inside ``"atlanta, ga"``
        since it's embedded mid-word there, not word-bounded), or when the
        shorter string equals the initials of the longer string's words
        (state-code-length tokens excluded, so ``"sf"`` matches
        ``"san francisco, ca"`` via "San Francisco" without "CA" corrupting the
        acronym).

        Preconditions:
            - ``a``, ``b`` are already lowercased, non-empty.

        Postconditions:
            - Returns True on a word-bounded containment or initials match,
              False otherwise.
        """
        shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
        if re.search(rf"\b{re.escape(shorter)}\b", longer):
            return True
        words = [w for w in re.findall(r"[a-z0-9]+", longer) if len(w) > 2]
        return len(shorter) > 1 and shorter == "".join(w[0] for w in words)

    def _fallback_route(self, trip: TripRequest, end: str) -> RoutePlan:
        """Build a minimal route covering start → required stops → end.

        Preconditions:
            - ``end`` is the resolved end location (``trip.end_location`` or the
              start location for a round trip).

        Postconditions:
            - Returns a ``RoutePlan`` whose ``ordered_stops`` begins at a
              pass-through start, includes every required stop as an overnight
              destination, and ends at a pass-through end — never empty. The
              start/end carry ``recommended_nights=0`` so the activities and
              composer steps treat them as pass-through and don't turn them into
              extra overnight days or LLM calls. Every stop after the start
              carries ``driving_from`` set to the previous stop's location, so
              the composer's fallback itinerary can show each leg's origin
              instead of an unknown ``driving_from``.
        """
        stops = [RouteStop(location=trip.start_location, stop_type="start", recommended_nights=0)]
        previous = trip.start_location
        for s in trip.required_stops:
            if not s or not s.strip():
                continue  # matches _covers_required_stops's own blank-entry skip
            stops.append(
                RouteStop(
                    location=s,
                    stop_type="destination",
                    recommended_nights=1,
                    driving_from=previous,
                )
            )
            previous = s
        stops.append(
            RouteStop(location=end, stop_type="end", recommended_nights=0, driving_from=previous)
        )
        return RoutePlan(
            ordered_stops=stops,
            # max(1, ...): a trip with no explicit duration and no required stops
            # (start → end only) would otherwise evaluate to 0 days.
            suggested_total_days=max(1, trip.trip_duration_days or len(trip.required_stops) * 2),
        )
