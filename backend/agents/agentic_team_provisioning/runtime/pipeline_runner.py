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
from datetime import datetime, timezone
from typing import Any, Optional

from agentic_team_provisioning.models import (
    AgenticTeamAgent,
    ProcessDefinition,
    ProcessStep,
    StepType,
)
from agentic_team_provisioning.runtime.agent_builder import build_agent, call_agent
from agentic_team_provisioning.testing.store import AgenticTestStore
from shared_env import parse_int
from shared_postgres import is_postgres_enabled

logger = logging.getLogger(__name__)

# Default bounds for the WAIT-state timeout/liveness knobs. See docs/ENV_VARS.md.
_DEFAULT_WAIT_TIMEOUT_S = 259200  # 72h — tolerates runs left overnight/weekend.
_MIN_WAIT_TIMEOUT_S = 60
_MAX_WAIT_TIMEOUT_S = 604800  # 7d — an upper clamp so a fat-fingered value can't
#                              recreate the original unbounded-wait bug.
_DEFAULT_WAIT_POLL_S = 5
_MIN_WAIT_POLL_S = 1
_MAX_WAIT_POLL_S = 60
_DEFAULT_STALE_S = 30


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


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

        self._wait_timeout_s = parse_int(
            "AGENTIC_TEAM_PIPELINE_WAIT_TIMEOUT_S",
            _DEFAULT_WAIT_TIMEOUT_S,
            minimum=_MIN_WAIT_TIMEOUT_S,
            maximum=_MAX_WAIT_TIMEOUT_S,
        )
        self._wait_poll_s = parse_int(
            "AGENTIC_TEAM_PIPELINE_WAIT_POLL_S",
            _DEFAULT_WAIT_POLL_S,
            minimum=_MIN_WAIT_POLL_S,
            maximum=_MAX_WAIT_POLL_S,
        )
        # Staleness must exceed a couple of poll intervals so a live run that just
        # heartbeated is never mistaken for an orphan.
        self._stale_s = parse_int(
            "AGENTIC_TEAM_PIPELINE_STALE_S",
            _DEFAULT_STALE_S,
            minimum=2 * self._wait_poll_s,
        )
        # Heartbeat comfortably inside the staleness window (half of it) so a live run
        # stays fresh with margin, while keeping the heartbeat write rate modest.
        self._heartbeat_interval_s = max(1, self._stale_s // 2)

        self._sweeper_stop = threading.Event()
        # No Postgres -> the reaper's queries would just raise; skip the sweeper so it
        # doesn't spin logging errors (and leak a thread) in tests / no-DB dev.
        if start_sweeper and is_postgres_enabled():
            threading.Thread(
                target=self._run_sweeper, daemon=True, name="pipeline-orphan-sweeper"
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

    def _run_sweeper(self) -> None:
        """Periodically reap orphans so a single crashed worker's runs don't linger.

        Preconditions: called on a dedicated daemon thread.
        Postconditions: runs until ``_sweeper_stop`` is set (only in tests), otherwise
        for the life of the daemon. A reap failure is logged and retried on the next
        tick — the sweeper must never die silently, or orphans would only be reaped on
        the next restart.
        """
        while not self._sweeper_stop.wait(self._stale_s):
            try:
                reaped = self.reap_orphaned_runs()
                if reaped:
                    logger.warning("Reaped %d orphaned pipeline run(s)", reaped)
            except Exception:
                logger.exception("Pipeline orphan sweeper tick failed; will retry")

    def _heartbeat_loop(self, run_id: str, stop: threading.Event) -> None:
        """Keep ``heartbeat_at`` fresh for the whole time a run executes.

        Runs on its own daemon thread alongside the executor so that even a long
        synchronous ``call_agent`` step (which never yields to the executor) still
        looks alive to the reaper. Heartbeats immediately, then every
        ``_heartbeat_interval_s`` until ``stop`` is set (in the executor's ``finally``).

        Preconditions: ``run_id`` is a non-empty str.
        Postconditions: issues heartbeats until stopped; a heartbeat error is logged and
        retried rather than killing the thread.
        """
        while True:
            try:
                self._store.heartbeat_pipeline_run(run_id)
            except Exception:
                logger.exception("Heartbeat for pipeline run %s failed; will retry", run_id)
            if stop.wait(self._heartbeat_interval_s):
                return

    def _execute(
        self,
        run_id: str,
        team_agents: list[AgenticTeamAgent],
        process: ProcessDefinition,
        resume_event: threading.Event,
    ) -> None:
        """Main pipeline execution loop."""
        stop_heartbeat = threading.Event()
        threading.Thread(
            target=self._heartbeat_loop,
            args=(run_id, stop_heartbeat),
            daemon=True,
            name=f"pipeline-hb-{run_id[:16]}",
        ).start()
        try:
            agents_by_name: dict[str, AgenticTeamAgent] = {a.agent_name: a for a in team_agents}
            step_order = self._topological_sort(process.steps)

            # Use initial_input as starting context for the first step
            run_data = self._store.get_pipeline_run(run_id)
            prev_output = (run_data or {}).get("initial_input") or ""
            step_results: list[dict[str, Any]] = []

            for step in step_order:
                # Stop if the run reached a terminal state out-of-band (cancelled by a
                # user, or reaped/expired by another actor). Checking before each step
                # — and completing via a CAS below — prevents resurrecting a run that
                # has already been finalized.
                run_data = self._store.get_pipeline_run(run_id)
                if run_data and run_data.get("status") in ("cancelled", "failed", "completed"):
                    return

                # Only advance the cursor; status stays 'running' (set at creation and
                # on resume) so this write can't resurrect a concurrently-cancelled run.
                self._store.update_pipeline_run(run_id, current_step_id=step.step_id)

                if step.step_type == StepType.WAIT:
                    resumed = self._handle_wait_step(
                        run_id, step, prev_output, step_results, resume_event
                    )
                    # None => the run reached a terminal state while waiting (timed
                    # out, cancelled, or reaped); stop without overwriting it.
                    if resumed is None:
                        return
                    prev_output = resumed
                elif step.step_type == StepType.DECISION:
                    prev_output = self._handle_decision_step(
                        run_id, step, prev_output, step_results, agents_by_name
                    )
                else:
                    prev_output = self._handle_action_step(
                        run_id, step, prev_output, step_results, agents_by_name
                    )

            # Complete only if still running — a run cancelled/reaped mid-step must keep
            # its terminal state rather than being clobbered back to completed.
            if not self._store.try_complete_pipeline_run(run_id, step_results):
                logger.info("Pipeline run %s finished executing but was already terminal", run_id)
        except Exception as exc:
            logger.exception("Pipeline run %s failed", run_id)
            self._store.update_pipeline_run(
                run_id, status="failed", error=str(exc), finished_at=_now_iso()
            )
        finally:
            stop_heartbeat.set()
            self._resume_events.pop(run_id, None)

    def _handle_action_step(
        self,
        run_id: str,
        step: ProcessStep,
        prev_output: str,
        step_results: list[dict[str, Any]],
        agents_by_name: dict[str, AgenticTeamAgent],
    ) -> str:
        """Build the agent, invoke it, store the result."""
        agent_name = step.agents[0].agent_name if step.agents else ""
        agent_def = agents_by_name.get(agent_name)

        step_input = f"Task: {step.name}\nDescription: {step.description}\n\nContext from previous step:\n{prev_output}"

        if agent_def:
            agent_instance = build_agent(
                agent_def.agent_name,
                agent_def.role,
                agent_def.skills,
                agent_def.capabilities,
                agent_def.tools,
                agent_def.expertise,
            )
            output = call_agent(agent_instance, step_input)
        else:
            output = f"[No agent assigned to step '{step.name}']"

        result = {
            "step_id": step.step_id,
            "step_name": step.name,
            "agent_name": agent_name,
            "input": prev_output,
            "output": output,
            "status": "completed",
        }
        step_results.append(result)
        self._store.update_pipeline_run(run_id, step_results=step_results)
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
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                # Deadline elapsed — try to claim the timeout. If another actor won the
                # row concurrently (resume/cancel), fall through and re-read below.
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

            wait_slice = min(self._wait_poll_s, remaining) if remaining > 0 else self._wait_poll_s
            resume_event.wait(timeout=wait_slice)

            run_data = self._store.get_pipeline_run(run_id)
            status = (run_data or {}).get("status")

            if status == "running":
                # Resumed here or on another worker — read the persisted answer.
                human_input = self._store.consume_pipeline_human_input(run_id)
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
            # Still waiting_for_input -> loop until resumed or the deadline elapses.

    def _handle_decision_step(
        self,
        run_id: str,
        step: ProcessStep,
        prev_output: str,
        step_results: list[dict[str, Any]],
        agents_by_name: dict[str, AgenticTeamAgent],
    ) -> str:
        """Evaluate condition and record the decision."""
        agent_name = step.agents[0].agent_name if step.agents else ""
        agent_def = agents_by_name.get(agent_name)

        condition_prompt = (
            f"Decision step: {step.name}\n"
            f"Condition: {step.condition or 'Choose the best next step'}\n"
            f"Previous output:\n{prev_output}\n\n"
            f"Available branches: {', '.join(step.next_steps)}\n"
            f"Which branch should be taken? Reply with only the step_id."
        )

        if agent_def:
            agent_instance = build_agent(
                agent_def.agent_name,
                agent_def.role,
                agent_def.skills,
                agent_def.capabilities,
                agent_def.tools,
                agent_def.expertise,
            )
            decision = call_agent(agent_instance, condition_prompt)
        else:
            decision = step.next_steps[0] if step.next_steps else "none"

        result = {
            "step_id": step.step_id,
            "step_name": step.name,
            "agent_name": agent_name,
            "input": prev_output,
            "output": f"Decision: {decision}",
            "status": "completed",
        }
        step_results.append(result)
        self._store.update_pipeline_run(run_id, step_results=step_results)
        return decision

    @staticmethod
    def _topological_sort(steps: list[ProcessStep]) -> list[ProcessStep]:
        """Sort steps in execution order following next_steps edges.

        Finds entry points (steps not referenced as next_step by any
        other step) and walks the DAG breadth-first. Falls back to the
        original order if the graph structure is ambiguous.
        """
        if not steps:
            return []

        step_map = {s.step_id: s for s in steps}
        all_next: set[str] = set()
        for s in steps:
            all_next.update(s.next_steps)

        entry_ids = [s.step_id for s in steps if s.step_id not in all_next]
        if not entry_ids:
            entry_ids = [steps[0].step_id]

        visited: set[str] = set()
        ordered: list[ProcessStep] = []
        queue = list(entry_ids)

        while queue:
            sid = queue.pop(0)
            if sid in visited:
                continue
            visited.add(sid)
            step = step_map.get(sid)
            if step:
                ordered.append(step)
                queue.extend(step.next_steps)

        # Include any unreachable steps at the end
        for s in steps:
            if s.step_id not in visited:
                ordered.append(s)

        return ordered


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_default_runner: Optional[PipelineRunner] = None


def get_pipeline_runner(store: AgenticTestStore) -> PipelineRunner:
    global _default_runner  # noqa: PLW0603
    if _default_runner is None:
        _default_runner = PipelineRunner(store)
    return _default_runner
