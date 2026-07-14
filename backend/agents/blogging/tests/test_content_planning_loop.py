"""Direct unit tests for shared.content_planning_loop.

Exercises the six extracted functions independently of BlogPlanningAgent /
BlogWriterAgent, using bare stub callables.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from shared.content_plan import (
    ContentPlan,
    ContentPlanSection,
    PlanningInput,
    RequirementsAnalysis,
    TitleCandidate,
)
from shared.content_planning_loop import (
    build_generate_plan_prompt,
    build_refine_plan_prompt,
    complete_plan_json,
    is_planning_done,
    post_validate_plan,
    run_content_planning_loop,
)
from shared.content_profile import ContentProfile, resolve_length_policy
from shared.errors import PlanningError


def _policy_standard():
    return resolve_length_policy(content_profile=ContentProfile.standard_article)


def _good_plan_dict() -> dict[str, Any]:
    return {
        "overarching_topic": "Topic",
        "narrative_flow": "x then y",
        "sections": [
            {"title": "A", "coverage_description": "doA", "order": 0},
            {"title": "B", "coverage_description": "doB", "order": 1},
            {"title": "C", "coverage_description": "doC", "order": 2},
            {"title": "D", "coverage_description": "doD", "order": 3},
        ],
        "title_candidates": [{"title": "T", "probability_of_success": 0.7}],
        "requirements_analysis": {
            "plan_acceptable": True,
            "scope_feasible": True,
            "research_gaps": [],
        },
    }


def _bad_plan_dict() -> dict[str, Any]:
    d = _good_plan_dict()
    d["requirements_analysis"]["plan_acceptable"] = False
    return d


def test_post_validate_plan_flags_out_of_bounds_section_count() -> None:
    sections = [
        ContentPlanSection(title=f"S{i}", coverage_description="x", order=i) for i in range(15)
    ]
    plan = ContentPlan(
        overarching_topic="T",
        narrative_flow="n",
        sections=sections,
        title_candidates=[TitleCandidate(title="T", probability_of_success=0.5)],
        requirements_analysis=RequirementsAnalysis(
            plan_acceptable=True, scope_feasible=True, research_gaps=[]
        ),
    )
    out = post_validate_plan(plan, _policy_standard())
    assert out.requirements_analysis.plan_acceptable is False
    assert any("outside expected range" in g for g in out.requirements_analysis.gaps)


def test_post_validate_plan_preserves_in_bounds_plan() -> None:
    plan = ContentPlan(
        overarching_topic="T",
        narrative_flow="n",
        sections=[
            ContentPlanSection(title=f"S{i}", coverage_description="x", order=i) for i in range(4)
        ],
        title_candidates=[TitleCandidate(title="T", probability_of_success=0.5)],
        requirements_analysis=RequirementsAnalysis(
            plan_acceptable=True, scope_feasible=True, research_gaps=[]
        ),
    )
    out = post_validate_plan(plan, _policy_standard())
    assert out.requirements_analysis.plan_acceptable is True


def test_is_planning_done() -> None:
    """True only when both plan_acceptable and scope_feasible are True."""
    plan = ContentPlan(
        overarching_topic="X",
        narrative_flow="f",
        sections=[ContentPlanSection(title="A", coverage_description="a", order=0)],
        title_candidates=[TitleCandidate(title="T", probability_of_success=0.5)],
        requirements_analysis=RequirementsAnalysis(
            plan_acceptable=True, scope_feasible=True, research_gaps=[]
        ),
    )
    assert is_planning_done(plan) is True

    plan2 = plan.model_copy(
        update={
            "requirements_analysis": RequirementsAnalysis(
                plan_acceptable=False, scope_feasible=True, research_gaps=[]
            )
        }
    )
    assert is_planning_done(plan2) is False


def test_build_generate_plan_prompt_with_optional_fields() -> None:
    inp = PlanningInput(
        brief="A brief",
        audience="audience-x",
        tone_or_purpose="tone-y",
        length_policy_context="ctx",
        research_digest="digest",
        series_context_block="series block content",
    )
    out = build_generate_plan_prompt(inp)
    assert "audience-x" in out
    assert "tone-y" in out
    assert "series block content" in out


def test_build_generate_plan_prompt_skips_blank_series_block() -> None:
    inp = PlanningInput(
        brief="A brief",
        length_policy_context="ctx",
        research_digest="digest",
        series_context_block="   ",
    )
    out = build_generate_plan_prompt(inp)
    assert "series" not in out.lower()


def test_build_refine_plan_prompt_includes_previous_plan_and_feedback() -> None:
    inp = PlanningInput(brief="b", length_policy_context="ctx", research_digest="digest")
    prev = ContentPlan(
        overarching_topic="t",
        narrative_flow="n",
        sections=[ContentPlanSection(title="x", coverage_description="x", order=0)],
        title_candidates=[TitleCandidate(title="T", probability_of_success=0.5)],
        requirements_analysis=RequirementsAnalysis(
            plan_acceptable=False, scope_feasible=True, research_gaps=[]
        ),
    )
    out = build_refine_plan_prompt(inp, prev, "fix gaps")
    assert "fix gaps" in out
    assert "PREVIOUS PLAN" in out


def test_complete_plan_json_first_attempt_success() -> None:
    data, retries = complete_plan_json(
        "p",
        system="s",
        on_llm_request=None,
        max_parse_retries=2,
        call_json_fn=lambda p, s: {"ok": True},
        call_raw_fn=lambda p, s: "unused",
    )
    assert data == {"ok": True}
    assert retries == 0


def test_complete_plan_json_falls_back_to_raw_call() -> None:
    data, retries = complete_plan_json(
        "p",
        system="s",
        on_llm_request=None,
        max_parse_retries=2,
        call_json_fn=lambda p, s: {},
        call_raw_fn=lambda p, s: json.dumps({"a": 1}),
    )
    assert data == {"a": 1}


def test_complete_plan_json_raises_after_max_parse_retries() -> None:
    with pytest.raises(PlanningError) as exc:
        complete_plan_json(
            "p",
            system="s",
            on_llm_request=None,
            max_parse_retries=2,
            call_json_fn=lambda p, s: {},
            call_raw_fn=lambda p, s: "not json at all",
        )
    assert "parse failed" in str(exc.value).lower()


def _loop_kwargs(**overrides: Any) -> dict[str, Any]:
    base = dict(
        planning_input=PlanningInput(brief="b", length_policy_context="c", research_digest="d"),
        length_policy=_policy_standard(),
        on_llm_request=None,
        max_iterations=3,
        max_parse_retries=2,
        plan_critic=None,
        brand_spec_prompt="",
        writing_guidelines="",
        work_dir=None,
        generate_system="GEN",
        refine_system="REF",
    )
    base.update(overrides)
    return base


def test_run_content_planning_loop_converges_first_iteration() -> None:
    good = _good_plan_dict()

    def complete_fn(prompt, *, system, on_llm_request, max_parse_retries):
        return good, 0

    result = run_content_planning_loop(**_loop_kwargs(complete_plan_json_fn=complete_fn))
    assert result.planning_iterations_used == 1
    assert result.content_plan.overarching_topic == "Topic"


def test_run_content_planning_loop_refines_then_converges() -> None:
    plans = iter([_bad_plan_dict(), _good_plan_dict()])

    def complete_fn(prompt, *, system, on_llm_request, max_parse_retries):
        return next(plans), 0

    result = run_content_planning_loop(**_loop_kwargs(complete_plan_json_fn=complete_fn))
    assert result.planning_iterations_used == 2


def test_run_content_planning_loop_raises_after_max_iterations() -> None:
    bad = _bad_plan_dict()

    def complete_fn(prompt, *, system, on_llm_request, max_parse_retries):
        return bad, 0

    with pytest.raises(PlanningError) as exc:
        run_content_planning_loop(
            **_loop_kwargs(max_iterations=2, complete_plan_json_fn=complete_fn)
        )
    assert "did not converge" in str(exc.value)


def test_run_content_planning_loop_invalid_schema_raises() -> None:
    def complete_fn(prompt, *, system, on_llm_request, max_parse_retries):
        return {"garbage": 1}, 0

    with pytest.raises(PlanningError):
        run_content_planning_loop(**_loop_kwargs(complete_plan_json_fn=complete_fn))


def test_run_content_planning_loop_fills_missing_title_candidates() -> None:
    plan = _good_plan_dict()
    plan["title_candidates"] = []
    plan["overarching_topic"] = "An example topic"

    def complete_fn(prompt, *, system, on_llm_request, max_parse_retries):
        return plan, 0

    result = run_content_planning_loop(**_loop_kwargs(complete_plan_json_fn=complete_fn))
    assert result.content_plan.title_candidates
    assert "An example topic" in result.content_plan.title_candidates[0].title


class _CriticReport:
    def __init__(self, approved: bool) -> None:
        self.approved = approved
        self.violations: list[Any] = []
        self.notes = ""

    def to_dict(self) -> dict[str, str]:
        return {"status": "approved" if self.approved else "rejected"}


def test_run_content_planning_loop_with_plan_critic() -> None:
    """Critic rejects iteration 1 and approves iteration 2; loop converges on the second pass."""

    class _Critic:
        def __init__(self):
            self.called = 0

        def run(self, **_kw):
            self.called += 1
            return _CriticReport(approved=self.called > 1)

    good = _good_plan_dict()

    def complete_fn(prompt, *, system, on_llm_request, max_parse_retries):
        return good, 0

    critic = _Critic()
    result = run_content_planning_loop(
        **_loop_kwargs(plan_critic=critic, complete_plan_json_fn=complete_fn)
    )
    assert result.planning_iterations_used == 2
    assert critic.called == 2
    assert result.plan_critic_report == {"status": "approved"}
