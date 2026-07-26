"""Tests for BlogPlanCriticAgent.run() prompt-length capping."""

from __future__ import annotations

from typing import Any


def _agent():
    from agents.blogging.blog_plan_critic_agent.agent import BlogPlanCriticAgent

    return BlogPlanCriticAgent(llm_client=object())


def _plan():
    from agents.blogging.shared.content_plan import ContentPlanSection, TitleCandidate
    from agents.blogging.tests._content_plan_test_utils import make_content_plan

    return make_content_plan(
        overarching_topic="Topic",
        narrative_flow="Flow",
        sections=[ContentPlanSection(title="Intro", coverage_description="hook", order=0)],
        title_candidates=[TitleCandidate(title="T", probability_of_success=0.5)],
    )


def _run_capturing_prompt(monkeypatch, **run_kwargs) -> str:
    captured: dict[str, Any] = {}

    def fake_retry(factory, prompt, **kwargs):
        captured["prompt"] = prompt
        return {"status": "PASS", "approved": True, "violations": [], "notes": None}

    monkeypatch.setattr(
        "agents.blogging.blog_plan_critic_agent.agent.call_json_with_retry",
        fake_retry,
    )
    _agent().run(plan=_plan(), **run_kwargs)
    return captured["prompt"]


def test_run_truncates_brand_spec_over_cap(monkeypatch) -> None:
    from agents.blogging.blog_plan_critic_agent.agent import _BRAND_SPEC_CHAR_CAP

    oversized = "a" * (_BRAND_SPEC_CHAR_CAP + 500)
    prompt = _run_capturing_prompt(
        monkeypatch, brand_spec_prompt=oversized, writing_guidelines="wg", research_digest="rd"
    )
    assert "a" * (_BRAND_SPEC_CHAR_CAP + 1) not in prompt
    assert "a" * _BRAND_SPEC_CHAR_CAP in prompt


def test_run_truncates_writing_guidelines_over_cap(monkeypatch) -> None:
    from agents.blogging.blog_plan_critic_agent.agent import _WRITING_GUIDELINES_CHAR_CAP

    oversized = "b" * (_WRITING_GUIDELINES_CHAR_CAP + 500)
    prompt = _run_capturing_prompt(
        monkeypatch, brand_spec_prompt="bs", writing_guidelines=oversized, research_digest="rd"
    )
    assert "b" * (_WRITING_GUIDELINES_CHAR_CAP + 1) not in prompt
    assert "b" * _WRITING_GUIDELINES_CHAR_CAP in prompt


def test_run_truncates_research_digest_over_cap(monkeypatch) -> None:
    from agents.blogging.blog_plan_critic_agent.agent import _RESEARCH_DIGEST_CHAR_CAP

    oversized = "c" * (_RESEARCH_DIGEST_CHAR_CAP + 500)
    prompt = _run_capturing_prompt(
        monkeypatch, brand_spec_prompt="bs", writing_guidelines="wg", research_digest=oversized
    )
    assert "c" * (_RESEARCH_DIGEST_CHAR_CAP + 1) not in prompt
    assert "c" * _RESEARCH_DIGEST_CHAR_CAP in prompt


def test_run_passes_through_inputs_under_cap(monkeypatch) -> None:
    prompt = _run_capturing_prompt(
        monkeypatch,
        brand_spec_prompt="short brand spec",
        writing_guidelines="short guidelines",
        research_digest="short digest",
    )
    assert "short brand spec" in prompt
    assert "short guidelines" in prompt
    assert "short digest" in prompt


def test_run_empty_research_digest_uses_placeholder(monkeypatch) -> None:
    prompt = _run_capturing_prompt(
        monkeypatch, brand_spec_prompt="bs", writing_guidelines="wg", research_digest=""
    )
    assert "(no research digest supplied)" in prompt
