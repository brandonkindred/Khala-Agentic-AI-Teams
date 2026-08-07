"""Pipeline runner: walks a ProcessDefinition DAG step-by-step.

Runs in a background thread. Auto-advances through ACTION, DECISION,
PARALLEL_SPLIT, PARALLEL_JOIN, and SUBPROCESS steps. Pauses at WAIT
steps until human input is submitted via the API.

Follows the background-thread pattern from ``ai_systems_team/api/main.py``.

Restart reliability (WAIT state)
--------------------------------
The terminal transition out of ``waiting_for_input`` (resume / timeout / cancel)
is decided in Postgres via single-row compare-and-swap, not in process memory, so
it is correct regardless of which uvicorn worker serves ``POST .../input`` and it
survives a service restart. The in-memory ``threading.Event`` is only a
same-worker wakeup optimization; the submitted answer is persisted to the DB and
re-read by the waiting thread. WAIT waits are bounded by a configurable timeout.

A per-run heartbeat thread refreshes a liveness timestamp for the whole duration a
run executes — including inside long synchronous ``call_agent`` steps — so an
advisory-locked reaper can fail runs whose worker has actually died (orphans)
without ever reaping a live run that merely happens to be slow.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Optional

from agent_team_studio.agentic_team_provisioning.models import (
    AgenticTeamAgent,
    ProcessDefinition,
    ProcessStep,
    StepType,
)
from agent_team_studio.agentic_team_provisioning.roster_resolve import resolve_persona
from agent_team_studio.agentic_team_provisioning.runtime.agent_builder import (
    build_agent,
    call_agent,
)
from agent_team_studio.agentic_team_provisioning.step_ordering import order_step_ids
from agent_team_studio.agentic_team_provisioning.testing.store import AgenticTestStore
from agent_team_studio.agentic_team_provisioning.wait_timeout import resolve_wait_timeout_s
from shared.concurrency import BackgroundHeartbeat
from shared.env import parse_int
from shared.postgres import is_postgres_enabled

logger = logging.getLogger(__name__)

# Default bounds for the WAIT-state liveness knobs. See docs/ENV_VARS.md. The WAIT
# human-input timeout itself lives in ``agent_team_studio.agentic_team_provisioning.wait_timeout`` so the
# thread and Temporal dispatch paths resolve it from one place.
_DEFAULT_WAIT_POLL_S = 5
_MIN_WAIT_POLL_S = 1
_MAX_WAIT_POLL_S = 60
_DEFAULT_STALE_S = 30


class PipelineRunner:
    """Walks the process DAG, runs agents at each step, pauses at WAIT steps.

    Invariants:
        - A run is resumed/timed-out/cancelled/completed by exactly one actor,
          enforced by the store's compare-and-swap out of its prior status.
        - ``_resume_events[run_id]`` exists only for a run whose worker thread is live
          in *this* process; its absence never strands a run (resume is DB-driven).
        - While a run executes, a heartbeat thread keeps ``heartbeat_at`` fresh, so a
          live run is never reaped as an orphan.
    """

    def __init__(self, store: AgenticTestStore, *, start_sweeper: bool = True) -> None:
        """Preconditions: ``store`` is an ``AgenticTestStore``.

        Postconditions: config knobs are parsed once; if ``start_sweeper`` and Postgres
        is configured, a daemon thread is running that periodically reaps orphaned runs.
        Tests pass ``start_sweeper=False`` to keep the background thread out of
        assertions.
        """
        self._store = store
        self._resume_events: dict[str, threading.Event] = {}

        self._wait_timeout_s = resolve_wait_timeout_s()
        self._wait_poll_s = parse_int(
            "AGENTIC_TEAM_PIPELINE_WAIT_POLL_S",
            _DEFAULT_WAIT_POLL_S,
            minimum=_MIN_WAIT_POLL_S,
            maximum=_MAX_WAIT_POLL_S,
        )
        # Staleness must span at least a few poll/heartbeat intervals so a live run
        # that just heartbeated is never mistaken for an orphan.
        self._stale_s = parse_int(
            "AGENTIC_TEAM_PIPELINE_STALE_S",
            _DEFAULT_STALE_S,
            minimum=3 * self._wait_poll_s,
        )
        # Heartbeat at a third of the staleness window so a live run tolerates a couple
        # of missed/blocked beats (DB stall, GC pause, GIL contention) before it could
        # be mistaken for an orphan — both resume and the reaper key on this freshness.
        self._heartbeat_interval_s = max(1, self._stale_s // 3)

        self._sweeper_stop = threading.Event()
        # No Postgres -> the reaper's queries would just raise; skip the sweeper so it
        # doesn't spin logging errors (and leak a thread) in tests / no-DB dev.
        if start_sweeper and is_postgres_enabled():
            BackgroundHeartbeat(
                self._sweep_once,
                self._stale_s,
                name="pipeline-orphan-sweeper",
                on_error=lambda exc: logger.error(
                    "Pipeline orphan sweeper tick failed; will retry: %s", exc
                ),
                stop_event=self._sweeper_stop,
            ).start()

    def start_run(
        self,
        run_id: str,
        team_agents: list[AgenticTeamAgent],
        process: ProcessDefinition,
    ) -> None:
        """Spawn a background thread to execute the pipeline."""
        event = threading.Event()
        self._resume_events[run_id] = event
        thread = threading.Thread(
            target=self._execute,
            args=(run_id, team_agents, process, event),
            daemon=True,
            name=f"pipeline-{run_id[:16]}",
        )
        thread.start()

    def submit_human_input(self, run_id: str, user_input: str) -> bool:
        """Resume a paused pipeline run with human input.

        Preconditions: ``run_id`` is a non-empty str; ``user_input`` is a str.
        Postconditions: returns True iff the run was ``waiting_for_input`` with a live
        (fresh-heartbeat) worker and has been atomically moved to ``running`` (input
        persisted). Returns False if the run is no longer resumable — timed out,
        cancelled, completed, reaped, or orphaned by a restart (stale heartbeat, so no
        worker would drive it) — and the caller should surface a 409. Never forces a
        non-waiting run to ``running`` and never resumes a run into a stuck state.
        """
        assert run_id, "run_id must be non-empty"
        won = self._store.try_resume_pipeline_run(run_id, user_input, self._stale_s)
        if won:
            # Fast same-worker wakeup; on another worker the waiter observes the DB
            # flip on its next poll tick instead.
            event = self._resume_events.get(run_id)
            if event:
                event.set()
        return won

    def cancel_run(self, run_id: str) -> None:
        """Cancel a running or waiting pipeline run.

        Preconditions: ``run_id`` is a non-empty str.
        Postconditions: the run is ``cancelled`` iff it was still active (compare-and-
        swap), so a cancel racing a completed/failed outcome cannot clobber the real
        result; any local waiter is woken so its thread observes the cancel promptly.
        """
        assert run_id, "run_id must be non-empty"
        self._store.try_cancel_pipeline_run(run_id)
        event = self._resume_events.get(run_id)
        if event:
            event.set()

    def reap_orphaned_runs(self) -> int:
        """Fail active runs whose heartbeat has gone stale (orphaned by a dead worker).

        Preconditions: none.
        Postconditions: returns the number of runs transitioned to ``failed`` (0 if
        another worker held the reaper's advisory lock). Safe to call from any worker
        and at startup — never touches freshly-heartbeated (live) runs.
        """
        return self._store.reap_orphaned_pipeline_runs(
            "orphaned: reaped after service restart / no heartbeat",
            self._stale_s,
        )

    def _sweep_once(self) -> None:
        """One reaper pass — the ``beat`` of the orphan-sweeper heartbeat.

        Preconditions: none.
        Postconditions: reaps orphaned runs once and logs how many were failed. A reap
        error propagates to the heartbeat driver's ``on_error`` (logged; the loop
        continues to the next tick) rather than being swallowed here.
        """
        reaped = self.reap_orphaned_runs()
        if reaped:
            logger.warning("Reaped %d orphaned pipeline run(s)", reaped)

    def _execute(
        self,
        run_id: str,
        team_agents: list[AgenticTeamAgent],
        process: ProcessDefinition,
        resume_event: threading.Event,
    ) -> None:
        """Main pipeline execution loop."""
        # A dedicated heartbeat thread keeps heartbeat_at fresh for the whole run —
        # including inside a long synchronous call_agent step that never yields — so a
        # live run is never reaped/refused as an orphan. BackgroundHeartbeat beats
        # immediately, then every interval, and joins on context exit.
        with BackgroundHeartbeat(
            lambda: self._store.heartbeat_pipeline_run(run_id),
            self._heartbeat_interval_s,
            name=f"pipeline-hb-{run_id[:16]}",
            beat_first=True,
            on_error=lambda exc: logger.error(
                "Heartbeat for pipeline run %s failed; will retry: %s", run_id, exc
            ),
        ):
            try:
                agents_by_name: dict[str, AgenticTeamAgent] = {a.agent_name: a for a in team_agents}
                step_order = self._topological_sort(process.steps)

                # Use initial_input as starting context for the first step
                run_data = self._store.get_pipeline_run(run_id)
                prev_output = (run_data or {}).get("initial_input") or ""
                step_results: list[dict[str, Any]] = []

                for step in step_order:
                    # Advance the cursor iff the run is still 'running' (one round-trip).
                    # A False return means the run reached a terminal state out-of-band
                    # (cancelled by a user, or reaped/expired) — stop without
                    # resurrecting it. Status stays 'running' between steps (set at
                    # creation and on resume), so this can't revive a cancelled run.
                    if not self._store.advance_pipeline_step(run_id, step.step_id):
                        return

                    if step.step_type == StepType.WAIT:
                        resumed = self._handle_wait_step(
                            run_id, step, prev_output, step_results, resume_event
                        )
                        # None => the run reached a terminal state while waiting (timed
                        # out, cancelled, or reaped); stop without overwriting it.
                        if resumed is None:
                            return
                        prev_output = resumed
                    else:
                        prev_output = self.run_step(
                            run_id, step, prev_output, step_results, agents_by_name
                        )

                # Complete only if still running — a run cancelled/reaped mid-step must
                # keep its terminal state rather than being clobbered back to completed.
                if not self._store.try_complete_pipeline_run(run_id, step_results):
                    logger.info(
                        "Pipeline run %s finished executing but was already terminal", run_id
                    )
            except Exception as exc:
                logger.exception("Pipeline run %s failed", run_id)
                # CAS: only fail an *active* run, so an exception racing an out-of-band
                # terminal transition (cancel / reap) can't clobber that outcome.
                self._store.try_fail_pipeline_run(run_id, str(exc))
            finally:
                self._resume_events.pop(run_id, None)

    @staticmethod
    def _resolve_agent(
        step: ProcessStep, agents_by_name: dict[str, AgenticTeamAgent]
    ) -> tuple[str, Optional[AgenticTeamAgent]]:
        """Return ``(agent_name, agent_def)`` for a step's first assigned agent."""
        agent_name = step.agents[0].agent_name if step.agents else ""
        return agent_name, agents_by_name.get(agent_name)

    @staticmethod
    def _run_agent(agent_def: AgenticTeamAgent, prompt: str) -> str:
        """Build and invoke an agent for a single prompt (blocking LLM call).

        v1 scope boundary: every roster agent runs this way — including a
        ``source == "registry"`` entry, which executes as a free-text LLM persona built from its
        projected ``role`` / ``skills`` / ``tools`` fields. There is deliberately **no**
        ``source == "registry"`` branch: a registry agent's declared typed input/output schema is
        not marshalled through the DAG in v1. Real typed-IO registry-agent invocation is deferred —
        see ``system_design/adr/ADR-008-typed-io-registry-agents-in-free-text-dag.md``. Do not add a
        registry-execution branch here without first resolving that spike.
        """
        persona = resolve_persona(agent_def.manifest_id)
        agent_instance = build_agent(
            agent_def.agent_name,
            persona.role,
            persona.skills,
            persona.capabilities,
            persona.tools,
            persona.expertise,
        )
        return call_agent(agent_instance, prompt)

    def _record_step(
        self, run_id: str, step_results: list[dict[str, Any]], result: dict[str, Any]
    ) -> None:
        """Append a finished step's result and persist the updated step_results."""
        step_results.append(result)
        self._store.update_pipeline_run(run_id, step_results=step_results)

    def run_step(
        self,
        run_id: str,
        step: ProcessStep,
        prev_output: str,
        step_results: list[dict[str, Any]],
        agents_by_name: dict[str, AgenticTeamAgent],
    ) -> str:
        """Run one non-WAIT step (ACTION/DECISION/default), recording its result.

        The public dispatch entry point shared by the in-thread ``_execute`` loop and the
        Temporal ``run_step_activity``, so callers depend on this method rather than the
        private per-type handlers. WAIT steps are handled separately (they pause for human
        input) and must not be routed here.

        Preconditions:
            - ``step.step_type`` is not ``StepType.WAIT``.
            - ``run_id`` refers to a run already created in the store; ``step_results`` is
              the current (mutable) list of recorded results for the run.

        Postconditions:
            - Appends exactly one ``completed`` result for ``step`` to ``step_results``
              (persisted via ``_record_step``) and returns the step's output: the chosen
              branch ``step_id`` for a DECISION, otherwise the agent output string.
        """
        assert step.step_type != StepType.WAIT, "run_step must not be called for WAIT steps"
        if step.step_type == StepType.DECISION:
            return self._handle_decision_step(
                run_id, step, prev_output, step_results, agents_by_name
            )
        return self._handle_action_step(run_id, step, prev_output, step_results, agents_by_name)

    def _handle_action_step(
        self,
        run_id: str,
        step: ProcessStep,
        prev_output: str,
        step_results: list[dict[str, Any]],
        agents_by_name: dict[str, AgenticTeamAgent],
    ) -> str:
        """Build the agent, invoke it, store the result."""
        agent_name, agent_def = self._resolve_agent(step, agents_by_name)
        step_input = (
            f"Task: {step.name}\nDescription: {step.description}\n\n"
            f"Context from previous step:\n{prev_output}"
        )
        if agent_def:
            output = self._run_agent(agent_def, step_input)
        else:
            output = f"[No agent assigned to step '{step.name}']"

        self._record_step(
            run_id,
            step_results,
            {
                "step_id": step.step_id,
                "step_name": step.name,
                "agent_name": agent_name,
                "input": prev_output,
                "output": output,
                "status": "completed",
            },
        )
        return output

    def _handle_wait_step(
        self,
        run_id: str,
        step: ProcessStep,
        prev_output: str,
        step_results: list[dict[str, Any]],
        resume_event: threading.Event,
    ) -> Optional[str]:
        """Pause execution and wait (bounded) for human input.

        Preconditions: called on the worker thread that owns ``resume_event``.
        Postconditions:
            - Returns the submitted human input (a str, possibly empty) when the run is
              resumed — from this worker's event or, cross-worker, from the DB flip.
            - Returns ``None`` when the run reached a terminal state while waiting: it
              timed out (this call claims the ``failed`` transition via compare-and-swap
              and records a ``timed_out`` step), or it was cancelled/reaped by another
              actor (the pending WAIT step is marked to match so the audit panel is
              consistent). In every ``None`` case the run row is already terminal, so
              the caller must stop without overwriting it.
        """
        prompt_text = step.description or f"Human input required for: {step.name}"

        result = {
            "step_id": step.step_id,
            "step_name": step.name,
            "agent_name": "",
            "input": prev_output,
            "output": "",
            "status": "waiting_for_input",
        }
        step_results.append(result)

        # Clear BEFORE publishing waiting_for_input: a submit that lands between the
        # publish and the first wait must not have its signal erased by a late clear().
        resume_event.clear()
        self._store.update_pipeline_run(
            run_id,
            status="waiting_for_input",
            human_prompt=prompt_text,
            step_results=step_results,
        )

        deadline = time.monotonic() + self._wait_timeout_s
        while True:
            # Status-first: a cheap read (no step_results marshalling) that also carries
            # the persisted answer, so a resume needs no second SELECT.
            row = self._store.get_pipeline_status(run_id)
            status = row["status"] if row else None

            if status == "running":
                # Resumed here or on another worker — the answer rode along on the read.
                human_input = row["human_input"]
                result["output"] = human_input
                result["status"] = "completed"
                self._store.update_pipeline_run(run_id, step_results=step_results)
                return human_input
            if status in ("cancelled", "failed", "completed"):
                # Cancelled, expired, or reaped by another actor. Reconcile the WAIT
                # step so the audit panel doesn't show a step still "waiting" under a
                # terminated run, then stop.
                if status in ("cancelled", "failed"):
                    result["status"] = status
                    self._store.update_pipeline_run(run_id, step_results=step_results)
                return None

            # Still waiting_for_input. Claim the timeout once the deadline elapses; if a
            # concurrent resume/cancel won the row, the next loop's read observes it.
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                error = (
                    f"wait_timeout: no human input for WAIT step '{step.name}' "
                    f"within {self._wait_timeout_s}s"
                )
                if self._store.try_expire_pipeline_run(run_id, error):
                    result["status"] = "timed_out"
                    result["output"] = (
                        f"Timed out after {self._wait_timeout_s}s waiting for human input"
                    )
                    self._store.update_pipeline_run(run_id, step_results=step_results)
                    logger.warning(
                        "Pipeline run %s timed out at WAIT step %s", run_id, step.step_id
                    )
                    return None
                continue  # lost the expire race — re-read immediately, no sleep
            resume_event.wait(timeout=min(self._wait_poll_s, remaining))

    def _handle_decision_step(
        self,
        run_id: str,
        step: ProcessStep,
        prev_output: str,
        step_results: list[dict[str, Any]],
        agents_by_name: dict[str, AgenticTeamAgent],
    ) -> str:
        """Evaluate condition and record the decision."""
        agent_name, agent_def = self._resolve_agent(step, agents_by_name)
        condition_prompt = (
            f"Decision step: {step.name}\n"
            f"Condition: {step.condition or 'Choose the best next step'}\n"
            f"Previous output:\n{prev_output}\n\n"
            f"Available branches: {', '.join(step.next_steps)}\n"
            f"Which branch should be taken? Reply with only the step_id."
        )

        if agent_def:
            decision = self._run_agent(agent_def, condition_prompt)
        else:
            decision = step.next_steps[0] if step.next_steps else "none"

        self._record_step(
            run_id,
            step_results,
            {
                "step_id": step.step_id,
                "step_name": step.name,
                "agent_name": agent_name,
                "input": prev_output,
                "output": f"Decision: {decision}",
                # The bare branch id this method returns (threaded as the next step's
                # prev_output) — distinct from the "Decision: ..." display ``output``, so a
                # Temporal activity replaying a completed decision returns the same value a
                # non-crash run would (see ``run_step_activity``'s idempotency guard).
                "output_raw": decision,
                "status": "completed",
            },
        )
        return decision

    @staticmethod
    def _topological_sort(steps: list[ProcessStep]) -> list[ProcessStep]:
        """Sort steps in execution order following next_steps edges.

        Finds entry points (steps not referenced as next_step by any
        other step) and walks the DAG breadth-first. Falls back to the
        original order if the graph structure is ambiguous.

        Delegates the pure ordering to ``step_ordering.order_step_ids`` so the
        Temporal workflow can reuse the identical algorithm without importing this
        heavyweight runtime module into its sandbox.
        """
        step_map = {s.step_id: s for s in steps}
        order = order_step_ids([(s.step_id, s.next_steps) for s in steps])
        return [step_map[sid] for sid in order]


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_default_runner: Optional[PipelineRunner] = None


def get_pipeline_runner(store: AgenticTestStore) -> PipelineRunner:
    global _default_runner  # noqa: PLW0603
    if _default_runner is None:
        _default_runner = PipelineRunner(store)
    return _default_runner
