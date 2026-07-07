"""Temporal workflow + activities for the agentic_team_provisioning pipeline runner.

Kept in its own module (separate from the package ``__init__``) so the temporalio
workflow sandbox can re-import the workflow class without executing any
non-deterministic top-level code (``os.getenv``, worker bootstrap, pydantic model
construction). The workflow body touches only plain dicts and a pure topological
sort; ALL pydantic reconstruction, LLM calls, and store writes happen inside
**activities**, which run outside the sandbox.

The activities delegate to the existing ``PipelineRunner`` step handlers and
``AgenticTestStore`` so the orchestration + job-status bookkeeping live in exactly
one place, shared with the daemon-thread dispatch path in
``agentic_team_provisioning.runtime.pipeline_runner``. Status is written to the
durable Postgres run store, so a completed run survives a worker/process restart.

WAIT (human-in-the-loop) steps use a Temporal **signal** (``submit_input``) plus
``workflow.wait_condition`` — a durable timer that survives a worker restart — rather
than the thread path's DB-polling loop.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

from temporalio import activity, workflow
from temporalio.common import RetryPolicy

# Activity timeouts: agent steps are long, blocking LLM calls; bookkeeping steps are
# quick. The wait timeout for a WAIT step is a *workflow timer*, not an activity
# timeout, so it is passed to ``run`` as an argument (resolved in the API process).
_AGENT_STEP_TIMEOUT = timedelta(hours=2)
_BOOKKEEPING_TIMEOUT = timedelta(seconds=30)

# LLM/store steps are non-idempotent; a failure surfaces as a failed workflow (and a
# FAILED run-store row) for explicit resubmission rather than being auto-retried.
_SINGLE_ATTEMPT = RetryPolicy(maximum_attempts=1)


def _topo_order(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Order step dicts following ``next_steps`` edges (pure, deterministic).

    A dict-based clone of ``PipelineRunner._topological_sort`` reading only
    ``step_id`` / ``next_steps``. Lives here (not imported from ``pipeline_runner``)
    so the workflow sandbox never imports the heavyweight runtime module. Being pure,
    it replays deterministically.

    Preconditions: each element is a dict with a ``step_id`` str and a ``next_steps``
        list of str (missing ``next_steps`` is treated as empty).
    Postconditions: returns the steps in breadth-first execution order from the entry
        points (steps referenced by no other step's ``next_steps``); unreachable steps
        are appended at the end. Falls back to input order when the graph is ambiguous.
    """
    if not steps:
        return []

    step_map = {s["step_id"]: s for s in steps}
    all_next: set[str] = set()
    for s in steps:
        all_next.update(s.get("next_steps") or [])

    entry_ids = [s["step_id"] for s in steps if s["step_id"] not in all_next]
    if not entry_ids:
        entry_ids = [steps[0]["step_id"]]

    visited: set[str] = set()
    ordered: list[dict[str, Any]] = []
    queue = list(entry_ids)
    while queue:
        sid = queue.pop(0)
        if sid in visited:
            continue
        visited.add(sid)
        step = step_map.get(sid)
        if step:
            ordered.append(step)
            queue.extend(step.get("next_steps") or [])

    for s in steps:
        if s["step_id"] not in visited:
            ordered.append(s)
    return ordered


# ---------------------------------------------------------------------------
# Activities (run outside the sandbox — pydantic + store I/O allowed)
# ---------------------------------------------------------------------------


@activity.defn(name="agentic_pipeline_advance_step")
def advance_step_activity(run_id: str, step_id: str) -> bool:
    """Advance the step cursor iff the run is still ``running``.

    Preconditions: ``run_id`` refers to a run created in the store; ``step_id`` a str.
    Postconditions: returns ``True`` and moves the cursor when the run is still
        ``running``; ``False`` (no write) when it reached a terminal state out-of-band
        (cancelled/failed/completed), signalling the workflow to stop.
    """
    from agentic_team_provisioning.testing.store import get_test_store

    return get_test_store().advance_pipeline_step(run_id, step_id)


