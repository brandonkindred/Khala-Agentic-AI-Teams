"""Temporal workflow IDs, task queue, and names for the blogging team."""

import os

TASK_QUEUE = os.getenv("TEMPORAL_TASK_QUEUE_BLOGGING", "blogging").strip()

WORKFLOW_ID_PREFIX_FULL_PIPELINE = "blog-full-pipeline-"

WORKFLOW_FULL_PIPELINE = "BlogFullPipelineWorkflow"

# Per-phase activity names (the pipeline is decomposed into four activities).
ACTIVITY_PLAN_STAGE = "blog_plan_stage"
ACTIVITY_DRAFT_STAGE = "blog_draft_stage"
ACTIVITY_GATES_STAGE = "blog_gates_stage"
ACTIVITY_FINALIZE = "blog_finalize"

# Legacy monolithic activity, still registered so pre-decomposition workflow
# histories can drain out (see the workflow's unpatched replay branch).
ACTIVITY_FULL_PIPELINE = "run_blog_full_pipeline"

# Shared between the workflow's finalize RetryPolicy and finalize_job_activity's
# last-attempt check: the activity re-raises transient errors until this attempt,
# then marks the job failed. Keeping one constant stops the two from drifting.
FINALIZE_MAX_ATTEMPTS = 3
