"""DevOps Engineering Team — contract-first, multi-agent DevOps orchestration.

MVP fleet: 9 core agents + 5 tool agents coordinated by DevOpsTeamLeadAgent.
Provides hard gates, environment-aware safety (dev/staging/prod), structured
completion packages with acceptance-criteria trace, and backward-compatible
run_workflow() for existing Tech Lead integration.
"""

from .models import DevOpsCompletionPackage, DevOpsTaskSpec, DevOpsTeamResult
from .orchestrator import DevOpsTeamLeadAgent

__all__ = [
    "DevOpsTeamLeadAgent",
    "DevOpsTaskSpec",
    "DevOpsCompletionPackage",
    "DevOpsTeamResult",
]
