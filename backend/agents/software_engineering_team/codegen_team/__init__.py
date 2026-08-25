"""
Codegen agent team — config-driven backend/frontend development team.

Delivers backend (Java or Python) and frontend (Angular/React/Vue/TypeScript)
tasks through the same 7-phase workflow: Setup -> Planning -> Execution ->
Review -> Problem Solving -> Documentation -> Deliver, selected at
construction time by a ``stack: Literal["backend", "frontend"]`` parameter
rather than by two separate team implementations.

This team does NOT import or reuse any code from ``backend_agent``,
``frontend_team``, or ``feature_agent``.
"""

from .orchestrator import CodegenDevelopmentAgent, CodegenTeamLead

__all__ = ["CodegenTeamLead", "CodegenDevelopmentAgent"]
