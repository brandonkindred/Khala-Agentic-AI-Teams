"""Shared Temporal scaffolding for all agent teams.

Provides a single place for Temporal client connection, worker boilerplate,
job-backed workflow runner, and generic checkpoint/pause-resume helpers so
every team can adopt durable, resumable job tracking with minimal code.

Public API:
    from shared_temporal import (
        get_temporal_client, is_temporal_enabled, connect_temporal_client,
        start_team_worker, run_team_job,
        save_checkpoint, load_checkpoint, wait_for_input, submit_input,
    )
"""

from shared_temporal.checkpoints import (
    load_checkpoint,
    save_checkpoint,
    submit_input,
    wait_for_input,
)
from shared_temporal.client import (
    connect_temporal_client,
    get_temporal_address,
    get_temporal_client,
    get_temporal_loop,
    get_temporal_namespace,
    is_temporal_enabled,
    set_temporal_client,
    set_temporal_loop,
)
from shared_temporal.runner import run_team_job
from shared_temporal.teams_registry import TEAM_TEMPORAL_MODULES, start_all_team_workers
from shared_temporal.worker import start_team_worker

__all__ = [
    "TEAM_TEMPORAL_MODULES",
    "connect_temporal_client",
    "start_all_team_workers",
    "get_temporal_address",
    "get_temporal_client",
    "get_temporal_loop",
    "get_temporal_namespace",
    "is_temporal_enabled",
    "load_checkpoint",
    "run_team_job",
    "save_checkpoint",
    "set_temporal_client",
    "set_temporal_loop",
    "start_team_worker",
    "submit_input",
    "wait_for_input",
]
