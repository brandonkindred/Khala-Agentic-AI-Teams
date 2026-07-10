"""Temporal workflow + activities for the deepthought team (shared_temporal Pattern A).

The reasoning pipeline is decomposed into one ``@activity.defn`` per LLM boundary
(classify strategy, analyse, force-direct-answer, deliberate, synthesise) plus the
job-store transition activities; ``DeepthoughtWorkflow`` (:mod:`.workflows`) drives
the recursion deterministically and calls those activities. See ``activities.py``
and ``workflows.py``.

Pattern A: importing this package only *binds* the workflow class and activity
functions into the ``WORKFLOWS`` / ``ACTIVITIES`` lists (and re-exports the
task-queue constants) — it has NO side effects. Worker startup is a separate
explicit step in ``deepthought.temporal.worker``, invoked by the team_service
entrypoint via ``TEAM_TEMPORAL_WORKER_MODULE`` / ``TEAM_TEMPORAL_WORKER_FUNC``.

``run_pipeline_activity`` (the pre-decomposition whole-pipeline activity) is kept
in ``ACTIVITIES`` so ``DeepthoughtWorkflow`` histories started before the
decomposition still replay via ``workflow.patched``.
"""

from __future__ import annotations

from deepthought.temporal.activities import (
    ALL_ACTIVITIES,
    analyse_activity,
    classify_strategy_activity,
    deliberate_activity,
    finalize_job_activity,
    force_direct_answer_activity,
    run_pipeline_activity,
    start_job_activity,
    synthesise_activity,
)
from deepthought.temporal.constants import TASK_QUEUE, WORKFLOW_ID_PREFIX
from deepthought.temporal.workflows import DeepthoughtWorkflow

WORKFLOWS = [DeepthoughtWorkflow]
ACTIVITIES = ALL_ACTIVITIES

__all__ = [
    "ACTIVITIES",
    "DeepthoughtWorkflow",
    "TASK_QUEUE",
    "WORKFLOWS",
    "WORKFLOW_ID_PREFIX",
    "analyse_activity",
    "classify_strategy_activity",
    "deliberate_activity",
    "finalize_job_activity",
    "force_direct_answer_activity",
    "run_pipeline_activity",
    "start_job_activity",
    "synthesise_activity",
]
