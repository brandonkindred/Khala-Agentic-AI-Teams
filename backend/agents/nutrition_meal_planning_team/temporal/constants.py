"""Temporal task queue and workflow IDs for the Nutrition & Meal Planning team.

Invariants:
    - ``TASK_QUEUE`` is a non-empty string; the workflow dispatcher
      (``start_workflow``) and the worker (``worker``) must agree on it, so both
      read it from here. We keep the team's original ``nutrition-meal-planning``
      queue name (rather than the ``f"{team}-queue"`` registry convention) so
      meal-plan workflows already in flight on the previous release's queue are
      still picked up after this migration deploys.
    - No ``os.getenv`` (or any other non-deterministic call) at module scope:
      this module is in the import graph of the workflow package, which the
      temporalio sandbox replays during workflow registration, and the sandbox
      aborts on restricted calls like ``os.getenv``.
"""

TASK_QUEUE = "nutrition-meal-planning"
WORKFLOW_ID_PREFIX = "nutrition-meal-planning-"
