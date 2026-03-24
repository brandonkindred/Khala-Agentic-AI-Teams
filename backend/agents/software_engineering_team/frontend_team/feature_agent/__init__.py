"""Compatibility shim: re-exports from frontend_team_deprecated.feature_agent."""

from frontend_team_deprecated.feature_agent import (  # noqa: F401
    FrontendExpertAgent,
    FrontendInput,
    FrontendOutput,
)
from frontend_team_deprecated.feature_agent.models import FrontendWorkflowResult  # noqa: F401

__all__ = [
    "FrontendExpertAgent",
    "FrontendInput",
    "FrontendOutput",
    "FrontendWorkflowResult",
]
