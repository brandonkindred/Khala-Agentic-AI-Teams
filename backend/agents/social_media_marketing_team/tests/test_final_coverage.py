"""Final-mile tests targeting the last residual uncovered branches."""

from __future__ import annotations

import builtins
from typing import Any

import pytest
from fastapi.testclient import TestClient

from social_media_marketing_team.api import main as api_main
from social_media_marketing_team.api.main import app


@pytest.fixture
def fake_jobs(monkeypatch: pytest.MonkeyPatch, fake_job_client):
    monkeypatch.setattr(api_main, "_job_manager", fake_job_client)
    return fake_job_client


# ---------------------------------------------------------------------------
# api/main.py — ImportError branches on list/get/delete winning-post routes
# ---------------------------------------------------------------------------


def _install_bad_import(
    monkeypatch: pytest.MonkeyPatch, target_module: str = "social_media_marketing_team.shared"
) -> None:
    real_import = builtins.__import__

    def _bad(name, *args, **kwargs):
        if name == target_module:
            raise ImportError("disabled in test")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _bad)


def test_list_winning_posts_503_when_import_fails(
    fake_jobs, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_bad_import(monkeypatch)
    client = TestClient(app)
    resp = client.get("/social-marketing/winning-posts")
    assert resp.status_code == 503


def test_get_winning_post_503_when_import_fails(
    fake_jobs, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_bad_import(monkeypatch)
    client = TestClient(app)
    resp = client.get("/social-marketing/winning-posts/abc")
    assert resp.status_code == 503


def test_delete_winning_post_503_when_import_fails(
    fake_jobs, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_bad_import(monkeypatch)
    client = TestClient(app)
    resp = client.delete("/social-marketing/winning-posts/abc")
    assert resp.status_code == 503


# ---------------------------------------------------------------------------
# api/main.py — module-level _job_manager init exception block (lines 101-104)
#
# The block runs at import time. We cover it by re-importing the module
# under a JobServiceClient that raises in its constructor.
# ---------------------------------------------------------------------------


def test_job_manager_init_failure_is_caught(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch JobServiceClient to raise and reload api.main so the except
    block on lines 101-104 fires."""
    import sys

    import job_service_client as jsc_mod

    class _BadClient:
        def __init__(self, *a, **k):
            raise RuntimeError("init failed in test")

    real_client = jsc_mod.JobServiceClient
    monkeypatch.setattr(jsc_mod, "JobServiceClient", _BadClient)

    import social_media_marketing_team.api as api_pkg

    # Remove module so reimport runs top-level body
    saved = sys.modules.pop("social_media_marketing_team.api.main", None)
    try:
        import importlib

        importlib.import_module("social_media_marketing_team.api.main")
        reloaded = sys.modules["social_media_marketing_team.api.main"]
        assert reloaded._job_manager is None
        assert reloaded._stale_monitor_stop is None
    finally:
        # Restore originals so other tests are unaffected
        monkeypatch.setattr(jsc_mod, "JobServiceClient", real_client)
        if saved is not None:
            sys.modules["social_media_marketing_team.api.main"] = saved
            # Re-bind the attribute on the parent package so attribute lookups
            # (`social_media_marketing_team.api.main`) and `sys.modules` agree
            # — otherwise the reloaded module lingers as `api.main` and later
            # monkeypatches hit the wrong object.
            api_pkg.main = saved
        else:
            sys.modules.pop("social_media_marketing_team.api.main", None)
            import importlib

            fresh = importlib.import_module("social_media_marketing_team.api.main")
            api_pkg.main = fresh


# ---------------------------------------------------------------------------
# orchestrator._load_winners — execute the happy path with a stub
# `find_relevant_winners` injected so lines 266-272 run.
# ---------------------------------------------------------------------------


def test_load_winners_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Inject a ``find_relevant_winners`` symbol into the bank module so
    the optimistic import inside ``_load_winners`` succeeds."""
    from social_media_marketing_team.models import BrandGoals, CampaignProposal
    from social_media_marketing_team.orchestrator import SocialMediaMarketingOrchestrator
    from social_media_marketing_team.shared import winning_posts_bank as wpb

    captured: dict[str, Any] = {}

    def _fake_find(**kwargs):
        captured.update(kwargs)
        return [{"id": "1"}, {"id": "2"}]

    monkeypatch.setattr(wpb, "find_relevant_winners", _fake_find, raising=False)

    proposal = CampaignProposal(
        campaign_name="c",
        objective="grow",
        audience_hypothesis="h",
        messaging_pillars=["pillar"],
    )
    goals = BrandGoals(brand_name="b", target_audience="a", goals=["growth"])
    out = SocialMediaMarketingOrchestrator._load_winners("brand-x", proposal, goals)
    assert len(out) == 2
    assert captured["brand_id"] == "brand-x"
    assert captured["limit"] == 10
    assert captured["concept_opportunity"] == "grow"
    assert "pillar" in captured["query_keywords"]
    assert "growth" in captured["query_keywords"]


# ---------------------------------------------------------------------------
# trend_discovery_agent — exercise the exception branch in synthesis
# ---------------------------------------------------------------------------


def test_trend_agent_logs_when_synthesis_throws(monkeypatch: pytest.MonkeyPatch, caplog) -> None:
    """If the agent invocation raises, the except branch (lines 184-187) logs
    a warning and returns an empty topic list."""
    from blog_research_agent.models import CandidateResult

    from llm_service import DummyLLMClient
    from social_media_marketing_team.trend_discovery_agent import TrendDiscoveryAgent

    class _SearchOK:
        def search(self, *a, **k):
            return [
                CandidateResult(
                    title="t", url="https://e.test", snippet="s", source="x", rank=1
                )
            ]

    agent = TrendDiscoveryAgent(llm_client=DummyLLMClient(), web_search=_SearchOK())

    def _explode(*a, **k):
        raise RuntimeError("synthesis blew up")

    # Replace the Strands Agent on this instance with a callable that raises
    agent._agent = _explode  # type: ignore[assignment]

    with caplog.at_level("WARNING"):
        digest = agent.run(date="2026-01-01")

    assert digest.topics == []
    assert any("LLM synthesis failed" in r.message for r in caplog.records)
