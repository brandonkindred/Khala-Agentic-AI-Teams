"""
Agent Provisioning Team

A swarm of agents that provisions sandboxed Docker environments with configurable
tool accounts for AI agents, following an employee-onboarding model with least-privilege
access and comprehensive onboarding documentation.

Every AI agent delivered or scaffolded by this team must conform to the standard
anatomy described in AGENT_ANATOMY.md (Input/Output, Tools, Memory tiers, Prompts,
Security Guardrails, Subagents).
"""

from .models import (
    AccessTier,
    Phase,
    ProvisioningResult,
    ProvisionRequest,
)
from .orchestrator import ProvisioningOrchestrator

__all__ = [
    "AccessTier",
    "Phase",
    "ProvisioningOrchestrator",
    "ProvisioningResult",
    "ProvisionRequest",
]
