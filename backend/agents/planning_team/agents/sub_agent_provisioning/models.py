"""Boundary contracts for the Sub-agent Provisioning agent (§1 typed Input/Output)."""

from __future__ import annotations

from typing import Any, Dict, Optional

from pydantic import BaseModel


class SubAgentProvisioningInput(BaseModel):
    """Request to provision a helper agent for an identified capability gap."""

    repo_path: str = ""
    capability_gap: Optional[str] = None


class SubAgentProvisioningOutput(BaseModel):
    """Outcome of a provisioning attempt.

    Fields map to the legacy ``(context_update, artifacts)`` seam:
        - ``sub_agent_blueprint`` (when set) is emitted to both
          ``context_update["sub_agent_blueprint"]`` and
          ``artifacts["sub_agent_blueprint"]``.
        - ``error`` (when set, and no blueprint) is emitted to
          ``artifacts["sub_agent_provisioning_error"]``.
        - Both ``None`` means the phase was skipped (no gap / missing repo or adapters).
    """

    sub_agent_blueprint: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
