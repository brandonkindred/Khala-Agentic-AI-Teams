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