@activity.defn(name="agentic_pipeline_run_step")
def run_step_activity(
    run_id: str,
    team_agents_json: list[dict[str, Any]],
    process_json: dict[str, Any],
    step_id: str,
    prev_output: str,
) -> str:
    """Run one ACTION/DECISION (or default) step via the existing PipelineRunner handler.

    Preconditions:
        - ``run_id`` refers to a run already created in the store.
        - ``team_agents_json`` / ``process_json`` are serialized ``AgenticTeamAgent`` /
          ``ProcessDefinition`` (``model_dump(mode="json")``); ``step_id`` names a step
          in ``process_json``.

    Postconditions:
        - Appends a ``completed`` step result for ``step_id`` (via the handler's
          ``_record_step``) and returns the step's output, which becomes the next
          step's ``prev_output``.
        - Idempotent per ``step_id``: if a ``completed`` result for ``step_id`` already
          exists (a mid-activity worker crash re-ran this activity), the stored output
          is returned WITHOUT re-invoking the LLM or re-appending — no double-charge,
          no duplicate step result.
        - A DECISION step returns the chosen branch's ``step_id`` string; an ACTION (or
          any non-WAIT) step returns the agent output. Handler exceptions propagate so
          the failure surfaces as a failed workflow.
    """
    from agentic_team_provisioning.models import (
        AgenticTeamAgent,
        ProcessDefinition,
        StepType,
    )
    from agentic_team_provisioning.runtime.pipeline_runner import PipelineRunner
    from agentic_team_provisioning.testing.store import get_test_store

    store = get_test_store()

    run = store.get_pipeline_run(run_id)
    step_results: list[dict[str, Any]] = list((run or {}).get("step_results") or [])
    for existing in step_results:
        if existing.get("step_id") == step_id and existing.get("status") == "completed":
            # Replay after a mid-activity crash: do not re-run the LLM or double-append.
            return existing.get("output") or ""

    process = ProcessDefinition(**process_json)
    step = next((s for s in process.steps if s.step_id == step_id), None)
    if step is None:  # pragma: no cover - dispatcher only sends known step ids
        raise ValueError(f"step {step_id} not found in process {process.process_id}")

    agents = [AgenticTeamAgent(**a) for a in team_agents_json]
    agents_by_name = {a.agent_name: a for a in agents}

    runner = PipelineRunner(store, start_sweeper=False)
    if step.step_type == StepType.DECISION:
        return runner._handle_decision_step(run_id, step, prev_output, step_results, agents_by_name)
    return runner._handle_action_step(run_id, step, prev_output, step_results, agents_by_name)


@activity.defn(name="agentic_pipeline_wait_setup")
def wait_setup_activity(run_id: str, step_id: str, step_name: str, prompt_text: str) -> None:
    """Publish a WAIT step: append a ``waiting_for_input`` result and pause the run.

    Preconditions: ``run_id`` refers to a run in the store; ``prompt_text`` is the
        human-facing prompt.
    Postconditions: the run row is ``waiting_for_input`` with ``human_prompt`` set and a
        ``waiting_for_input`` step result appended (idempotent — a re-run does not append
        a second waiting result for the same ``step_id``). GET status endpoints observe
        the pause immediately.
    """
    from agentic_team_provisioning.testing.store import get_test_store

    store = get_test_store()
    run = store.get_pipeline_run(run_id)
    step_results: list[dict[str, Any]] = list((run or {}).get("step_results") or [])
    if not any(s.get("step_id") == step_id for s in step_results):
        step_results.append(
            {
                "step_id": step_id,
                "step_name": step_name,
                "agent_name": "",
                "input": "",
                "output": "",
                "status": "waiting_for_input",
            }
        )
    store.update_pipeline_run(
        run_id,
        status="waiting_for_input",
        human_prompt=prompt_text,
        step_results=step_results,
    )


@activity.defn(name="agentic_pipeline_wait_resume")
def wait_resume_activity(run_id: str, step_id: str, human_input: str) -> str:
    """Record the submitted human input and move the run back to ``running``.

    Preconditions: ``run_id`` refers to a run that was ``waiting_for_input``;
        ``human_input`` is a str (may be empty).
    Postconditions: the matching WAIT step result is marked ``completed`` with
        ``output = human_input``, ``human_prompt`` is cleared, the run is ``running``,
        and ``human_input`` is returned as the next step's ``prev_output``. No
        heartbeat-freshness guard is applied — Temporal owns this run's liveness.
    """
    from agentic_team_provisioning.testing.store import get_test_store

    store = get_test_store()
    run = store.get_pipeline_run(run_id)
    step_results: list[dict[str, Any]] = list((run or {}).get("step_results") or [])
    for s in step_results:
        if s.get("step_id") == step_id:
            s["status"] = "completed"
            s["output"] = human_input
            break
    store.update_pipeline_run(
        run_id,
        status="running",
        human_prompt=None,
        step_results=step_results,
    )
    return human_input


