"""Shared ContentPlan / PlanningPhaseResult builders for blogging tests.

Centralizes the ContentPlan construction boilerplate previously copy-pasted
(with per-file variations in topic, section count, and title-candidate count)
across several test modules. Every field a caller's assertions depend on must
be passed explicitly; only ``requirements_analysis`` and a minimal
``title_candidates`` default are shared, since every call site uses the
identical accepted/feasible/no-gaps triple and overrides ``title_candidates``
explicitly whenever it needs a different shape.
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
    """Build a passing RequirementsAnalysis (accepted/feasible/no-gaps), overridable per field.

    Preconditions:
        - ``overrides`` keys are valid ``RequirementsAnalysis`` field names.
    Postconditions:
        - Returns a ``RequirementsAnalysis`` with ``plan_acceptable=True``,
          ``scope_feasible=True``, ``research_gaps=[]`` unless a caller override replaces one.
    """
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
    """Build a ContentPlan from the fields a test cares about, filling in safe defaults.

    Preconditions:
        - ``sections`` has at least one ``ContentPlanSection`` (required by ``ContentPlan``).
        - ``extra`` keys are valid ``ContentPlan`` field names (e.g. ``plan_version``,
          ``target_reader``).
    Postconditions:
        - Returns a valid ``ContentPlan``; ``title_candidates`` defaults to a single generic
          candidate and ``requirements_analysis`` defaults to :func:`make_requirements_analysis`
          when the caller omits them.
    """
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
    """Wrap a ContentPlan in a PlanningPhaseResult with minimal passing observability fields.

    Preconditions:
        - ``result_overrides`` keys are valid ``PlanningPhaseResult`` field names other than
          ``content_plan``.
    Postconditions:
        - Returns a ``PlanningPhaseResult`` with ``planning_iterations_used=1``,
          ``parse_retry_count=0``, ``planning_wall_ms_total=10.0`` unless overridden.
    """
    defaults: dict[str, Any] = dict(
        planning_iterations_used=1, parse_retry_count=0, planning_wall_ms_total=10.0
    )
    defaults.update(result_overrides)
    return PlanningPhaseResult(content_plan=plan, **defaults)


def make_minimal_planning_phase_result(**overrides: Any) -> PlanningPhaseResult:
    """Build the canonical minimal PlanningPhaseResult (Topic/Flow/Intro plan, one title).

    Centralizes the "Topic"/"Flow"/single-"Intro"-section/"My Title" shape previously
    copy-pasted as a local ``_make_plan()`` helper in multiple pipeline-gate test modules.

    Preconditions:
        - ``overrides`` keys are valid ``PlanningPhaseResult`` field names other than
          ``content_plan`` (e.g. ``planning_wall_ms_total``).
    Postconditions:
        - Returns a ``PlanningPhaseResult`` wrapping a one-section, one-title-candidate
          ContentPlan, with :func:`make_planning_phase_result`'s defaults unless overridden.
    """
    plan = make_content_plan(
        overarching_topic="Topic",
        narrative_flow="Flow",
        sections=[ContentPlanSection(title="Intro", coverage_description="hook", order=0)],
        title_candidates=[TitleCandidate(title="My Title", probability_of_success=0.7)],
    )
    return make_planning_phase_result(plan, **overrides)


def make_pipeline_doubles(
    *,
    title: str = "My Title",
    probability: float = 0.7,
    planning_wall_ms_total: float = 5.0,
    include_status: bool = True,
) -> tuple[PlanningPhaseResult, Any] | tuple[PlanningPhaseResult, Any, str]:
    """Build (planning_phase_result, draft_double[, status]) doubles for pipeline-job tests.

    Centralizes the "Topic"/"Flow"/single-"Intro"-section plan plus a minimal draft stub
    previously copy-pasted as local ``_make_pipeline_doubles``/``_pipeline_doubles`` helpers
    across the run_pipeline_job test modules.

    Preconditions:
        - ``probability`` is a valid success probability for a ``TitleCandidate``.
    Postconditions:
        - Returns a 3-tuple ``(ppr, draft, "PASS")`` when ``include_status`` is true (default);
          returns a 2-tuple ``(ppr, draft)`` when ``include_status`` is false. ``draft`` always
          exposes a ``.draft`` attribute with placeholder markdown body text.
    """
    plan = make_content_plan(
        overarching_topic="Topic",
        narrative_flow="Flow",
        sections=[ContentPlanSection(title="Intro", coverage_description="hook", order=0)],
        title_candidates=[TitleCandidate(title=title, probability_of_success=probability)],
    )
    ppr = make_planning_phase_result(plan, planning_wall_ms_total=planning_wall_ms_total)

    class _Draft:
        draft = "# Draft\n\nBody."

    if include_status:
        return ppr, _Draft(), "PASS"
    return ppr, _Draft()
