"""Compatibility shim: re-exports from frontend_team_deprecated."""

from frontend_team_deprecated import (  # noqa: F401
    DesignSystemOutput,
    FrontendArchitectOutput,
    FrontendExpertAgent,
    FrontendInput,
    FrontendOrchestratorAgent,
    FrontendOutput,
    FrontendWorkflowResult,
    UIDesignerOutput,
    UXDesignerOutput,
    build_feature_implementation_context,
)

__all__ = [
    "DesignSystemOutput",
    "FrontendArchitectOutput",
    "FrontendExpertAgent",
    "FrontendInput",
    "FrontendOrchestratorAgent",
    "FrontendOutput",
    "FrontendWorkflowResult",
    "UIDesignerOutput",
    "UXDesignerOutput",
    "build_feature_implementation_context",
]
