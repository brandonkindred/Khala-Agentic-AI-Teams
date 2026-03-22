"""Shared handoff models for the Frontend Engineering Team pipeline."""

from typing import Optional

from pydantic import BaseModel, Field


class UXDesignerOutput(BaseModel):
    """Output from the UX Designer agent."""

    user_journeys: str = Field(
        default="",
        description="User journeys including happy path and sad paths",
    )
    wireframes_summary: str = Field(
        default="",
        description="Wireframes and flow diagrams summary",
    )
    interaction_rules: str = Field(
        default="",
        description="Interaction rules for empty states, errors, loading, success",
    )
    microcopy_guidelines: str = Field(
        default="",
        description="Microcopy guidelines: tone, clarity, consistency",
    )
    summary: str = Field(default="", description="Brief summary of UX design decisions")


class UIDesignerOutput(BaseModel):
    """Output from the UI / Visual Designer agent."""

    component_specs: str = Field(
        default="",
        description="Component specs: states, variants, responsive rules",
    )
    design_tokens: str = Field(
        default="",
        description="Design tokens: colors, typography scale, spacing scale",
    )
    motion_guidelines: str = Field(
        default="",
        description="Motion guidelines: when and how animation is used",
    )
    high_fidelity_summary: str = Field(
        default="",
        description="High-fidelity screens and layout summary",
    )
    summary: str = Field(default="", description="Brief summary of UI design decisions")


class DesignSystemOutput(BaseModel):
    """Output from the Design System & UI Engineering agent."""

    component_library_plan: str = Field(
        default="",
        description="Component library plan: shared vs app-specific components",
    )
    token_implementation_plan: str = Field(
        default="",
        description="Token implementation: CSS variables, theming, dark mode",
    )
    a11y_in_components: str = Field(
        default="",
        description="Accessibility baked into components: focus, keyboard, ARIA patterns",
    )
    documentation_plan: str = Field(
        default="",
        description="Storybook-style documentation plan",
    )
    summary: str = Field(default="", description="Brief summary of design system decisions")


class FrontendArchitectOutput(BaseModel):
    """Output from the Frontend Architect agent."""

    folder_structure: str = Field(
        default="",
        description="Folder/module structure and conventions",
    )
    routing_strategy: str = Field(
        default="",
        description="Routing strategy",
    )
    state_management: str = Field(
        default="",
        description="State management strategy: server state vs UI state",
    )
    error_handling: str = Field(
        default="",
        description="Error handling strategy and global boundary patterns",
    )
    api_client_patterns: str = Field(
        default="",
        description="API client patterns and typing strategy",
    )
    summary: str = Field(default="", description="Brief summary of architecture decisions")


def _summarize_ux(ux: Optional[UXDesignerOutput]) -> str:
    """Produce a concise summary of UX output for Feature Implementation context."""
    if not ux:
        return ""
    parts = []
    if ux.user_journeys:
        parts.append(f"User Journeys:\n{ux.user_journeys}")
    if ux.interaction_rules:
        parts.append(f"Interaction Rules:\n{ux.interaction_rules}")
    if ux.microcopy_guidelines:
        parts.append(f"Microcopy Guidelines:\n{ux.microcopy_guidelines}")
    if ux.summary:
        parts.append(f"UX Summary: {ux.summary}")
    return "\n\n".join(parts) if parts else ""


def _summarize_ui(ui: Optional[UIDesignerOutput]) -> str:
    """Produce a concise summary of UI output for Feature Implementation context."""
    if not ui:
        return ""
    parts = []
    if ui.component_specs:
        parts.append(f"Component Specs:\n{ui.component_specs}")
    if ui.design_tokens:
        parts.append(f"Design Tokens:\n{ui.design_tokens}")
    if ui.motion_guidelines:
        parts.append(f"Motion Guidelines:\n{ui.motion_guidelines}")
    if ui.summary:
        parts.append(f"UI Summary: {ui.summary}")
    return "\n\n".join(parts) if parts else ""


def _summarize_design_system(ds: Optional[DesignSystemOutput]) -> str:
    """Produce a concise summary of Design System output for Feature Implementation context."""
    if not ds:
        return ""
    parts = []
    if ds.component_library_plan:
        parts.append(f"Component Library Plan:\n{ds.component_library_plan}")
    if ds.token_implementation_plan:
        parts.append(f"Token Implementation:\n{ds.token_implementation_plan}")
    if ds.a11y_in_components:
        parts.append(f"A11y in Components:\n{ds.a11y_in_components}")
    if ds.summary:
        parts.append(f"Design System Summary: {ds.summary}")
    return "\n\n".join(parts) if parts else ""


def _summarize_architect(arch: Optional[FrontendArchitectOutput]) -> str:
    """Produce a concise summary of Architect output for Feature Implementation context."""
    if not arch:
        return ""
    parts = []
    if arch.folder_structure:
        parts.append(f"Folder Structure:\n{arch.folder_structure}")
    if arch.routing_strategy:
        parts.append(f"Routing Strategy:\n{arch.routing_strategy}")
    if arch.state_management:
        parts.append(f"State Management:\n{arch.state_management}")
    if arch.error_handling:
        parts.append(f"Error Handling:\n{arch.error_handling}")
    if arch.api_client_patterns:
        parts.append(f"API Client Patterns:\n{arch.api_client_patterns}")
    if arch.summary:
        parts.append(f"Architecture Summary: {arch.summary}")
    return "\n\n".join(parts) if parts else ""


def build_feature_implementation_context(
    ux: Optional[UXDesignerOutput] = None,
    ui: Optional[UIDesignerOutput] = None,
    design_system: Optional[DesignSystemOutput] = None,
    architect: Optional[FrontendArchitectOutput] = None,
) -> str:
    """Build enriched context string for Feature Implementation (FrontendExpertAgent)."""
    sections = []
    ux_sum = _summarize_ux(ux)
    if ux_sum:
        sections.append("--- Design & UX Context ---\n" + ux_sum)
    ui_sum = _summarize_ui(ui)
    if ui_sum:
        sections.append("--- UI & Visual Design Context ---\n" + ui_sum)
    ds_sum = _summarize_design_system(design_system)
    if ds_sum:
        sections.append("--- Design System Context ---\n" + ds_sum)
    arch_sum = _summarize_architect(architect)
    if arch_sum:
        sections.append("--- Architecture Context ---\n" + arch_sum)
    return "\n\n".join(sections) if sections else ""
