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


class TitleScoring(BaseModel):
    """Breakdown of how a title candidate was scored across multiple dimensions."""

    curiosity_gap: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Does this title create a question in the reader's mind that they need answered? (0=no curiosity, 1=irresistible)",
    )
    specificity: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Does this title promise something concrete vs vague? Numbers, names, and outcomes score high. (0=generic, 1=very specific)",
    )
    audience_fit: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="How well does this title speak to the target reader's level and interests? (0=wrong audience, 1=perfect fit)",
    )
    seo_potential: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Does this title contain searchable terms the target audience would Google? (0=not searchable, 1=highly searchable)",
    )
    emotional_pull: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Does this title trigger an emotion (fear of missing out, excitement, frustration, relief)? (0=flat, 1=strong emotion)",
    )
    rationale: str = Field(
        default="",
        description="One sentence explaining WHY this title scored the way it did — what makes it strong or weak.",
    )


class TitleCandidate(BaseModel):
    """A candidate title with multi-dimensional scoring."""

    title: str = Field(..., description="Catchy title for the post.")
    probability_of_success: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Overall score (0–1) — weighted average of the scoring dimensions.",
    )
    scoring: Optional[TitleScoring] = Field(
        None,
        description="Breakdown of how this title was scored across curiosity, specificity, audience fit, SEO, and emotion.",
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
        description="What argument or belief shift this section creates, and how it builds on the prior section.",
    )
    key_points: List[str] = Field(
        default_factory=list,
        description="3-5 specific points, claims, or examples this section MUST cover. Concrete, not vague.",
    )
    what_to_avoid: List[str] = Field(
        default_factory=list,
        description="1-3 things this section should NOT do (e.g. 'don't just list features', 'avoid generic advice').",
    )
    reader_takeaway: Optional[str] = Field(
        default="",
        description="One sentence: what the reader should understand or believe after reading this section.",
    )
    strongest_point: Optional[str] = Field(
        default="",
        description="The single most important thing to get across in this section — the hill to die on.",
    )
    story_opportunity: Optional[str] = Field(
        None,
        description="If this section would benefit from a personal story or anecdote, describe what kind of story fits here "
        "(e.g. 'a time you over-engineered something', 'a debugging war story'). "
        "Reference specific stories from the research or author material if available. "
        "Null if this section is better served by data, explanation, or hypotheticals.",
    )
    opening_hook: Optional[str] = Field(
        default="",
        description="How this section should open — a question, a surprising fact, a callback to the prior section.",
    )
    transition_to_next: Optional[str] = Field(
        default="",
        description="How this section hands off to the next one — what tension or question leads the reader forward. Null for the last section.",
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
    opening_strategy: Optional[str] = Field(
        default="",
        description="How the post should open — a personal story, a provocative question, a surprising stat, a pain point.",
    )
    conclusion_guidance: Optional[str] = Field(
        default="",
        description="How the post should wrap up — what final insight to land, whether to include a call to action, and what the reader should do next.",
    )
    target_reader: Optional[str] = Field(
        default="",
        description="One sentence describing who this post is for and what they already know.",
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
        default="",
        description="Bounded digest of research (claims, sources); capped by caller. Empty when research is skipped.",
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
    plan_critic_report: Optional[Any] = Field(
        default=None,
        description=(
            "PlanCriticReport from the independent critic pass when enabled. "
            "Typed as Any here to avoid a hard dependency on blog_plan_critic_agent in this shared module."
        ),
    )


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
    """Full outline string for API `outline` field and draft agent prompts."""
    lines: List[str] = [
        f"# {plan.overarching_topic}",
        "",
        "## Narrative flow",
        plan.narrative_flow,
        "",
    ]
    if plan.target_reader:
        lines.extend(["## Target reader", plan.target_reader, ""])
    if plan.opening_strategy:
        lines.extend(["## Opening strategy", plan.opening_strategy, ""])
    if plan.conclusion_guidance:
        lines.extend(["## Conclusion guidance", plan.conclusion_guidance, ""])
    lines.append("## Sections")
    for sec in sorted(plan.sections, key=lambda s: s.order):
        lines.append(f"### {sec.title}")
        lines.append(f"**Coverage:** {sec.coverage_description}")
        if sec.key_points:
            lines.append("**Key points to hit:**")
            for kp in sec.key_points:
                lines.append(f"- {kp}")
        if sec.what_to_avoid:
            lines.append("**What to avoid:**")
            for wa in sec.what_to_avoid:
                lines.append(f"- {wa}")
        if sec.reader_takeaway:
            lines.append(f"**Reader takeaway:** {sec.reader_takeaway}")
        if sec.strongest_point:
            lines.append(f"**Strongest point (must land):** {sec.strongest_point}")
        if sec.story_opportunity:
            lines.append(f"**Story opportunity:** {sec.story_opportunity}")
        if sec.opening_hook:
            lines.append(f"**Opening hook:** {sec.opening_hook}")
        if sec.transition_to_next:
            lines.append(f"**Transition to next:** {sec.transition_to_next}")
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
        brief += f"\n### {i}. {tc.title} — **{tc.probability_of_success:.0%}**\n"
        if tc.scoring:
            s = tc.scoring
            brief += (
                f"| Curiosity | Specificity | Audience Fit | SEO | Emotional Pull |\n"
                f"|:---------:|:-----------:|:------------:|:---:|:--------------:|\n"
                f"| {s.curiosity_gap:.0%} | {s.specificity:.0%} | {s.audience_fit:.0%} "
                f"| {s.seo_potential:.0%} | {s.emotional_pull:.0%} |\n"
            )
            if s.rationale:
                brief += f"\n_{s.rationale}_\n"
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
