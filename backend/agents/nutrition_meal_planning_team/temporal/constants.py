"""Temporal task queue and workflow IDs for the Nutrition & Meal Planning team.

Invariants:
    - ``TASK_QUEUE`` is a non-empty string; the workflow dispatcher
      (``start_workflow``) and the worker (``worker``) must agree on it, so both
      read it from here. It matches the ``f"{team}-queue"`` convention used by
      ``shared_temporal.teams_registry.start_all_team_workers`` (team slug
      ``nutrition_meal_planning``).
    - No ``os.getenv`` (or any other non-deterministic call) at module scope:
      this module is in the import graph of the workflow package, which the
      temporalio sandbox replays during workflow registration, and the sandbox
      aborts on restricted calls like ``os.getenv``.
"""

TASK_QUEUE = "nutrition_meal_planning-queue"
WORKFLOW_ID_PREFIX = "nutrition-meal-planning-"
