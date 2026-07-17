"""
Intake phase: client identity, initial brief/spec, existing artifacts.

Thin backward-compatible adapter over ``planning_team.agents.intake.IntakeAgent``:
maps the request fields to the agent's typed Input and maps the typed Output back to
the ``(context_update, artifacts)`` tuple the orchestrator/Temporal callers expect.
"""

from __future__ import annotations

from typing import Any, Dict, List


def run_intake(
    repo_path: str,
    client_name: str | None = None,
    initial_brief: str | None = None,
    spec_content: str | None = None,
    existing_artifacts: List[str] | None = None,
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    """
    Run intake phase: build initial context from client name, brief, spec, and artifact paths.

    Returns (context_update, artifacts). context_update should be merged into the main
    workflow context; artifacts is a dict of phase outputs (e.g. client_context).
    """
    from planning_team.agents.intake import IntakeAgent, IntakeInput

    out = IntakeAgent().run(
        IntakeInput(
            repo_path=repo_path,
            client_name=client_name,
            initial_brief=initial_brief,
            spec_content=spec_content,
            existing_artifacts=existing_artifacts,
        )
    )
    context_update: Dict[str, Any] = {
        "client_context": out.client_context,
        "repo_path": out.repo_path,
        "initial_brief": out.initial_brief,
        "spec_content": out.spec_content,
    }
    artifacts: Dict[str, Any] = {
        "client_context": out.client_context.model_dump(),
    }
    return context_update, artifacts
