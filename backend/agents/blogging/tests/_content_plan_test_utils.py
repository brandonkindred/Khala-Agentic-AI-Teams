"""Shared ContentPlan / PlanningPhaseResult builders for blogging tests.

Centralizes the ContentPlan construction boilerplate previously copy-pasted
(with per-file variations in topic, section count, and title-candidate count)
across several test modules. Every field a caller's assertions depend on must
be passed explicitly; only ``requirements_analysis`` defaults are shared,
since every call site uses the identical accepted/feasible/no-gaps triple.
"""

from __future__ import annotations

from typing import Any

from shared.content_plan import (
    ContentPlan,
    ContentPlanSection,
    PlanningPhaseResult,
    RequirementsAnalysis,
    TitleCandidate,
)


def make_requirements_analysis(**overrides: Any) -> RequirementsAnalysis:
    defaults: dict[str, Any] = dict(plan_acceptable=True, scope_feasible=True, research_gaps=[])
    defaults.update(overrides)
    return RequirementsAnalysis(**defaults)


def make_content_plan(
    *,
    overarching_topic: str,
    narrative_flow: str,
    sections: list[ContentPlanSection],
    title_candidates: list[TitleCandidate] | None = None,
    requirements_analysis: RequirementsAnalysis | None = None,
    **extra: Any,
) -> ContentPlan:
    return ContentPlan(
        overarching_topic=overarching_topic,
        narrative_flow=narrative_flow,
        sections=sections,
        title_candidates=title_candidates
        if title_candidates is not None
        else [TitleCandidate(title="T", probability_of_success=0.5)],
        requirements_analysis=requirements_analysis or make_requirements_analysis(),
        **extra,
    )


def make_planning_phase_result(plan: ContentPlan, **result_overrides: Any) -> PlanningPhaseResult:
    defaults: dict[str, Any] = dict(
        planning_iterations_used=1, parse_retry_count=0, planning_wall_ms_total=10.0
    )
    defaults.update(result_overrides)
    return PlanningPhaseResult(content_plan=plan, **defaults)
