"""Temporal workflow IDs, task queue, and names for the blogging team."""

import os

TASK_QUEUE = os.getenv("TEMPORAL_TASK_QUEUE_BLOGGING", "blogging").strip()

WORKFLOW_ID_PREFIX_FULL_PIPELINE = "blog-full-pipeline-"

WORKFLOW_FULL_PIPELINE = "BlogFullPipelineWorkflow"
ACTIVITY_FULL_PIPELINE = "run_blog_full_pipeline"
