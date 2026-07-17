"""Sub-agent provisioning agent: draft a spec and call AI Systems for a capability gap.

Writes a minimal spec to disk and delegates the build to the AI Systems team via the
injected ``start_build_fn``/``wait_build_fn`` tools (§3). Deterministic apart from those
tool calls.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from planning_team.agents.sub_agent_provisioning.models import (
    SubAgentProvisioningInput,
    SubAgentProvisioningOutput,
)

logger = logging.getLogger(__name__)

SUB_AGENT_SPEC_FILENAME = "sub_agent_spec.md"


def _default_agent_spec_for_gap(capability_gap: str) -> str:
    """Generate a minimal agent spec for the AI Systems Team."""
    return f"""# Sub-agent specification (Planning)

## Problem statement
{capability_gap}

## Desired outcome
A single-purpose agent or tool that can perform this capability as part of the Planning workflow.

## Constraints
- Must be invocable from Python or via HTTP.
- Inputs and outputs should be clearly defined.
- No human-in-the-loop required unless the capability inherently needs approval.

## Non-goals
- Full multi-agent system; only this capability is in scope.
"""


class SubAgentProvisioningAgent:
    """Stateless agent that provisions a helper agent for a capability gap.

    Invariants:
        - Holds no mutable state; a single instance is safe to reuse across runs.
    """

    def run(
        self,
        input_data: SubAgentProvisioningInput,
        *,
        start_build_fn: Optional[Callable[..., Optional[str]]] = None,
        wait_build_fn: Optional[Callable[..., Dict[str, Any]]] = None,
    ) -> SubAgentProvisioningOutput:
        """Provision a sub-agent when a capability gap is identified.

        Preconditions:
            - ``start_build_fn(project_name, spec_path, constraints?, output_dir?) -> job_id``
              and ``wait_build_fn(job_id) -> status dict`` when provisioning should run.
        Postconditions:
            - Skips (both output fields ``None``) when ``capability_gap`` is empty or a
              ``repo_path``/build tool is missing.
            - On a completed build with a blueprint, ``sub_agent_blueprint`` is the
              blueprint (``model_dump``'d if it is a model); otherwise ``error`` carries
              the failure message.
        """
        capability_gap = input_data.capability_gap
        if not capability_gap or not capability_gap.strip():
            return SubAgentProvisioningOutput()

        repo_path = input_data.repo_path or ""
        if not repo_path or not start_build_fn or not wait_build_fn:
            logger.debug("Sub-agent provisioning skipped: missing repo_path or adapter.")
            return SubAgentProvisioningOutput()

        path = Path(repo_path)
        path.mkdir(parents=True, exist_ok=True)
        (path / "plan").mkdir(parents=True, exist_ok=True)
        spec_path = path / "plan" / SUB_AGENT_SPEC_FILENAME
        spec_content = _default_agent_spec_for_gap(capability_gap)
        spec_path.write_text(spec_content, encoding="utf-8")
        project_name = "planning_sub_agent"

        job_id = start_build_fn(
            project_name=project_name,
            spec_path=str(spec_path),
            constraints={"source": "planning", "capability_gap": capability_gap},
            output_dir=str(path / "plan" / "sub_agent_output"),
        )
        if not job_id:
            return SubAgentProvisioningOutput(error="AI Systems build start failed")

        result = wait_build_fn(job_id=job_id)
        if result.get("status") == "completed" and result.get("blueprint"):
            blueprint = result.get("blueprint")
            if hasattr(blueprint, "model_dump"):
                blueprint = blueprint.model_dump()
            return SubAgentProvisioningOutput(sub_agent_blueprint=blueprint)

        return SubAgentProvisioningOutput(error=result.get("error", "Build failed or no blueprint"))
