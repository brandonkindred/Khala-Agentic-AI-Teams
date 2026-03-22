"""
Intake phase: client identity, initial brief/spec, existing artifacts.

Builds initial ClientContext from request inputs.
"""

from __future__ import annotations

from typing import Any, Dict, List

from planning_v3_team.models import ClientContext


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
    client_context = ClientContext(
        client_name=client_name,
        raw_brief=initial_brief,
        raw_spec=spec_content,
        existing_artifacts=existing_artifacts or [],
    )
    context_update: Dict[str, Any] = {
        "client_context": client_context,
        "repo_path": repo_path,
        "initial_brief": initial_brief or "",
        "spec_content": spec_content or "",
    }
    artifacts: Dict[str, Any] = {
        "client_context": client_context.model_dump(),
    }
    return context_update, artifacts
