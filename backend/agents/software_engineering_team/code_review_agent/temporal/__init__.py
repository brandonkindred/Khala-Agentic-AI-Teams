"""Temporal workflows and activities for the code review agent.

The code review agent runs in Temporal mode by default (see :mod:`.config`):
``CodeReviewAgent.run`` dispatches the durable ``CodeReviewWorkflow`` and only
falls back to the in-process coordinator when Temporal is explicitly disabled or
unavailable.

Exports ``WORKFLOWS`` / ``ACTIVITIES`` (consumed by ``start_team_worker`` and the
shared teams registry) plus ``TASK_QUEUE`` / ``WORKFLOW_ID_PREFIX``. This module
has **no import-time side effects** (no ``os.getenv``, no worker boot) because the
temporalio sandbox replays it during workflow registration.
"""

from __future__ import annotations

from .activities import (
    filter_false_positives_activity,
    finalize_review_activity,
    prepare_review_activity,
    review_chunk_activity,
    synthesize_findings_activity,
)
from .config import (
    TASK_QUEUE,
    WORKFLOW_ID_PREFIX,
    code_review_temporal_enabled,
    resolve_code_review_temporal_address,
)
from .workflows import CodeReviewWorkflow

WORKFLOWS = [CodeReviewWorkflow]
ACTIVITIES = [
    prepare_review_activity,
    review_chunk_activity,
    filter_false_positives_activity,
    finalize_review_activity,
    synthesize_findings_activity,
]

__all__ = [
    "WORKFLOWS",
    "ACTIVITIES",
    "TASK_QUEUE",
    "WORKFLOW_ID_PREFIX",
    "CodeReviewWorkflow",
    "prepare_review_activity",
    "review_chunk_activity",
    "filter_false_positives_activity",
    "finalize_review_activity",
    "synthesize_findings_activity",
    "code_review_temporal_enabled",
    "resolve_code_review_temporal_address",
]
