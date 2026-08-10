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
``agent_team_studio.agentic_team_provisioning.runtime.pipeline_runner``. Status is written to the
durable Postgres run store, so a completed run survives a worker/process restart.

WAIT (human-in-the-loop) steps use a Temporal **signal** (``submit_input``) plus
``workflow.wait_condition`` — a durable timer that survives a worker restart — rather
than the thread path's DB-polling loop.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import timedelta
from typing import Any

from temporalio import activity, workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError

from agent_team_studio.agentic_team_provisioning.step_ordering import order_step_ids

# Run statuses that are terminal — an activity must never write over one of these
# (e.g. resurrect a cancelled run). Mirrors the store's compare-and-swap guards.
_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})

# Activity timeouts: agent steps are long, blocking LLM calls; bookkeeping steps are
# quick. The wait timeout for a WAIT step is a *workflow timer*, not an activity
# timeout, so it is passed to ``run`` as an argument (resolved in the API process).
_AGENT_STEP_TIMEOUT = timedelta(hours=2)
_BOOKKEEPING_TIMEOUT = timedelta(seconds=30)

# Retry policies. Temporal only retries an activity when the retry policy allows it, so
# ``maximum_attempts=1`` would mean a worker crash / lost task mid-activity is NOT
# recovered — the workflow would fail once the start-to-close timeout elapses, defeating
# the "survives a worker/process restart" guarantee. Instead we allow bounded retries so
# a crashed activity is picked up by another worker:
#   * ``_STORE_RETRY`` — the short, idempotent store-bookkeeping activities (advance,
#     wait setup/resume/expire, complete, cancel, fail). All are written to be safe to
#     re-run (compare-and-swap or "append iff absent"), so retrying on any transient
#     fault is safe.
#   * ``_AGENT_RETRY`` — the long, non-idempotent LLM step. Retries recover a crashed
#     worker (``run_step_activity`` short-circuits on an already-completed step, so a
#     re-run does not double-charge), but a *genuine* application error is raised as a
#     ``non_retryable`` ``ApplicationError`` (see ``run_step_activity``) so it fails fast
#     instead of re-running the expensive model call. Note: without activity
#     heartbeating, a crash mid-LLM-call is only detected after ``_AGENT_STEP_TIMEOUT``;
#     adding heartbeats to shorten that window is a follow-up.
_STORE_RETRY = RetryPolicy(initial_interval=timedelta(seconds=1), maximum_attempts=5)
_AGENT_RETRY = RetryPolicy(initial_interval=timedelta(seconds=2), maximum_attempts=3)


