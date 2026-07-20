"""Temporal task queue and workflow IDs for the personal assistant team.

``TASK_QUEUE`` is the fixed literal ``"personal-assistant"`` — the SAME queue the
team used before the activity decomposition (the previous default of
``os.getenv("TEMPORAL_TASK_QUEUE_PA", "personal-assistant")``). Keeping the queue
name stable is what actually lets pre-decomposition workflow executions drain: a
Temporal execution's workflow/activity tasks stay pinned to the queue it was
started on, so the single PA worker must keep polling this queue for the retained
legacy ``run_pa_assistant`` activity (gated by ``workflow.patched``) to reach
them. Renaming the queue would strand those in-flight executions on the old
queue, defeating the drain.

It is a literal (not read from the environment) because the package ``__init__``
and ``workflows`` import this module, and the temporalio workflow sandbox replays
them during workflow registration — a top-level ``os.getenv`` here would trip the
sandbox. (Dropping the ``TEMPORAL_TASK_QUEUE_PA`` override is safe: docker-compose
never set it, so the effective queue is unchanged.)

The team IS registered in ``shared.temporal.teams_registry.TEAM_TEMPORAL_MODULES``:
``start_all_team_workers`` reads this module's own ``TASK_QUEUE`` export (falling
back to ``f"{team}-queue"`` only when a team doesn't define one), so it correctly
polls ``"personal-assistant"`` rather than deriving a mismatched queue name. PA's
primary boot path remains its own docker-compose hook
(``TEAM_TEMPORAL_WORKER_MODULE`` / ``TEAM_TEMPORAL_WORKER_FUNC``); the registry
entry lets ``start_all_team_workers`` also host PA's worker (e.g. in a
consolidated process) on the SAME queue, safely, since ``start_team_worker`` is
idempotent per team name.

``MAX_CONCURRENT_ACTIVITIES`` is the SAME single source of truth read by both
boot paths, for the same reason ``TASK_QUEUE`` is: ``start_team_worker`` is
idempotent per team name, so whichever caller starts the worker FIRST wins for
the whole process, and a caller that hardcoded a different concurrency value
would silently never take effect. ``temporal/worker.py``'s dedicated hook
imports this constant instead of hardcoding the cap, and
``start_all_team_workers`` reads it via ``getattr(mod, "MAX_CONCURRENT_ACTIVITIES", 4)``
— so both paths agree on 2 regardless of which one wins the startup race.
"""

TASK_QUEUE = "personal-assistant"
WORKFLOW_ID_PREFIX_ASSISTANT = "pa-assistant-"

# Pins the pre-migration cap (the hand-rolled worker this replaced used
# `max_workers=2` / `max_concurrent_activities=2`), rather than leaving it to
# `start_team_worker`'s default of 4. See this module's docstring for why this
# lives here rather than as a literal in `worker.py`.
MAX_CONCURRENT_ACTIVITIES = 2
