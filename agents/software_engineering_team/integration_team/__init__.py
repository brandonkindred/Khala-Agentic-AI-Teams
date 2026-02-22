"""
Integration and release: post-execution agents (Integration, DevOps, Documentation).

These agents run after backend and frontend workers complete. Tech Lead triggers DevOps and Documentation.
"""

from __future__ import annotations

# Integration agent (in-package)
from .agent import IntegrationAgent
from .models import IntegrationInput, IntegrationIssue, IntegrationOutput

# Re-exports for discoverability
from devops_agent import DevOpsExpertAgent, DevOpsInput
from technical_writers.documentation_agent import DocumentationAgent, DocumentationInput

__all__ = [
    "IntegrationAgent",
    "IntegrationInput",
    "IntegrationIssue",
    "IntegrationOutput",
    "DevOpsExpertAgent",
    "DevOpsInput",
    "DocumentationAgent",
    "DocumentationInput",
]
