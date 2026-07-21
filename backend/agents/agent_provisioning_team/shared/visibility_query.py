"""Detection primitive for the agent-lock rollout drain gate.

``AgentProvisioningWorkflow``/``AgentDeprovisioningWorkflow`` (see
``temporal/workflows.py``) gate their per-``agent_id`` ownership lock behind
``workflow.patched(_PROVISIONING_LOCK_PATCH)`` / ``workflow.patched(
_DEPROVISIONING_LOCK_PATCH)`` so a workflow history recorded before the lock
existed keeps replaying its original, lock-free command sequence. Those
markers can only be safely deleted once no such pre-patch history is still
open — today that is a manual "confirm via the Temporal UI" step (see the
``TODO`` next to the markers in ``temporal/workflows.py``).

This module replaces that manual check with a programmatic Temporal
visibility query: :func:`find_open_pre_patch_executions` lists every open
execution of either workflow type started before a configured cutoff
timestamp (the lock-patch deploy time), so a drain gate or runbook can ask
"is it safe yet?" instead of eyeballing the UI.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from shared.temporal import await_client

logger = logging.getLogger(__name__)

# The two workflow types whose replay behavior is gated by the lock-patch
# markers (temporal/workflows.py's _PROVISIONING_LOCK_PATCH /
# _DEPROVISIONING_LOCK_PATCH).
_WATCHED_WORKFLOW_TYPES = ("AgentProvisioningWorkflow", "AgentDeprovisioningWorkflow")

# Positional index of `agent_id` in each workflow type's `run()` signature
# (temporal/workflows.py), used to decode it from an execution's
# WorkflowExecutionStarted history event when needed.
_AGENT_ID_ARG_INDEX = {
    "AgentProvisioningWorkflow": 1,  # run(job_id, agent_id, manifest_path, ...)
    "AgentDeprovisioningWorkflow": 0,  # run(agent_id, force=False)
}

# The env var ops sets to the lock-patch release's deploy timestamp (ISO-8601,
# e.g. "2026-07-17T00:00:00Z"). Documented in docs/ENV_VARS.md.
LOCK_PATCH_CUTOFF_ENV_VAR = "AGENT_PROVISIONING_LOCK_PATCH_CUTOFF_AT"

# Query-side RPC ceiling, independent of the client-readiness wait
# (await_client's own default). Generous because resolving agent_id may
# fetch one history event per open execution found.
DEFAULT_QUERY_TIMEOUT_S = 30.0


@dataclass(frozen=True)
class PrePatchExecution:
    """One open execution a drain gate/runbook needs to know about.

    Invariants:
        * ``agent_id`` is ``None`` only when it could not be resolved from
          the execution's start-event input — never a signal that the
          execution is unrelated to any agent.
    """

    workflow_id: str
    run_id: str
    workflow_type: str
    start_time: datetime
    agent_id: Optional[str]


def _lock_patch_cutoff() -> Optional[datetime]:
    """Parse ``LOCK_PATCH_CUTOFF_ENV_VAR`` into a timezone-aware cutoff.

    Preconditions:
        * None.
    Postconditions:
        * Returns a tz-aware ``datetime`` for a valid ISO-8601 value (naive
          values are treated as UTC).
        * Returns ``None`` when the var is unset or unparseable — this is a
          fail-safe "no cutoff configured", not "cutoff is now"; callers must
          treat ``None`` as "include every open execution of either watched
          type" rather than silently reporting nothing.
    """
    raw = os.environ.get(LOCK_PATCH_CUTOFF_ENV_VAR, "").strip()
    if not raw:
        return None
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        logger.warning(
            "%s is not a valid ISO-8601 timestamp (%r); treating as unset "
            "(every open execution of either watched workflow type will be reported)",
            LOCK_PATCH_CUTOFF_ENV_VAR,
            raw,
        )
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value


def _build_query(cutoff: Optional[datetime]) -> str:
    """Build the Temporal visibility list filter for open watched executions.

    Preconditions:
        * None.
    Postconditions:
        * Always filters to ``_WATCHED_WORKFLOW_TYPES`` and
          ``ExecutionStatus = 'Running'``. Adds a ``StartTime <`` predicate
          (UTC, second precision) only when ``cutoff`` is given.
    """
    types = ", ".join(f"'{t}'" for t in _WATCHED_WORKFLOW_TYPES)
    clauses = [f"WorkflowType IN ({types})", "ExecutionStatus = 'Running'"]
    if cutoff is not None:
        cutoff_utc = cutoff.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        clauses.append(f"StartTime < '{cutoff_utc}'")
    return " AND ".join(clauses)


async def _resolve_agent_id(client: Any, execution: Any) -> Optional[str]:
    """Best-effort decode of ``agent_id`` from an execution's start input.

    Preconditions:
        * ``execution`` is a ``temporalio.client.WorkflowExecution`` for one
          of ``_WATCHED_WORKFLOW_TYPES``.
    Postconditions:
        * Returns the decoded ``agent_id`` string on success.
        * Returns ``None`` (never raises) when the workflow type is
          unrecognized, the start event/payload is missing or malformed, or
          decoding otherwise fails — callers must still include the
          execution in results rather than dropping it, since under-reporting
          an open pre-patch execution defeats this module's safety purpose.
    """
    arg_index = _AGENT_ID_ARG_INDEX.get(execution.workflow_type)
    if arg_index is None:
        return None
    try:
        handle = client.get_workflow_handle(execution.id, run_id=execution.run_id)
        async for event in handle.fetch_history_events(page_size=1):
            if not event.HasField("workflow_execution_started_event_attributes"):
                return None
            payloads = event.workflow_execution_started_event_attributes.input.payloads
            values = await client.data_converter.decode(payloads)
            if len(values) <= arg_index or not isinstance(values[arg_index], str):
                return None
            return values[arg_index]
        return None
    except Exception as exc:  # best-effort resolution, never fatal
        logger.warning(
            "Could not resolve agent_id for open execution id=%s run_id=%s (%s)",
            execution.id,
            execution.run_id,
            exc,
        )
        return None


async def _find_open_pre_patch_executions_async(
    client: Any,
    *,
    agent_id: Optional[str],
    cutoff: Optional[datetime],
) -> list[PrePatchExecution]:
    query = _build_query(cutoff)
    results: list[PrePatchExecution] = []
    async for execution in client.list_workflows(query=query):
        resolved_agent_id = await _resolve_agent_id(client, execution)
        if agent_id is not None and resolved_agent_id is not None and resolved_agent_id != agent_id:
            continue
        results.append(
            PrePatchExecution(
                workflow_id=execution.id,
                run_id=execution.run_id,
                workflow_type=execution.workflow_type,
                start_time=execution.start_time,
                agent_id=resolved_agent_id,
            )
        )
    return results


def find_open_pre_patch_executions(
    agent_id: Optional[str] = None,
    *,
    client_ready_timeout_s: Optional[float] = None,
    query_timeout_s: float = DEFAULT_QUERY_TIMEOUT_S,
) -> list[PrePatchExecution]:
    """Return open pre-patch provisioning/deprovisioning workflow executions.

    The detection primitive a drain gate or a manual runbook check uses to
    decide whether the ``_PROVISIONING_LOCK_PATCH`` /
    ``_DEPROVISIONING_LOCK_PATCH`` markers (``temporal/workflows.py``) can be
    safely removed: while this returns any results, at least one open
    workflow history predates the lock and must not be treated as
    unconditionally locked.

    Preconditions:
        * ``agent_id``, when given, is non-empty.
        * The Agent Provisioning Temporal worker's client/loop become
          available within ``client_ready_timeout_s``.
    Postconditions:
        * Returns every open execution of either watched workflow type
          started before ``LOCK_PATCH_CUTOFF_ENV_VAR`` — or every open
          execution of either type when that var is unset/unparseable
          (fail-safe: an unconfigured cutoff must never look like "nothing
          is open").
        * When ``agent_id`` is given, excludes only executions whose
          agent_id was positively resolved to a *different* value; an
          execution whose agent_id could not be resolved is still included
          (conservative — it might be a match).
    Raises:
        * ``RuntimeError`` when the Temporal client/loop never becomes
          available within ``client_ready_timeout_s`` — callers must treat
          this as "cannot prove drained," not as "nothing open."
        * ``concurrent.futures.TimeoutError`` when the query/history fetches
          exceed ``query_timeout_s``.
    """
    if agent_id is not None:
        assert agent_id, "agent_id must be non-empty when provided"
    client, loop = await_client(client_ready_timeout_s)
    future = asyncio.run_coroutine_threadsafe(
        _find_open_pre_patch_executions_async(
            client, agent_id=agent_id, cutoff=_lock_patch_cutoff()
        ),
        loop,
    )
    return future.result(timeout=query_timeout_s)
