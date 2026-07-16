"""Verification for the planning-logic dedup: BlogPlanningAgent.run() and
BlogWriterAgent.plan_content() now both delegate to
shared.content_planning_loop.run_content_planning_loop and must produce
equivalent ContentPlan output for identical inputs."""

from __future__ import annotations

from agents.blogging.blog_planning_agent.agent import BlogPlanningAgent
from agents.blogging.blog_writer_agent.agent import BlogWriterAgent
from agents.blogging.shared.content_plan import PlanningInput
from agents.blogging.shared.content_profile import ContentProfile, resolve_length_policy

from llm_service import DummyLLMClient


def test_planning_agent_and_writer_agent_produce_equivalent_plan() -> None:
    """Same PlanningInput/LengthPolicy through both entry points yields the same
    ContentPlan, aside from plan_version and PlanningPhaseResult's timing/iteration
    fields, which are excluded from the comparison since they're not expected to match."""
    policy = resolve_length_policy(content_profile=ContentProfile.standard_article)
    inp = PlanningInput(
        brief="Test brief about observability.",
        research_digest="## Sources\n- Source one: summary.",
        length_policy_context=policy.length_guidance,
    )

    planning_result = BlogPlanningAgent(DummyLLMClient()).run(inp, length_policy=policy)
    writer_result = BlogWriterAgent(
        llm_client=DummyLLMClient(),
        writing_style_guide_content="x",
        brand_spec_content="y",
    ).plan_content(inp, length_policy=policy)

    a = planning_result.content_plan.model_dump(mode="json")
    b = writer_result.content_plan.model_dump(mode="json")

    # plan_version is allowed to differ trivially (both converge on iteration
    # 1 here, so it doesn't in practice, but excluding it documents the
    # explicit non-goal). Iteration/timing fields on PlanningPhaseResult are
    # excluded outright since they are never expected to match.
    a.pop("plan_version", None)
    b.pop("plan_version", None)
    assert a == b
    assert planning_result.planning_iterations_used == writer_result.planning_iterations_used == 1
