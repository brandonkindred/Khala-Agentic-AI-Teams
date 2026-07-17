"""
Sub-agent provisioning phase: when capability gap identified, draft agent spec and call AI Systems.

Thin backward-compatible adapter over
``planning_team.agents.sub_agent_provisioning.SubAgentProvisioningAgent``: maps the
``context`` + ``capability_gap`` to the agent's typed Input, injects the AI-Systems build
tools, and reconstructs the exact ``(context_update, artifacts)`` tuple from the typed Output.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional


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
    from planning_team.agents.sub_agent_provisioning import (
        SubAgentProvisioningAgent,
        SubAgentProvisioningInput,
    )

    out = SubAgentProvisioningAgent().run(
        SubAgentProvisioningInput(
            repo_path=context.get("repo_path", ""),
            capability_gap=capability_gap,
        ),
        start_build_fn=start_build_fn,
        wait_build_fn=wait_build_fn,
    )
    context_update: Dict[str, Any] = {}
    artifacts: Dict[str, Any] = {}
    if out.sub_agent_blueprint is not None:
        context_update["sub_agent_blueprint"] = out.sub_agent_blueprint
        artifacts["sub_agent_blueprint"] = out.sub_agent_blueprint
    elif out.error is not None:
        artifacts["sub_agent_provisioning_error"] = out.error
    return context_update, artifacts
