"""Execution phase: run tool agents against planned microtasks."""

from __future__ import annotations

from typing import Callable, Dict, List

from ..models import (
    ExecutionResult,
    Microtask,
    MicrotaskStatus,
    PlanningResult,
    ToolAgentInput,
    ToolAgentKind,
    ToolAgentOutput,
)

ToolRunner = Callable[[ToolAgentInput], ToolAgentOutput]


def run_execution(
    *,
    planning_result: PlanningResult,
    repo_path: str,
    spec_context: str,
    existing_code: str,
    tool_runners: Dict[ToolAgentKind, ToolRunner],
) -> ExecutionResult:
    """Run each planned microtask's tool agent once, in planner order.

    Preconditions: ``tool_runners`` must contain an entry for
      ``ToolAgentKind.GENERAL`` (used as the fallback runner for any
      microtask whose ``tool_agent`` has no dedicated entry); the caller
      (``AIAgentDevelopmentTeamLead._build_tool_runners``) guarantees this.
      ``planning_result.microtasks`` may be empty.
    Postconditions: returns an ``ExecutionResult`` whose ``microtasks`` list
      has one entry per input microtask (the same objects, mutated in place),
      each with ``status`` set to ``COMPLETED`` or ``FAILED`` based on the
      runner's ``out.success`` and ``notes`` set from the runner summary.
      ``microtask.output_files`` is populated only for successful microtasks
      and cleared for failed ones; ``ExecutionResult.files`` is the union of
      every runner's returned files (including failed runs). Does not handle
      ``Microtask.depends_on`` — microtasks always run once, in planner order,
      regardless of dependency status.
    """
    files: Dict[str, str] = {}
    notes: List[str] = []
    updated_microtasks: List[Microtask] = []

    for microtask in planning_result.microtasks:
        runner = tool_runners.get(microtask.tool_agent) or tool_runners[ToolAgentKind.GENERAL]
        microtask.status = MicrotaskStatus.IN_PROGRESS

        out = runner(
            ToolAgentInput(
                microtask=microtask,
                repo_path=repo_path,
                spec_context=spec_context,
                existing_code=existing_code,
            )
        )

        if out.success:
            microtask.status = MicrotaskStatus.COMPLETED
            microtask.output_files = out.files or {}
        else:
            microtask.status = MicrotaskStatus.FAILED
            microtask.output_files = {}

        microtask.notes = out.summary or ""
        files.update(out.files)
        notes.extend(out.recommendations or [])
        updated_microtasks.append(microtask)

    return ExecutionResult(
        files=files,
        microtasks=updated_microtasks,
        notes=notes,
        summary=f"Executed {len(updated_microtasks)} microtasks and generated {len(files)} files.",
    )
