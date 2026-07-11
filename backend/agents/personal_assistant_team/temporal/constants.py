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

The team is intentionally NOT registered in
``shared_temporal.teams_registry.TEAM_TEMPORAL_MODULES``: its
``start_all_team_workers`` would poll ``f"{team}-queue"`` = ``personal_assistant-queue``,
a DIFFERENT queue from this one, causing a split-brain. PA boots its worker via
its own docker-compose hook (``TEAM_TEMPORAL_WORKER_MODULE`` /
``TEAM_TEMPORAL_WORKER_FUNC``) on this queue instead.
"""

TASK_QUEUE = "personal-assistant"
WORKFLOW_ID_PREFIX_ASSISTANT = "pa-assistant-"
