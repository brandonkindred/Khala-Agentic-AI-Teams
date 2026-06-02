"""Tests for the query builder agent."""

from __future__ import annotations

from job_matching_team.agents.query_builder import QueryBuilderAgent
from job_matching_team.profile.model import JobSeekerProfile


def test_llm_queries_used_and_deduped(scripted_llm):
    llm = scripted_llm(lambda p, s: {"queries": ["python jobs", "python jobs", "go jobs"]})
    agent = QueryBuilderAgent(llm_client=llm)
    out = agent.build(JobSeekerProfile(target_titles=["Eng"]), max_queries=5)
    assert out == ["python jobs", "go jobs"]
    assert len(llm.calls) == 1


def test_respects_max_queries(scripted_llm):
    llm = scripted_llm(lambda p, s: {"queries": [f"q{i}" for i in range(20)]})
    agent = QueryBuilderAgent(llm_client=llm)
    out = agent.build(JobSeekerProfile(target_titles=["Eng"]), max_queries=3)
    assert len(out) == 3


def test_string_queries_treated_as_single_query(scripted_llm):
    # A bare string (not a list) must not be iterated character-by-character.
    llm = scripted_llm(lambda p, s: {"queries": "python backend jobs"})
    agent = QueryBuilderAgent(llm_client=llm)
    out = agent.build(JobSeekerProfile(target_titles=["Eng"]), max_queries=5)
    assert out == ["python backend jobs"]


def test_non_list_non_str_queries_falls_back(scripted_llm):
    # A dict/garbage value yields no LLM queries -> deterministic fallback.
    llm = scripted_llm(lambda p, s: {"queries": {"unexpected": 1}})
    agent = QueryBuilderAgent(llm_client=llm)
    out = agent.build(JobSeekerProfile(target_titles=["Backend Engineer"]), max_queries=3)
    assert out
    assert any("Backend Engineer" in q for q in out)


def test_fallback_when_llm_returns_nothing(scripted_llm):
    llm = scripted_llm(lambda p, s: {"queries": []})
    agent = QueryBuilderAgent(llm_client=llm)
    profile = JobSeekerProfile(
        target_titles=["Backend Engineer"],
        locations=["NYC"],
        remote_preference="remote",
        preferred_companies=["Stripe"],
    )
    out = agent.build(profile, max_queries=6)
    assert out  # deterministic fallback produced queries
    assert any("Backend Engineer" in q for q in out)
    assert any("remote" in q for q in out)


def test_fallback_when_llm_raises():
    class Boom:
        def complete_json(self, *a, **k):
            raise RuntimeError("down")

    agent = QueryBuilderAgent(llm_client=Boom())
    out = agent.build(JobSeekerProfile(target_titles=["SRE"]), max_queries=2)
    assert out
    assert all(q.strip() for q in out)


def test_fallback_with_empty_profile_uses_default_title():
    agent = QueryBuilderAgent(llm_client=type("L", (), {"complete_json": lambda *a, **k: {}})())
    out = agent.build(JobSeekerProfile(), max_queries=3)
    assert out
    assert any("software engineer" in q for q in out)
