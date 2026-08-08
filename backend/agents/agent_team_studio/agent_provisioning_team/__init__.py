"""
Agent Provisioning Team

A swarm of agents that provisions sandboxed Docker environments with
configurable tool accounts for AI agents, following an employee-onboarding
model with comprehensive onboarding documentation. Every sandbox is
provisioned with full access on every backing service — there is no
permission-tier ladder (#456); the project is single-operator.

Every AI agent delivered or scaffolded by this team must conform to the standard
anatomy described in AGENT_ANATOMY.md (Input/Output, Tools, Memory tiers, Prompts,
Security Guardrails, Subagents).
"""

from .models import (
    Phase,
    ProvisioningResult,
    ProvisionRequest,
)
from .orchestrator import ProvisioningOrchestrator

__all__ = [
    "Phase",
    "ProvisioningOrchestrator",
    "ProvisioningResult",
    "ProvisionRequest",
]