@activity.defn(name="agentic_pipeline_wait_expire")
def wait_expire_activity(run_id: str, step_id: str, wait_timeout_s: int) -> None:
    """Fail a WAIT step whose human-input timeout elapsed.

    Preconditions: ``run_id`` refers to a still-waiting run; ``wait_timeout_s`` > 0.
    Postconditions: if the run is still ``waiting_for_input`` it is moved to ``failed``
        (compare-and-swap) and the WAIT step result is marked ``timed_out``; a concurrent
        resume/cancel that already left ``waiting_for_input`` is left untouched.
    """
    from agentic_team_provisioning.testing.store import get_test_store

    store = get_test_store()
    error = f"wait_timeout: no human input for WAIT step '{step_id}' within {wait_timeout_s}s"
    if store.try_expire_pipeline_run(run_id, error):
        run = store.get_pipeline_run(run_id)
        step_results: list[dict[str, Any]] = list((run or {}).get("step_results") or [])
        for s in step_results:
            if s.get("step_id") == step_id:
                s["status"] = "timed_out"
                s["output"] = f"Timed out after {wait_timeout_s}s waiting for human input"
                break
        store.update_pipeline_run(run_id, step_results=step_results)


@activity.defn(name="agentic_pipeline_complete")
def complete_activity(run_id: str) -> None:
    """Complete a run iff it is still ``running`` (compare-and-swap).

    Preconditions: ``run_id`` refers to a run in the store.
    Postconditions: the run is ``completed`` with its current ``step_results`` iff it
        was still ``running``; a run moved terminal out-of-band (cancel/expire) keeps
        that status rather than being clobbered to ``completed``.
    """
    from agentic_team_provisioning.testing.store import get_test_store

    store = get_test_store()
    run = store.get_pipeline_run(run_id)
    step_results = list((run or {}).get("step_results") or [])
    store.try_complete_pipeline_run(run_id, step_results)


@activity.defn(name="agentic_pipeline_cancel_reconcile")
def cancel_reconcile_activity(run_id: str) -> None:
    """Reconcile the run row to ``cancelled`` after a workflow cancellation.

    Preconditions: ``run_id`` refers to a run in the store.
    Postconditions: the run is ``cancelled`` iff it was still active; a no-op if the
        endpoint already CAS-cancelled it or the run was otherwise terminal.
    """
    from agentic_team_provisioning.testing.store import get_test_store

    get_test_store().try_cancel_pipeline_run(run_id)


@activity.defn(name="agentic_pipeline_fail")
def fail_activity(run_id: str, error: str) -> None:
    """Fail the run row iff it is still active (compare-and-swap).

    Preconditions: ``run_id`` refers to a run in the store; ``error`` is a str.
    Postconditions: the run is ``failed`` with ``error`` iff it was still active; a run
        already terminal (cancelled/expired) is left untouched.
    """
    from agentic_team_provisioning.testing.store import get_test_store

    get_test_store().try_fail_pipeline_run(run_id, error)


# ---------------------------------------------------------------------------
# Workflow (deterministic — plain dicts + pure topo sort only)
# ---------------------------------------------------------------------------


