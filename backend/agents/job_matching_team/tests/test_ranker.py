"""Tests for the job ranker agent."""

from __future__ import annotations

from job_matching_team.agents.ranker import JobRankerAgent, _clamp
from job_matching_team.models import JobPosting
from job_matching_team.profile.model import JobSeekerProfile

from .conftest import ScriptedLLM


def _llm_with_scores(score_by_company):
    def handler(prompt, system):
        for company, payload in score_by_company.items():
            if company in prompt:
                return payload
        return {}

    return ScriptedLLM(handler)


def test_rank_sorts_descending():
    postings = [
        JobPosting(company="Low", title="Eng").ensure_fingerprint(),
        JobPosting(company="High", title="Eng").ensure_fingerprint(),
    ]
    llm = _llm_with_scores(
        {
            "Low": {
                f: 0.2
                for f in (
                    "title_fit",
                    "seniority_fit",
                    "location_fit",
                    "comp_fit",
                    "company_fit",
                    "skills_fit",
                )
            },
            "High": {
                f: 0.9
                for f in (
                    "title_fit",
                    "seniority_fit",
                    "location_fit",
                    "comp_fit",
                    "company_fit",
                    "skills_fit",
                )
            },
        }
    )
    ranked = JobRankerAgent(llm_client=llm).rank(postings, JobSeekerProfile())
    assert [r.posting.company for r in ranked] == ["High", "Low"]
    assert ranked[0].score > ranked[1].score


def test_excluded_company_forced_skip():
    posting = JobPosting(company="Example Bad Co", title="Eng").ensure_fingerprint()
    llm = _llm_with_scores(
        {
            "Example Bad Co": {
                f: 1.0
                for f in (
                    "title_fit",
                    "seniority_fit",
                    "location_fit",
                    "comp_fit",
                    "company_fit",
                    "skills_fit",
                )
            }
        }
    )
    profile = JobSeekerProfile(excluded_companies=["Example Bad Co"])
    ranked = JobRankerAgent(llm_client=llm).rank([posting], profile)
    assert ranked[0].recommendation == "skip"
    assert any("Excluded company" in c for c in ranked[0].concerns)


def test_salary_floor_forces_skip():
    posting = JobPosting(company="Acme", title="Eng", salary_max=90000).ensure_fingerprint()
    llm = _llm_with_scores(
        {
            "Acme": {
                f: 1.0
                for f in (
                    "title_fit",
                    "seniority_fit",
                    "location_fit",
                    "comp_fit",
                    "company_fit",
                    "skills_fit",
                )
            }
        }
    )
    profile = JobSeekerProfile(salary_min=150000)
    ranked = JobRankerAgent(llm_client=llm).rank([posting], profile)
    assert ranked[0].recommendation == "skip"


def test_deal_breaker_forces_skip():
    posting = JobPosting(
        company="Acme", title="Eng", description="Requires relocation to HQ"
    ).ensure_fingerprint()
    llm = _llm_with_scores(
        {
            "Acme": {
                f: 1.0
                for f in (
                    "title_fit",
                    "seniority_fit",
                    "location_fit",
                    "comp_fit",
                    "company_fit",
                    "skills_fit",
                )
            }
        }
    )
    profile = JobSeekerProfile(deal_breakers=["relocation"])
    ranked = JobRankerAgent(llm_client=llm).rank([posting], profile)
    assert ranked[0].recommendation == "skip"


def test_band_recommendation_when_llm_omits_it():
    posting = JobPosting(company="Acme", title="Eng").ensure_fingerprint()
    # No recommendation in payload -> derived from score band (all 0.9 -> apply).
    llm = _llm_with_scores(
        {
            "Acme": {
                f: 0.9
                for f in (
                    "title_fit",
                    "seniority_fit",
                    "location_fit",
                    "comp_fit",
                    "company_fit",
                    "skills_fit",
                )
            }
        }
    )
    ranked = JobRankerAgent(llm_client=llm).rank([posting], JobSeekerProfile())
    assert ranked[0].recommendation == "apply"


def test_llm_failure_yields_neutral_scores():
    class BrokenLLM:
        def complete_json(self, *a, **k):
            raise RuntimeError("down")

    posting = JobPosting(company="Acme", title="Eng").ensure_fingerprint()
    ranked = JobRankerAgent(llm_client=BrokenLLM()).rank([posting], JobSeekerProfile())
    assert ranked[0].sub_scores.comp_fit == 0.5  # default for unstated comp
    assert ranked[0].recommendation == "skip"  # low score band


def test_empty_postings_returns_empty():
    assert (
        JobRankerAgent(llm_client=ScriptedLLM(lambda p, s: {})).rank([], JobSeekerProfile()) == []
    )


def test_clamp_helper():
    assert _clamp(2.0) == 1.0
    assert _clamp(-1.0) == 0.0
    assert _clamp("x") == 0.0
    assert _clamp("x", default=0.5) == 0.5
    assert _clamp(0.42) == 0.42