def _topo_order(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Order step dicts following ``next_steps`` edges (pure, deterministic).

    Delegates the ordering to the shared, stdlib-only
    ``agent_team_studio.agentic_team_provisioning.step_ordering.order_step_ids`` — the same algorithm the
    daemon-thread ``PipelineRunner._topological_sort`` uses — reading only ``step_id`` /
    ``next_steps`` from each dict. ``step_ordering`` pulls in nothing heavy, so importing
    it here keeps the workflow sandbox-safe; being pure, this replays deterministically.

    Preconditions: each element is a dict with a ``step_id`` str and a ``next_steps``
        list of str (missing ``next_steps`` is treated as empty).
    Postconditions: returns the steps in breadth-first execution order from the entry
        points (steps referenced by no other step's ``next_steps``); unreachable steps
        are appended at the end. Falls back to input order when the graph is ambiguous.
    """
    step_map = {s["step_id"]: s for s in steps}
    order = order_step_ids([(s["step_id"], s.get("next_steps") or []) for s in steps])
    return [step_map[sid] for sid in order]


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
    from agent_team_studio.agentic_team_provisioning.testing.store import get_test_store

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
          any non-WAIT) step returns the agent output.
        - A genuine handler failure (e.g. the LLM call raising) is re-raised as a
          ``non_retryable`` ``ApplicationError`` so it fails the workflow *fast* rather
          than re-running the expensive model call under ``_AGENT_RETRY``. A worker
          crash / lost task (not an exception) is still retried by that policy so the
          step is picked up by another worker — recovery, without retrying real errors.
    """
    from agent_team_studio.agentic_team_provisioning.models import (
        ProcessDefinition,
    )
    from agent_team_studio.agentic_team_provisioning.runtime.pipeline_runner import PipelineRunner
    from agent_team_studio.agentic_team_provisioning.testing.store import get_test_store

    store = get_test_store()

    run = store.get_pipeline_run(run_id)
    step_results: list[dict[str, Any]] = list((run or {}).get("step_results") or [])
    for existing in step_results:
        if existing.get("step_id") == step_id and existing.get("status") == "completed":
            # Replay after a mid-activity crash: do not re-run the LLM or double-append.
            # Return the RAW handler return value (``output_raw``), not the display
            # ``output``. They differ for a DECISION step — ``run_step`` returns the bare
            # branch id while the recorded output is ``"Decision: <id>"`` — so returning
            # ``output`` would change ``prev_output`` for later steps versus a non-crash
            # run. ``output_raw`` is absent only on rows written before this field existed.
            return existing.get("output_raw", existing.get("output")) or ""

    process = ProcessDefinition(**process_json)
    step = next((s for s in process.steps if s.step_id == step_id), None)
    if step is None:  # pragma: no cover - dispatcher only sends known step ids
        raise ValueError(f"step {step_id} not found in process {process.process_id}")

    # In-flight workflows may still carry pre-thin fat roster JSON (manifest_id
    # null). Coerce via migrate so ValidationError does not fail the run.
    from agent_team_studio.agentic_team_provisioning.roster_resolve import coerce_roster_agent

    team_id = str((run or {}).get("team_id") or "")
    if not team_id:
        raise ApplicationError(
            f"pipeline run {run_id} is missing team_id; cannot coerce roster agents",
            type="MissingTeamId",
            non_retryable=True,
        )
    agents = [coerce_roster_agent(team_id, a) for a in team_agents_json]
    agents_by_name = {a.agent_name: a for a in agents}

    runner = PipelineRunner(store, start_sweeper=False)
    try:
        return runner.run_step(run_id, step, prev_output, step_results, agents_by_name)
    except Exception as exc:
        # Genuine application failure — do NOT retry the expensive, non-idempotent LLM
        # call. Mark non_retryable so ``_AGENT_RETRY`` fails the workflow immediately;
        # only crashes/timeouts (which arrive as retryable task failures, not exceptions)
        # consume the retry budget.
        raise ApplicationError(str(exc), type=type(exc).__name__, non_retryable=True) from exc


@activity.defn(name="agentic_pipeline_wait_setup")
def wait_setup_activity(run_id: str, step_id: str, step_name: str, prompt_text: str) -> bool:
    """Publish a WAIT step: append a ``waiting_for_input`` result and pause the run.

    Preconditions: ``run_id`` refers to a run in the store; ``prompt_text`` is the
        human-facing prompt.
    Postconditions:
        - Returns ``False`` without writing anything if the run is missing or already
          terminal (cancelled/failed/completed) — e.g. a cancel that landed while this
          activity was in flight or being retried. This prevents resurrecting a
          cancelled row back to ``waiting_for_input`` (Temporal-owned rows are skipped by
          the stale-heartbeat reaper, so a resurrected row would otherwise be stuck). The
          workflow stops the run when this returns ``False``.
        - Otherwise sets the run ``waiting_for_input`` with ``human_prompt`` and appends a
          ``waiting_for_input`` step result (idempotent — a re-run does not append a
          second waiting result for the same ``step_id``) and returns ``True``.
    """
    from agent_team_studio.agentic_team_provisioning.testing.store import get_test_store

    store = get_test_store()
    run = store.get_pipeline_run(run_id)
    if run is None or run.get("status") in _TERMINAL_STATUSES:
        return False
    step_results: list[dict[str, Any]] = list(run.get("step_results") or [])
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
    return True


@activity.defn(name="agentic_pipeline_wait_finalize")
def wait_finalize_activity(
    run_id: str, step_id: str, allow_expire: bool, wait_timeout_s: int
) -> dict:
    """Reconcile a WAIT step's outcome from the durable store (single source of truth).

    Called after ``workflow.wait_condition`` returns (a ``submit_input`` signal) OR its
    timer elapses. Rather than trusting the local signal/timeout, this reads the run's
    actual status — which the ``/input`` endpoint (resume) and the cancel path (cancel)
    both drive via compare-and-swap — and decides the outcome, taking the human input
    from the persisted row (not the signal). This closes the resume-vs-timeout and
    signal-delivery races: a resume that won just as the timer fired is still honoured,
    and a resume whose signal was lost is picked up here when the timer elapses.

    Preconditions: ``run_id`` refers to a run in the store; ``allow_expire`` is True only
        when the WAIT timer has fully elapsed; ``wait_timeout_s`` > 0.
    Postconditions: returns ``{"state": ...}`` (with ``"output"`` when resumed):
        - ``"resumed"`` — the row is ``running`` (endpoint resume committed). The WAIT
          step result is marked ``completed`` with the row's persisted ``human_input``
          (idempotent) and ``human_prompt`` is cleared WITHOUT writing ``status`` (the
          endpoint owns it, so a cancelled run is never revived). ``output`` is that input.
        - ``"terminal"`` — the row is cancelled/failed/completed; the workflow stops.
        - ``"expired"`` — only when ``allow_expire`` and the row was still
          ``waiting_for_input``: the run is CAS-moved to ``failed`` and the step marked
          ``timed_out``. If the expire CAS is lost (a resume/cancel won the race) the
          store is re-read and ``"resumed"``/``"terminal"`` is returned instead — the
          timeout never clobbers a resume.
        - ``"waiting"`` — still ``waiting_for_input`` and not yet allowed to expire; the
          workflow re-arms its wait.
    """
    from agent_team_studio.agentic_team_provisioning.testing.store import get_test_store

    store = get_test_store()

    def _resumed(run_row: dict) -> dict:
        human_input = store.get_pipeline_status(run_id)
        human_input = human_input["human_input"] if human_input else ""
        step_results = list(run_row.get("step_results") or [])
        for s in step_results:
            if s.get("step_id") == step_id and s.get("status") != "completed":
                s["status"] = "completed"
                s["output"] = human_input
                break
        store.update_pipeline_run(run_id, human_prompt=None, step_results=step_results)
        return {"state": "resumed", "output": human_input}

    run = store.get_pipeline_run(run_id)
    status = run.get("status") if run else None
    if status == "running":
        return _resumed(run)
    if status in _TERMINAL_STATUSES:
        return {"state": "terminal"}
    # status == "waiting_for_input" (or missing)
    if not allow_expire:
        return {"state": "waiting"}

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
        return {"state": "expired"}

    # Lost the expire CAS — a resume or cancel won the race just now. Re-read and honour it.
    run = store.get_pipeline_run(run_id)
    status = run.get("status") if run else None
    if status == "running":
        return _resumed(run)
    return {"state": "terminal"}


@activity.defn(name="agentic_pipeline_complete")
def complete_activity(run_id: str) -> None:
    """Complete a run iff it is still ``running`` (compare-and-swap).

    Preconditions: ``run_id`` refers to a run in the store.
    Postconditions: the run is ``completed`` with its current ``step_results`` iff it
        was still ``running``; a run moved terminal out-of-band (cancel/expire) keeps
        that status rather than being clobbered to ``completed``.
    """
    from agent_team_studio.agentic_team_provisioning.testing.store import get_test_store

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
    from agent_team_studio.agentic_team_provisioning.testing.store import get_test_store

    get_test_store().try_cancel_pipeline_run(run_id)


@activity.defn(name="agentic_pipeline_fail")
def fail_activity(run_id: str, error: str) -> None:
    """Fail the run row iff it is still active (compare-and-swap).

    Preconditions: ``run_id`` refers to a run in the store; ``error`` is a str.
    Postconditions: the run is ``failed`` with ``error`` iff it was still active; a run
        already terminal (cancelled/expired) is left untouched.
    """
    from agent_team_studio.agentic_team_provisioning.testing.store import get_test_store

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

    async def _run_wait_step(
        self, run_id: str, step: dict[str, Any], wait_timeout_s: int
    ) -> dict[str, Any]:
        """Drive one WAIT step to a store-reconciled outcome.

        The ``submit_input`` signal is only a fast-path *wake*; the authoritative
        outcome is always read from the durable store by ``wait_finalize_activity``
        (resume, cancel, and timeout all transition the row via compare-and-swap). This
        makes the store the single source of truth, so none of the resume-vs-timeout,
        signal-loss, or cancel races can desync the workflow from the run row.

        Preconditions: ``step`` is a WAIT step dict; ``wait_timeout_s`` > 0.
        Postconditions: returns ``{"state": "resumed", "output": <input>}``,
            ``{"state": "expired"}`` (WAIT timed out), or ``{"state": "terminal"}``
            (cancelled/failed out-of-band, or setup skipped because the run was already
            terminal). Bounded by a deterministic ``workflow.now()`` deadline, so a
            spurious wake cannot loop past the WAIT timeout.
        """
        step_id = step["step_id"]
        prompt_text = step.get("description") or (
            f"Human input required for: {step.get('name', step_id)}"
        )
        # Reset BEFORE setup makes the run resumable: once ``waiting_for_input`` is
        # published, the API can signal; a reset after setup could discard a signal
        # landing in that window. (Mirrors the thread runner's "clear before publishing".)
        self._human_input = None
        published = await workflow.execute_activity(
            wait_setup_activity,
            args=[run_id, step_id, step.get("name", ""), prompt_text],
            start_to_close_timeout=_BOOKKEEPING_TIMEOUT,
            retry_policy=_STORE_RETRY,
        )
        if not published:
            # Run went terminal (e.g. cancelled) before/while setup ran — do not wait.
            return {"state": "terminal"}

        deadline = workflow.now() + timedelta(seconds=wait_timeout_s)
        while True:
            remaining = (deadline - workflow.now()).total_seconds()
            allow_expire = remaining <= 0
            if not allow_expire:
                # A signal wakes us immediately; otherwise we re-check at the deadline.
                with contextlib.suppress(asyncio.TimeoutError):
                    await workflow.wait_condition(
                        lambda: self._human_input is not None,
                        timeout=timedelta(seconds=remaining),
                    )
            outcome = await workflow.execute_activity(
                wait_finalize_activity,
                args=[run_id, step_id, allow_expire, wait_timeout_s],
                start_to_close_timeout=_BOOKKEEPING_TIMEOUT,
                retry_policy=_STORE_RETRY,
            )
            if outcome["state"] != "waiting":
                return outcome
            # Still waiting (a spurious/duplicate wake before the resume CAS committed).
            # Reset the wake flag and re-arm; the workflow.now() deadline bounds the loop.
            self._human_input = None

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
                    retry_policy=_STORE_RETRY,
                )
                if not advanced:
                    return {"run_id": run_id, "terminal": "out_of_band"}

                if step.get("step_type") == "wait":
                    outcome = await self._run_wait_step(run_id, step, wait_timeout_s)
                    if outcome["state"] == "resumed":
                        prev_output = outcome["output"]
                    elif outcome["state"] == "expired":
                        return {"run_id": run_id, "terminal": "timed_out"}
                    else:  # "terminal" — cancelled/failed out-of-band, or setup skipped it
                        return {"run_id": run_id, "terminal": "out_of_band"}
                else:
                    prev_output = await workflow.execute_activity(
                        run_step_activity,
                        args=[run_id, team_agents_json, process_json, step["step_id"], prev_output],
                        start_to_close_timeout=_AGENT_STEP_TIMEOUT,
                        retry_policy=_AGENT_RETRY,
                    )

            await workflow.execute_activity(
                complete_activity,
                args=[run_id],
                start_to_close_timeout=_BOOKKEEPING_TIMEOUT,
                retry_policy=_STORE_RETRY,
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
                retry_policy=_STORE_RETRY,
            )
            raise
        except Exception as exc:
            await workflow.execute_activity(
                fail_activity,
                args=[run_id, str(exc)],
                start_to_close_timeout=_BOOKKEEPING_TIMEOUT,
                retry_policy=_STORE_RETRY,
            )
            raise
