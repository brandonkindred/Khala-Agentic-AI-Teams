"""
Content plan models for the planning-first blogging pipeline.

Single source of truth for structure before the author draft; replaces BlogReviewAgent output.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, List, Optional

from pydantic import BaseModel, Field


class PlanningFailureReason(str, Enum):
    """Why planning stopped without an acceptable plan."""

    MAX_ITERATIONS_REACHED = "max_iterations_reached"
    INFEASIBLE_SCOPE = "infeasible_scope"
    PARSE_FAILURE = "parse_failure"
    MODEL_ABORT = "model_abort"


class TitleCandidate(BaseModel):
    """A candidate title with estimated audience fit."""

    title: str = Field(..., description="Catchy title for the post.")
    probability_of_success: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Estimated probability (0–1) of strong reader engagement.",
    )


class RequirementsAnalysis(BaseModel):
    """Structured self-critique; drives the refine loop."""

    plan_acceptable: bool = Field(
        ...,
        description="True if the plan is coherent and complete enough to author.",
    )
    scope_feasible: bool = Field(
        ...,
        description="True if scope fits content_profile / length_policy.",
    )
    research_gaps: List[str] = Field(
        default_factory=list,
        description="Where planned coverage is not supported by the research digest.",
    )
    fits_profile: bool = Field(default=True, description="Aligns with chosen content profile.")
    gaps: List[str] = Field(
        default_factory=list, description="Structural or coverage gaps in the plan."
    )
    risks: List[str] = Field(
        default_factory=list, description="Risks to clarity, flow, or feasibility."
    )
    suggested_format_change: Optional[str] = Field(
        None,
        description="If scope is wrong for the brief, suggest a different format (e.g. deep_dive).",
    )


class ContentPlanSection(BaseModel):
    """One section of the post with explicit coverage intent."""

    title: str = Field(..., description="Section heading (H2-level).")
    coverage_description: str = Field(
        ...,
        description="What this section must cover (substance, not filler).",
    )
    order: int = Field(0, ge=0, description="Section order (0-based).")
    research_support_note: Optional[str] = Field(
        None,
        description="How research supports this section, or None if gap.",
    )
    gap_flag: bool = Field(
        False,
        description="True if this section lacks research support (intentional gap to address carefully).",
    )


class ContentPlan(BaseModel):
    """Approved or in-refinement content plan for the author step."""

    overarching_topic: str = Field(..., description="Single sentence: what the post is about.")
    narrative_flow: str = Field(
        ...,
        description="How the post progresses from opening to close (not only a list of headings).",
    )
    sections: List[ContentPlanSection] = Field(
        ..., min_length=1, description="Ordered sections with coverage."
    )
    title_candidates: List[TitleCandidate] = Field(
        default_factory=list,
        description="Ranked title options with probabilities.",
    )
    requirements_analysis: RequirementsAnalysis = Field(
        ...,
        description="Latest analysis; refine until plan_acceptable and scope_feasible.",
    )
    plan_version: int = Field(1, ge=1, description="Monotonic version after refinements.")


class PlanningInput(BaseModel):
    """Inputs for the planning agent."""

    brief: str
    audience: Optional[str] = None
    tone_or_purpose: Optional[str] = None
    research_digest: str = Field(
        ...,
        description="Bounded digest of research (claims, sources); capped by caller.",
    )
    length_policy_context: str = Field(
        ...,
        description="Length/profile guidance (e.g. build_review_length_context).",
    )
    series_context_block: Optional[str] = Field(
        None,
        description="Optional series instalment block text.",
    )


class PlanningPhaseResult(BaseModel):
    """Result of the planning phase + observability."""

    content_plan: ContentPlan
    planning_iterations_used: int = Field(..., ge=1)
    parse_retry_count: int = Field(0, ge=0)
    planning_wall_ms_total: float = Field(0.0, ge=0.0)
    planning_failure_reason: Optional[PlanningFailureReason] = None


# --- Section count expectations by profile (for post-validation / thresholds) ---

_PROFILE_SECTION_BOUNDS = {
    "short_listicle": (3, 7),
    "standard_article": (4, 10),
    "technical_deep_dive": (6, 14),
    "series_instalment": (4, 10),
}


def section_count_bounds_for_profile(content_profile_value: str) -> tuple[int, int]:
    """Return (min_sections, max_sections) for the given profile enum value."""
    return _PROFILE_SECTION_BOUNDS.get(content_profile_value, (4, 10))


def content_plan_summary_text(plan: ContentPlan, *, max_chars: int = 800) -> str:
    """
    Short human-readable summary for API responses (topic + narrative flow, truncated).
    """
    topic = (plan.overarching_topic or "").strip()
    flow = (plan.narrative_flow or "").strip()
    body = f"{topic}\n\n{flow}".strip()
    if len(body) <= max_chars:
        return body
    return body[: max_chars - 3].rstrip() + "..."


def content_plan_to_outline_markdown(plan: ContentPlan) -> str:
    """Flat outline string for API `outline` field and legacy-shaped prompts."""
    lines: List[str] = [
        f"# {plan.overarching_topic}",
        "",
        "## Narrative flow",
        plan.narrative_flow,
        "",
        "## Sections",
    ]
    for sec in sorted(plan.sections, key=lambda s: s.order):
        lines.append(f"### {sec.title}")
        lines.append(sec.coverage_description)
        if sec.research_support_note:
            lines.append(f"_(Research: {sec.research_support_note})_")
        if sec.gap_flag:
            lines.append("_(Gap: limited direct research support — handle carefully.)_")
        lines.append("")
    return "\n".join(lines).strip()


def content_plan_to_markdown_doc(plan: ContentPlan) -> str:
    """Human-readable markdown artifact (titles, outline, requirements analysis)."""
    base = content_plan_to_content_brief_markdown(plan)
    analysis_json = plan.requirements_analysis.model_dump_json(indent=2)
    return base + "\n\n## Requirements analysis (JSON)\n\n```json\n" + analysis_json + "\n```\n"


def content_plan_to_content_brief_markdown(plan: ContentPlan) -> str:
    """Human-readable brief with titles + outline body."""
    brief = "# Content Brief\n\n## Title choices\n"
    for i, tc in enumerate(plan.title_candidates, 1):
        brief += f"{i}. {tc.title} [{tc.probability_of_success:.0%}]\n"
    brief += "\n## Outline\n\n"
    brief += content_plan_to_outline_markdown(plan)
    return brief


def build_research_digest(
    research_document: str, *, max_chars: int = 200_000, llm: Any = None
) -> str:
    """Bounded digest for planning prompts (compact with LLM when over budget)."""
    doc = (research_document or "").strip()
    if not doc or len(doc) <= max_chars:
        return doc
    if llm is not None:
        from llm_service import compact_text

        return compact_text(doc, max_chars, llm, "research digest")
    return doc
