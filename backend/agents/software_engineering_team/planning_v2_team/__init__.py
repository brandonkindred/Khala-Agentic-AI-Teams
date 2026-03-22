"""
Planning-V2 agent team — standalone product planning team.

3-layer architecture:
- Layer 1: PlanningV2ProductLead (top) - handles spec intake, inspiration, feedback
- Layer 2: PlanningV2PlanningAgent (middle) - orchestrates 8 tool agents across phases
- Layer 3: 8 Product Planning Tool Agents (bottom) - specialized planning agents

Delivers planning through a 6-phase cycle:
Spec Review and Gap analysis → Planning → Implementation → Review → Problem-solving → Deliver.

This team does NOT import or reuse any code from ``planning_team`` or ``project_planning_agent``.
"""

from .models import Phase, ToolAgentKind
from .orchestrator import (
    PlanningV2PlanningAgent,
    PlanningV2ProductLead,
    PlanningV2TeamLead,
)

__all__ = [
    "PlanningV2TeamLead",
    "PlanningV2ProductLead",
    "PlanningV2PlanningAgent",
    "Phase",
    "ToolAgentKind",
]
