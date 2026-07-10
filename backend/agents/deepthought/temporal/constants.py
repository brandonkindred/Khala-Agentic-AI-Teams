"""Constants for the deepthought Temporal decomposition.

Pulled out of ``temporal/__init__.py`` so both the workflow and the activities
modules can import them without a circular dependency, and so the activity
names / retry policies / timeouts live in one place (blogging/SE-style).
"""

from __future__ import annotations

from datetime import timedelta
from types import MappingProxyType

from temporalio.common import RetryPolicy

TASK_QUEUE = "deepthought-queue"
WORKFLOW_ID_PREFIX = "deepthought-"

# ``workflow.patched`` marker gating the per-step decomposition. Histories
# started before this change (a single ``run_pipeline_activity`` call) must keep
# replaying through the legacy path; only new runs take the decomposed path.
DECOMPOSED_PIPELINE_PATCH = "deepthought-decomposed-pipeline"

# Stable activity names (identifiers surfaced in Temporal history / UI).
CLASSIFY_STRATEGY_ACTIVITY = "deepthought_classify_strategy"
ANALYSE_ACTIVITY = "deepthought_analyse"
FORCE_DIRECT_ANSWER_ACTIVITY = "deepthought_force_direct_answer"
DELIBERATE_ACTIVITY = "deepthought_deliberate"
SYNTHESISE_ACTIVITY = "deepthought_synthesise"
START_JOB_ACTIVITY = "deepthought_start_job"
FINALIZE_JOB_ACTIVITY = "deepthought_finalize_job"
# Legacy single-activity name — unchanged so ``workflow.patched`` replay of
# in-flight histories still resolves it.
RUN_PIPELINE_ACTIVITY = "deepthought_run_pipeline"

# One attempt for the LLM reasoning activities: each already degrades gracefully
# on an LLM error (it returns a fallback rather than raising), so a genuine raise
# is a non-transient bug — retrying would only re-charge LLM cost. Worker/process
# loss still reschedules an *incomplete* attempt (the durability benefit).
_LLM_RETRY_POLICY = RetryPolicy(maximum_attempts=1)

# Job-store transitions are idempotent status writes over HTTP, so a small
# bounded retry rides out a transient job-service blip.
_JOB_RETRY_POLICY = RetryPolicy(maximum_attempts=3, initial_interval=timedelta(seconds=1))

# One attempt for the legacy whole-pipeline activity (matches the pre-decomposition
# policy: it already records FAILED itself, and re-running is expensive).
RUN_PIPELINE_RETRY_POLICY = RetryPolicy(maximum_attempts=1)

# Shared, immutable ``execute_activity`` option bundles (unpacked with ``**``).
# A single LLM reasoning call — analysis/synthesis with thinking enabled can be
# slow, so the per-call ceiling is generous; the workflow bounds the whole run
# by summing these.
LLM_ACTIVITY_OPTS = MappingProxyType(
    {
        "start_to_close_timeout": timedelta(minutes=10),
        "retry_policy": _LLM_RETRY_POLICY,
    }
)

# Job-store writes are quick; short timeout with a bounded retry.
JOB_ACTIVITY_OPTS = MappingProxyType(
    {
        "start_to_close_timeout": timedelta(seconds=30),
        "retry_policy": _JOB_RETRY_POLICY,
    }
)

# The legacy whole-pipeline activity can run the entire recursive tree.
RUN_PIPELINE_TIMEOUT = timedelta(hours=1)
