"""CI/CD Pipeline Agent — creates secure CI/CD workflows with gates.

Generates build/test/lint/scan/deploy workflows with explicit production
approval gates, OIDC auth preference, and environment promotion logic.
"""

from .agent import CICDPipelineAgent
from .models import CICDPipelineAgentInput, CICDPipelineAgentOutput

__all__ = ["CICDPipelineAgent", "CICDPipelineAgentInput", "CICDPipelineAgentOutput"]
