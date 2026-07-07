"""Temporal task queue and workflow IDs for the Startup Advisor team.

Invariants:
    - ``TASK_QUEUE`` is a non-empty string; the workflow dispatcher and the
      worker must agree on it, so both read it from here. It matches the
      ``f"{team}-queue"`` convention used by
      ``shared_temporal.teams_registry.start_all_team_workers``.
"""

TASK_QUEUE = "startup_advisor-queue"
WORKFLOW_ID_PREFIX = "startup-advisor-"
