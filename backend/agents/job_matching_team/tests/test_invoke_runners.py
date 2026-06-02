"""Tests for the Agent Console invoke entrypoints."""

from __future__ import annotations

import pytest

from job_matching_team import invoke_runners
from job_matching_team.models import JobPosting, RankedJob


def test_invoke_scanner_requires_queries():
    with pytest.raises(ValueError, match="non-empty 'queries'"):
        invoke_runners.invoke_scanner({"queries": []})


def test_invoke_scanner_delegates(monkeypatch):
    captured = {}

    class FakeScanner:
        def scan(self, queries, *, max_roles):
            captured["queries"] = queries
            captured["max_roles"] = max_roles
            return [JobPosting(company="Acme", title="Eng").ensure_fingerprint()]

    monkeypatch.setattr(invoke_runners, "JobScannerAgent", lambda: FakeScanner())
    out = invoke_runners.invoke_scanner({"queries": ["a", " ", "b"], "max_roles": 7})
    assert captured["queries"] == ["a", "b"]
    assert captured["max_roles"] == 7
    assert out["postings"][0]["company"] == "Acme"


def test_invoke_ranker_delegates(monkeypatch):
    class FakeRanker:
        def rank(self, postings, profile):
            return [RankedJob(posting=p, score=0.5) for p in postings]

    monkeypatch.setattr(invoke_runners, "JobRankerAgent", lambda: FakeRanker())
    body = {
        "profile": {"target_titles": ["Eng"]},
        "postings": [{"company": "Acme", "title": "Eng"}],
    }
    out = invoke_runners.invoke_ranker(body)
    assert len(out["ranked_jobs"]) == 1
    assert out["ranked_jobs"][0]["posting"]["fingerprint"]
