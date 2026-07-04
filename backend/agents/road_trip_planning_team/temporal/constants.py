"""Temporal task queue and workflow IDs for the Road Trip Planning team.

Invariants:
    - ``TASK_QUEUE`` is a non-empty string; the workflow dispatcher and the
      worker must agree on it, so both read it from here. It matches the
      ``f"{team}-queue"`` convention used by
      ``shared_temporal.teams_registry.start_all_team_workers``.
"""

TASK_QUEUE = "road_trip_planning-queue"
WORKFLOW_ID_PREFIX = "road-trip-planning-"