@workflow.defn(name="AgenticPipelineWorkflow")
class AgenticPipelineWorkflow:
    """Durable, signal-driven driver for a pipeline test run.

    Invariants:
        - Exactly one activity performs each store transition; the workflow never
          touches the store or an LLM directly (determinism).
        - Human input for a WAIT step arrives via the ``submit_input`` signal and is
          observed by ``workflow.wait_condition`` — no DB polling.
    """

    def __init__(self) -> None:
        self._human_input: str | None = None

    @workflow.signal(name="submit_input")
    def submit_input(self, human_input: str) -> None:
        """Deliver human input for the current WAIT step (wakes ``wait_condition``)."""
        self._human_input = human_input

    @workflow.run
    async def run(
        self,
        run_id: str,
        team_agents_json: list[dict[str, Any]],
        process_json: dict[str, Any],
        initial_input: str | None,
        wait_timeout_s: int,
    ) -> dict[str, Any]:
        """Walk the process DAG, running each step as an activity.

        Preconditions:
            - ``run_id`` refers to a run already created in the store.
            - ``team_agents_json`` / ``process_json`` are serialized ``AgenticTeamAgent``
              / ``ProcessDefinition``; ``wait_timeout_s`` > 0.

        Postconditions:
            - Each step is executed once (activities own store bookkeeping); WAIT steps
              pause on the ``submit_input`` signal, bounded by a ``wait_timeout_s`` timer.
            - On normal completion the run row ends ``completed``; on cancellation it is
              reconciled to ``cancelled``; on any other failure the row is marked
              ``failed`` and the workflow fails. Returns a small ``{run_id, terminal}``
              dict describing the terminal reason.
        """
        steps = _topo_order(process_json.get("steps") or [])
        prev_output = initial_input or ""
        try:
            for step in steps:
                advanced = await workflow.execute_activity(
                    advance_step_activity,
                    args=[run_id, step["step_id"]],
                    start_to_close_timeout=_BOOKKEEPING_TIMEOUT,
                    retry_policy=_SINGLE_ATTEMPT,
                )
                if not advanced:
                    return {"run_id": run_id, "terminal": "out_of_band"}

                if step.get("step_type") == "wait":
                    prompt_text = step.get("description") or (
                        f"Human input required for: {step.get('name', step['step_id'])}"
                    )
                    await workflow.execute_activity(
                        wait_setup_activity,
                        args=[run_id, step["step_id"], step.get("name", ""), prompt_text],
                        start_to_close_timeout=_BOOKKEEPING_TIMEOUT,
                        retry_policy=_SINGLE_ATTEMPT,
                    )
                    self._human_input = None
                    try:
                        await workflow.wait_condition(
                            lambda: self._human_input is not None,
                            timeout=timedelta(seconds=wait_timeout_s),
                        )
                    except asyncio.TimeoutError:
                        await workflow.execute_activity(
                            wait_expire_activity,
                            args=[run_id, step["step_id"], wait_timeout_s],
                            start_to_close_timeout=_BOOKKEEPING_TIMEOUT,
                            retry_policy=_SINGLE_ATTEMPT,
                        )
                        return {"run_id": run_id, "terminal": "timed_out"}
                    prev_output = await workflow.execute_activity(
                        wait_resume_activity,
                        args=[run_id, step["step_id"], self._human_input],
                        start_to_close_timeout=_BOOKKEEPING_TIMEOUT,
                        retry_policy=_SINGLE_ATTEMPT,
                    )
                else:
                    prev_output = await workflow.execute_activity(
                        run_step_activity,
                        args=[run_id, team_agents_json, process_json, step["step_id"], prev_output],
                        start_to_close_timeout=_AGENT_STEP_TIMEOUT,
                        retry_policy=_SINGLE_ATTEMPT,
                    )

            await workflow.execute_activity(
                complete_activity,
                args=[run_id],
                start_to_close_timeout=_BOOKKEEPING_TIMEOUT,
                retry_policy=_SINGLE_ATTEMPT,
            )
            return {"run_id": run_id, "terminal": "completed"}
        except asyncio.CancelledError:
            # Native workflow cancellation — reconcile the run row, then re-raise so the
            # workflow ends CANCELLED. The reconcile activity is a CAS no-op if the
            # cancel endpoint already flipped the row.
            await workflow.execute_activity(
                cancel_reconcile_activity,
                args=[run_id],
                start_to_close_timeout=_BOOKKEEPING_TIMEOUT,
                retry_policy=_SINGLE_ATTEMPT,
            )
            raise
        except Exception as exc:
            await workflow.execute_activity(
                fail_activity,
                args=[run_id, str(exc)],
                start_to_close_timeout=_BOOKKEEPING_TIMEOUT,
                retry_policy=_SINGLE_ATTEMPT,
            )
            raise
