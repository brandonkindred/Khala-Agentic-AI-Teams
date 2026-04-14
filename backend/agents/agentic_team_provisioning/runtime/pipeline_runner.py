"""Pipeline runner: walks a ProcessDefinition DAG step-by-step.

Runs in a background thread. Auto-advances through ACTION, DECISION,
PARALLEL_SPLIT, PARALLEL_JOIN, and SUBPROCESS steps. Pauses at WAIT
steps until human input is submitted via the API.

Follows the background-thread pattern from ``ai_systems_team/api/main.py``.
"""

from __future__ import annotations

import logging
import threading
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

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


class PipelineRunner:
    """Walks the process DAG, runs agents at each step, pauses at WAIT steps."""

    def __init__(self, store: AgenticTestStore) -> None:
        self._store = store
        self._resume_events: dict[str, threading.Event] = {}
        self._human_inputs: dict[str, str] = {}

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

    def submit_human_input(self, run_id: str, user_input: str) -> None:
        """Resume a paused pipeline run with human input."""
        self._human_inputs[run_id] = user_input
        self._store.update_pipeline_run(run_id, status="running", human_prompt=None)
        event = self._resume_events.get(run_id)
        if event:
            event.set()

    def cancel_run(self, run_id: str) -> None:
        """Cancel a running or waiting pipeline run."""
        self._store.update_pipeline_run(run_id, status="cancelled", finished_at=_now_iso())
        event = self._resume_events.get(run_id)
        if event:
            event.set()

    def _execute(
        self,
        run_id: str,
        team_agents: list[AgenticTeamAgent],
        process: ProcessDefinition,
        resume_event: threading.Event,
    ) -> None:
        """Main pipeline execution loop."""
        try:
            agents_by_name: dict[str, AgenticTeamAgent] = {a.agent_name: a for a in team_agents}
            step_order = self._topological_sort(process.steps)

            # Use initial_input as starting context for the first step
            run_data = self._store.get_pipeline_run(run_id)
            prev_output = (run_data or {}).get("initial_input") or ""
            step_results: list[dict[str, Any]] = []

            for step in step_order:
                # Check for cancellation before each step
                run_data = self._store.get_pipeline_run(run_id)
                if run_data and run_data.get("status") == "cancelled":
                    return

                self._store.update_pipeline_run(
                    run_id, current_step_id=step.step_id, status="running"
                )

                if step.step_type == StepType.WAIT:
                    prev_output = self._handle_wait_step(
                        run_id, step, prev_output, step_results, resume_event
                    )
                    # Check if cancelled while waiting
                    run_data = self._store.get_pipeline_run(run_id)
                    if run_data and run_data.get("status") == "cancelled":
                        return
                elif step.step_type == StepType.DECISION:
                    prev_output = self._handle_decision_step(
                        run_id, step, prev_output, step_results, agents_by_name
                    )
                else:
                    prev_output = self._handle_action_step(
                        run_id, step, prev_output, step_results, agents_by_name
                    )

            self._store.update_pipeline_run(
                run_id,
                status="completed",
                step_results=step_results,
                finished_at=_now_iso(),
            )
        except Exception as exc:
            logger.exception("Pipeline run %s failed", run_id)
            self._store.update_pipeline_run(
                run_id, status="failed", error=str(exc), finished_at=_now_iso()
            )
        finally:
            self._resume_events.pop(run_id, None)
            self._human_inputs.pop(run_id, None)

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
            "input": prev_output[:500],
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
    ) -> str:
        """Pause execution and wait for human input."""
        prompt_text = step.description or f"Human input required for: {step.name}"

        result = {
            "step_id": step.step_id,
            "step_name": step.name,
            "agent_name": "",
            "input": prev_output[:500],
            "output": "",
            "status": "waiting_for_input",
        }
        step_results.append(result)
        self._store.update_pipeline_run(
            run_id,
            status="waiting_for_input",
            human_prompt=prompt_text,
            step_results=step_results,
        )

        # Block until human input arrives or run is cancelled
        resume_event.clear()
        resume_event.wait()

        # Retrieve the human input
        human_input = self._human_inputs.pop(run_id, "")
        result["output"] = human_input
        result["status"] = "completed"
        self._store.update_pipeline_run(run_id, step_results=step_results)
        return human_input

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
            "input": prev_output[:500],
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
