"""
Sub-agent provisioning phase: when capability gap identified, draft agent spec and call AI Systems.

Writes minimal spec to disk, calls AI Systems build, stores blueprint in context.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

SUB_AGENT_SPEC_FILENAME = "sub_agent_spec.md"


def _default_agent_spec_for_gap(capability_gap: str) -> str:
    """Generate a minimal agent spec for the AI Systems Team."""
    return f"""# Sub-agent specification (Planning V3)

## Problem statement
{capability_gap}

## Desired outcome
A single-purpose agent or tool that can perform this capability as part of the Planning V3 workflow.

## Constraints
- Must be invocable from Python or via HTTP.
- Inputs and outputs should be clearly defined.
- No human-in-the-loop required unless the capability inherently needs approval.

## Non-goals
- Full multi-agent system; only this capability is in scope.
"""


def run_sub_agent_provisioning(
    context: Dict[str, Any],
    capability_gap: Optional[str] = None,
    start_build_fn: Optional[Callable[..., Optional[str]]] = None,
    wait_build_fn: Optional[Callable[..., Dict[str, Any]]] = None,
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    """
    Run sub-agent provisioning when a capability gap is identified.

    If capability_gap is None or empty, skip. Otherwise write a minimal spec to
    repo_path/plan/sub_agent_spec.md, call AI Systems build, wait for completion,
    and attach blueprint to context.
    start_build_fn(project_name, spec_path, constraints?, output_dir?) -> job_id
    wait_build_fn(job_id) -> status dict with optional blueprint.
    Returns (context_update, artifacts).
    """
    context_update: Dict[str, Any] = {}
    artifacts: Dict[str, Any] = {}

    if not capability_gap or not capability_gap.strip():
        return context_update, artifacts

    repo_path = context.get("repo_path", "")
    if not repo_path or not start_build_fn or not wait_build_fn:
        logger.debug("Sub-agent provisioning skipped: missing repo_path or adapter.")
        return context_update, artifacts

    path = Path(repo_path)
    path.mkdir(parents=True, exist_ok=True)
    (path / "plan").mkdir(parents=True, exist_ok=True)
    spec_path = path / "plan" / SUB_AGENT_SPEC_FILENAME
    spec_content = _default_agent_spec_for_gap(capability_gap)
    spec_path.write_text(spec_content, encoding="utf-8")
    project_name = "planning_v3_sub_agent"

    job_id = start_build_fn(
        project_name=project_name,
        spec_path=str(spec_path),
        constraints={"source": "planning_v3", "capability_gap": capability_gap[:500]},
        output_dir=str(path / "plan" / "sub_agent_output"),
    )
    if not job_id:
        artifacts["sub_agent_provisioning_error"] = "AI Systems build start failed"
        return context_update, artifacts

    result = wait_build_fn(job_id=job_id)
    if result.get("status") == "completed" and result.get("blueprint"):
        blueprint = result.get("blueprint")
        if hasattr(blueprint, "model_dump"):
            blueprint = blueprint.model_dump()
        context_update["sub_agent_blueprint"] = blueprint
        artifacts["sub_agent_blueprint"] = blueprint
    else:
        artifacts["sub_agent_provisioning_error"] = result.get(
            "error", "Build failed or no blueprint"
        )

    return context_update, artifacts
