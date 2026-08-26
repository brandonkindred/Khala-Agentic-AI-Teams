"""Tests for the independent plan critic.

These tests drive the critic directly via a fake Strands Agent so we can
exercise the full parse → coerce → report path without hitting an LLM, plus
an integration test that runs the critic inside BlogPlanningAgent.run.

Uses the shared ContentPlan factory from ``_content_plan_test_utils``.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import pytest
from agents.blogging.blog_plan_critic_agent import BlogPlanCriticAgent, PlanCriticReport
from agents.blogging.blog_plan_critic_agent.agent import build_refine_feedback_from_critic
from agents.blogging.blog_planning_agent import BlogPlanningAgent
from agents.blogging.shared.content_plan import (
    ContentPlan,
    ContentPlanSection,
    PlanningInput,
    TitleCandidate,
)
from agents.blogging.shared.content_profile import (
    ContentProfile,
    LengthPolicy,
    resolve_length_policy,
)

from llm_service import DummyLLMClient, LLMRateLimitError, LLMTemporaryError

from ._content_plan_test_utils import make_content_plan


def _policy_standard() -> LengthPolicy:
    return resolve_length_policy(content_profile=ContentProfile.standard_article)


def _minimal_plan(topic: str = "A stance about X that readers should adopt") -> ContentPlan:
    return make_content_plan(
        overarching_topic=topic,
        narrative_flow="Reader journey from skepticism to conviction.",
        sections=[
            ContentPlanSection(
                title=f"S{i}",
                coverage_description="Specific coverage.",
                key_points=[f"Point {i}A", f"Point {i}B", f"Point {i}C"],
                what_to_avoid=["Generic advice"],
                reader_takeaway="After this section, the reader believes X.",
                strongest_point="The specific hill to die on.",
                opening_hook="A concrete question.",
                transition_to_next="Tension that leads forward.",
                order=i,
            )
            for i in range(4)
        ],
        title_candidates=[
            TitleCandidate(title=f"Title candidate {i}", probability_of_success=0.7)
            for i in range(5)
        ],
    )


class _FakeAgent:
    """Drop-in replacement for strands.Agent that returns a canned response.

    Records every call into ``self.calls`` and pops replies off
    ``self.responses``. Both are instance attributes shared across every
    ``_FakeAgent`` built by the same ``_FakeAgentFactory``, rather than
    class-level mutable state -- so tests never need a fixture-based reset.

    Preconditions:
        calls and responses are lists owned by the caller (typically a single
        ``_FakeAgentFactory`` instance); this object mutates them in place and
        does not copy them.
    Postconditions:
        Each ``__call__`` appends exactly one ``(system_prompt, user_prompt)``
        tuple to ``calls`` and, if ``responses`` is non-empty, removes and
        returns its first element; otherwise returns a canned PASS payload.
    """

    def __init__(
        self,
        model: Any,
        system_prompt: str = "",
        *,
        calls: list[tuple[str, str]],
        responses: list[str],
    ) -> None:
        self._system = system_prompt
        self.calls = calls
        self.responses = responses

    def __call__(self, user_prompt: str) -> str:
        self.calls.append((self._system, user_prompt))
        if self.responses:
            return self.responses.pop(0)
        return json.dumps(
            {
                "status": "PASS",
                "approved": True,
                "violations": [],
                "notes": None,
                "rubric_version": "v1",
            }
        )


class _FakeAgentFactory:
    """Callable matching ``Agent(model, system_prompt=...)`` for use with ``patch()``.

    Every ``_FakeAgent`` this factory constructs shares this instance's
    ``calls``/``responses`` lists, so a test can hand one factory instance to
    ``patch()`` and later inspect ``factory.calls`` -- private, instance-owned
    state instead of class-level attributes reset by a fixture.

    Preconditions:
        responses, if provided, is a list of JSON (or deliberately malformed)
        strings to return in call order.
    Postconditions:
        self.calls starts empty and gains one entry per ``_FakeAgent.__call__``
        across every instance this factory constructs.
        self.responses starts as a copy of the input list (empty if omitted)
        and is consumed in order as instances are called.
    """

    def __init__(self, responses: list[str] | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self.responses: list[str] = list(responses) if responses is not None else []

    def __call__(self, model: Any, system_prompt: str = "") -> _FakeAgent:
        return _FakeAgent(
            model, system_prompt=system_prompt, calls=self.calls, responses=self.responses
        )


# ---------------------------------------------------------------------------
# Agent unit tests
# ---------------------------------------------------------------------------


def test_critic_returns_pass_on_clean_json() -> None:
    fake_agent = _FakeAgentFactory(
        responses=[
            json.dumps(
                {
                    "status": "PASS",
                    "approved": True,
                    "violations": [],
                    "notes": "Plan looks good.",
                    "rubric_version": "v1",
                }
            )
        ]
    )
    critic = BlogPlanCriticAgent(llm_client=DummyLLMClient())
    with patch("agents.blogging.shared.json_retry.Agent", fake_agent):
        report = critic.run(
            plan=_minimal_plan(),
            brand_spec_prompt="Brand spec text.",
            writing_guidelines="Writing guidelines text.",
            research_digest="Research.",
        )
    assert report.status == "PASS"
    assert report.approved is True
    assert report.violations == []
    assert len(fake_agent.calls) == 1


def test_critic_surfaces_violations_and_fails() -> None:
    fake_agent = _FakeAgentFactory(
        responses=[
            json.dumps(
                {
                    "status": "FAIL",
                    "approved": False,
                    "violations": [
                        {
                            "rule_id": "overarching_topic.stance_not_label",
                            "severity": "must_fix",
                            "section": "overall",
                            "evidence_quote": "A guide to caching",
                            "description": "Topic is a label, not a stance.",
                            "suggested_fix": "Rewrite as a stance.",
                        },
                        {
                            "rule_id": "section.key_points.specificity",
                            "severity": "must_fix",
                            "section": "Introduction",
                            "evidence_quote": "Discuss scaling",
                            "description": "Vague key point.",
                            "suggested_fix": "Replace with a specific claim.",
                        },
                    ],
                    "rubric_version": "v1",
                }
            )
        ]
    )
    critic = BlogPlanCriticAgent(llm_client=DummyLLMClient())
    with patch("agents.blogging.shared.json_retry.Agent", fake_agent):
        report = critic.run(
            plan=_minimal_plan(),
            brand_spec_prompt="Brand spec text.",
            writing_guidelines="Writing guidelines text.",
        )
    assert report.status == "FAIL"
    assert report.approved is False
    assert report.must_fix_count() == 2
    assert {v.rule_id for v in report.violations} == {
        "overarching_topic.stance_not_label",
        "section.key_points.specificity",
    }


def test_critic_approved_invariant_enforced() -> None:
    """approved must equal (status == PASS) regardless of what the LLM returned."""
    fake_agent = _FakeAgentFactory(
        responses=[
            json.dumps(
                {
                    "status": "FAIL",
                    "approved": True,  # inconsistent with status; critic should fix
                    "violations": [
                        {
                            "rule_id": "x",
                            "severity": "must_fix",
                            "description": "y",
                            "suggested_fix": "z",
                        }
                    ],
                    "rubric_version": "v1",
                }
            )
        ]
    )
    critic = BlogPlanCriticAgent(llm_client=DummyLLMClient())
    with patch("agents.blogging.shared.json_retry.Agent", fake_agent):
        report = critic.run(
            plan=_minimal_plan(),
            brand_spec_prompt="b",
            writing_guidelines="g",
        )
    assert report.status == "FAIL"
    assert report.approved is False


def test_critic_parse_failure_falls_back_to_fail() -> None:
    fake_agent = _FakeAgentFactory(responses=["not json at all", "also not json"])
    critic = BlogPlanCriticAgent(llm_client=DummyLLMClient())
    with patch("agents.blogging.shared.json_retry.Agent", fake_agent):
        report = critic.run(
            plan=_minimal_plan(),
            brand_spec_prompt="b",
            writing_guidelines="g",
        )
    assert report.status == "FAIL"
    assert report.approved is False
    assert report.notes is not None
    assert "parseable JSON" in (report.notes or "")


@pytest.mark.parametrize("err_cls", [LLMRateLimitError, LLMTemporaryError])
def test_critic_transient_error_reraises(err_cls) -> None:
    """Transient LLM errors propagate so the job runner / Temporal owns retry."""

    class _BoomAgent:
        def __init__(self, *a, **kw):
            pass

        def __call__(self, prompt):
            raise err_cls("transient outage")

    critic = BlogPlanCriticAgent(llm_client=DummyLLMClient())
    with patch("agents.blogging.shared.json_retry.Agent", _BoomAgent):
        with pytest.raises(err_cls):
            critic.run(
                plan=_minimal_plan(),
                brand_spec_prompt="b",
                writing_guidelines="g",
            )


def test_critic_agent_construction_failure_falls_back() -> None:
    """Agent(...) construction errors fail closed via the FAIL fallback report."""

    class _BoomCtor:
        def __init__(self, *a, **kw):
            raise RuntimeError("rejected model config")

    critic = BlogPlanCriticAgent(llm_client=DummyLLMClient())
    with patch("agents.blogging.shared.json_retry.Agent", _BoomCtor):
        report = critic.run(
            plan=_minimal_plan(),
            brand_spec_prompt="b",
            writing_guidelines="g",
        )
    assert report.status == "FAIL"
    assert report.approved is False
    assert report.notes is not None
    assert "rejected model config" in (report.notes or "")


@pytest.mark.parametrize("err_cls", [LLMRateLimitError, LLMTemporaryError])
def test_critic_event_loop_exception_unwraps_transient(err_cls) -> None:
    """strands EventLoopException must re-raise the unwrapped transient cause."""
    from strands.types.exceptions import EventLoopException

    cause = err_cls("transient outage")

    class _BoomAgent:
        def __init__(self, *a, **kw):
            pass

        def __call__(self, prompt):
            raise EventLoopException(cause)

    critic = BlogPlanCriticAgent(llm_client=DummyLLMClient())
    with patch("agents.blogging.shared.json_retry.Agent", _BoomAgent):
        with pytest.raises(err_cls) as exc_info:
            critic.run(
                plan=_minimal_plan(),
                brand_spec_prompt="b",
                writing_guidelines="g",
            )
    assert exc_info.value is cause


def test_critic_coerces_non_string_status_to_fail() -> None:
    """A non-string `status` (e.g. bool) must not crash _coerce_report."""
    fake_agent = _FakeAgentFactory(
        responses=[
            json.dumps(
                {
                    "status": True,
                    "approved": True,
                    "violations": [],
                    "notes": None,
                    "rubric_version": "v1",
                }
            )
        ]
    )
    critic = BlogPlanCriticAgent(llm_client=DummyLLMClient())
    with patch("agents.blogging.shared.json_retry.Agent", fake_agent):
        report = critic.run(
            plan=_minimal_plan(),
            brand_spec_prompt="b",
            writing_guidelines="g",
        )
    assert report.status == "FAIL"
    assert report.approved is False


def test_critic_coerces_non_string_violation_fields() -> None:
    """Non-string violation fields (description/suggested_fix) must not crash."""
    fake_agent = _FakeAgentFactory(
        responses=[
            json.dumps(
                {
                    "status": "FAIL",
                    "approved": False,
                    "violations": [
                        {
                            "rule_id": "x",
                            "severity": "must_fix",
                            "description": 1,
                            "suggested_fix": True,
                        }
                    ],
                    "rubric_version": "v1",
                }
            )
        ]
    )
    critic = BlogPlanCriticAgent(llm_client=DummyLLMClient())
    with patch("agents.blogging.shared.json_retry.Agent", fake_agent):
        report = critic.run(
            plan=_minimal_plan(),
            brand_spec_prompt="b",
            writing_guidelines="g",
        )
    assert report.status == "FAIL"
    assert len(report.violations) == 1
    assert report.violations[0].description == "1"
    assert report.violations[0].suggested_fix == "True"


def test_critic_coerce_report_unexpected_exception_falls_back() -> None:
    """Any unanticipated exception during coercion still yields a FAIL report."""
    fake_agent = _FakeAgentFactory(
        responses=[
            json.dumps(
                {
                    "status": "PASS",
                    "approved": True,
                    "violations": [{"rule_id": "x"}],
                    "rubric_version": "v1",
                }
            )
        ]
    )
    critic = BlogPlanCriticAgent(llm_client=DummyLLMClient())
    with patch("agents.blogging.shared.json_retry.Agent", fake_agent):
        with patch(
            "agents.blogging.blog_plan_critic_agent.agent.PlanViolation",
            side_effect=RuntimeError("boom"),
        ):
            report = critic.run(
                plan=_minimal_plan(),
                brand_spec_prompt="b",
                writing_guidelines="g",
            )
    assert report.status == "FAIL"
    assert report.approved is False
    assert report.notes is not None
    assert "boom" in report.notes


def test_critic_persists_report_to_work_dir(tmp_path) -> None:
    fake_agent = _FakeAgentFactory(
        responses=[
            json.dumps(
                {
                    "status": "PASS",
                    "approved": True,
                    "violations": [],
                    "rubric_version": "v1",
                }
            )
        ]
    )
    critic = BlogPlanCriticAgent(llm_client=DummyLLMClient())
    with patch("agents.blogging.shared.json_retry.Agent", fake_agent):
        critic.run(
            plan=_minimal_plan(),
            brand_spec_prompt="b",
            writing_guidelines="g",
            work_dir=tmp_path,
            artifact_name="plan_critic_report_v1.json",
        )
    assert (tmp_path / "plan_critic_report_v1.json").exists()


# ---------------------------------------------------------------------------
# Refine-feedback formatting
# ---------------------------------------------------------------------------


def test_refine_feedback_lists_must_fix_first() -> None:
    report = PlanCriticReport(
        status="FAIL",
        approved=False,
        violations=[
            {
                "rule_id": "z.consider",
                "severity": "consider",
                "description": "consider item",
                "suggested_fix": "consider fix",
            },  # type: ignore[list-item]
            {
                "rule_id": "a.must_fix",
                "severity": "must_fix",
                "description": "must fix item",
                "suggested_fix": "must fix fix",
            },  # type: ignore[list-item]
            {
                "rule_id": "m.should_fix",
                "severity": "should_fix",
                "description": "should fix item",
                "suggested_fix": "should fix fix",
            },  # type: ignore[list-item]
        ],
    )
    feedback = build_refine_feedback_from_critic(report)
    must_idx = feedback.index("a.must_fix")
    should_idx = feedback.index("m.should_fix")
    consider_idx = feedback.index("z.consider")
    assert must_idx < should_idx < consider_idx
    assert "independent plan critic reviewed" in feedback


def test_refine_feedback_empty_when_approved() -> None:
    report = PlanCriticReport(status="PASS", approved=True, violations=[])
    assert "no refinement needed" in build_refine_feedback_from_critic(report)


# ---------------------------------------------------------------------------
# Integration: BlogPlanningAgent with the critic
# ---------------------------------------------------------------------------


def test_planning_agent_integrates_critic_and_persists_reports(tmp_path) -> None:
    """When a critic is attached, the planner gates on critic approval and writes artifacts."""
    llm = DummyLLMClient()
    critic = BlogPlanCriticAgent(llm_client=llm)
    agent = BlogPlanningAgent(
        llm,
        plan_critic=critic,
        brand_spec_prompt="Brand: tests.",
        writing_guidelines="Guidelines: keep it short.",
    )
    inp = PlanningInput(
        brief="Test brief about observability.",
        research_digest="## Sources\n- Source one: summary.",
        length_policy_context=_policy_standard().length_guidance,
    )
    result = agent.run(inp, length_policy=_policy_standard(), work_dir=tmp_path)
    assert result.content_plan.requirements_analysis.plan_acceptable is True
    # Critic report is attached to the result and persisted.
    assert result.plan_critic_report is not None
    assert result.plan_critic_report["status"] == "PASS"
    assert result.plan_critic_report["approved"] is True
    assert (tmp_path / "plan_critic_report_v1.json").exists()


def test_planning_agent_without_critic_keeps_legacy_behaviour() -> None:
    """When no critic is attached, the planner's self-eval is authoritative."""
    llm = DummyLLMClient()
    agent = BlogPlanningAgent(llm)
    inp = PlanningInput(
        brief="Test brief about observability.",
        research_digest="## Sources\n- Source one: summary.",
        length_policy_context=_policy_standard().length_guidance,
    )
    result = agent.run(inp, length_policy=_policy_standard())
    assert result.plan_critic_report is None
