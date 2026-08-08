"""Shared Temporal scaffolding for all agent teams.

Provides a single place for Temporal client connection, worker boilerplate,
job-backed workflow runner, and generic checkpoint/pause-resume helpers so
every team can adopt durable, resumable job tracking with minimal code.

Public API:
    from shared.temporal import (
        get_temporal_client, is_temporal_enabled, connect_temporal_client,
        start_team_worker, run_team_job,
        save_checkpoint, load_checkpoint, wait_for_input, submit_input,
        json_safe, merge_context, is_final_attempt, fail_job, guarded,
    )
"""

from shared.temporal.activity_helpers import (
    fail_job,
    guarded,
    is_final_attempt,
    json_safe,
    merge_context,
)
from shared.temporal.activity_utils import (
    is_cancelled,
    is_last_attempt,
    raise_if_cancelled,
)
from shared.temporal.checkpoints import (
    load_checkpoint,
    save_checkpoint,
    submit_input,
    wait_for_input,
)
from shared.temporal.client import (
    connect_temporal_client,
    get_temporal_address,
    get_temporal_client,
    get_temporal_loop,
    get_temporal_namespace,
    is_temporal_enabled,
    set_temporal_client,
    set_temporal_loop,
)
from shared.temporal.failure_translation import translate_workflow_failure
from shared.temporal.runner import (
    await_client,
    cancel_workflow_sync,
    execute_workflow_async,
    execute_workflow_sync,
    run_team_job,
    signal_workflow_sync,
    start_workflow_sync,
    terminate_and_await_workflow_sync,
)
from shared.temporal.teams_registry import TEAM_TEMPORAL_MODULES, start_all_team_workers
from shared.temporal.worker import (
    is_team_worker_alive,
    start_team_worker,
    wait_for_team_worker_ready,
)

__all__ = [
    "TEAM_TEMPORAL_MODULES",
    "await_client",
    "cancel_workflow_sync",
    "connect_temporal_client",
    "execute_workflow_async",
    "execute_workflow_sync",
    "fail_job",
    "guarded",
    "signal_workflow_sync",
    "start_all_team_workers",
    "get_temporal_address",
    "get_temporal_client",
    "get_temporal_loop",
    "get_temporal_namespace",
    "is_cancelled",
    "is_final_attempt",
    "is_last_attempt",
    "is_team_worker_alive",
    "is_temporal_enabled",
    "json_safe",
    "load_checkpoint",
    "merge_context",
    "raise_if_cancelled",
    "run_team_job",
    "save_checkpoint",
    "set_temporal_client",
    "set_temporal_loop",
    "start_team_worker",
    "start_workflow_sync",
    "submit_input",
    "terminate_and_await_workflow_sync",
    "translate_workflow_failure",
    "wait_for_input",
    "wait_for_team_worker_ready",
]
