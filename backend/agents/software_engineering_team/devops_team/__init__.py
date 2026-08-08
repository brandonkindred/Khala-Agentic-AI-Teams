"""DevOps Engineering Team — contract-first, multi-agent DevOps orchestration.

MVP fleet: 9 core agents + 5 tool agents coordinated by DevOpsTeamLeadAgent.
Provides hard gates, environment-aware safety (dev/staging/prod), structured
completion packages with acceptance-criteria trace, a legacy free-text
run_workflow() adapter, and the structured run_task() entry point the
coding-team's opt-in devops handoff (CODING_TEAM_DEVOPS_ROUTING) uses.
"""

from .models import DevOpsCompletionPackage, DevOpsTaskSpec, DevOpsTeamResult
from .orchestrator import DevOpsTeamLeadAgent

__all__ = [
    "DevOpsTeamLeadAgent",
    "DevOpsTaskSpec",
    "DevOpsCompletionPackage",
    "DevOpsTeamResult",
]
