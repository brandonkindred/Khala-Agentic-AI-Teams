"""Tests for the AI Sales Team pod — including outcome tracking and learning loop.

All tests run without the strands SDK installed (agents operate in stub mode),
making them suitable for CI environments without AWS credentials.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from sales_team.models import (
    BANTScore,
    CloseType,
    DealOutcome,
    IdealCustomerProfile,
    LearningInsights,
    OutcomeResult,
    PipelineStage,
    Prospect,
    SalesPipelineRequest,
    SalesPipelineResult,
    StageOutcome,
)
from sales_team.orchestrator import SalesPodOrchestrator, _parse_json, _prospects_from_json

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def sample_icp() -> IdealCustomerProfile:
    return IdealCustomerProfile(
        industry=["SaaS", "FinTech"],
        company_size_min=50,
        company_size_max=2000,
        job_titles=["VP Sales", "CRO", "Head of Revenue"],
        pain_points=["manual reporting", "long sales cycles", "poor pipeline visibility"],
        budget_range_usd="$20k–$80k/yr",
        geographic_focus=["United States", "Canada"],
        tech_stack_keywords=["Salesforce", "HubSpot", "Outreach"],
        disqualifying_traits=["non-profit", "government"],
    )


@pytest.fixture()
def sample_prospect() -> Prospect:
    return Prospect(
        company_name="Acme Corp",
        website="https://acme.example.com",
        contact_name="Jane Smith",
        contact_title="VP of Sales",
        contact_email=None,
        linkedin_url="https://linkedin.com/in/jane-smith-example",
        company_size_estimate="200–500",
        industry="SaaS",
        icp_match_score=0.85,
        research_notes="Recently raised Series B; hiring 10 AEs; uses Salesforce.",
        trigger_events=["Series B funding announced"],
    )


@pytest.fixture()
def orchestrator() -> SalesPodOrchestrator:
    return SalesPodOrchestrator()


@pytest.fixture()
def api_client():
    from sales_team.api.main import app

    return TestClient(app)


# ---------------------------------------------------------------------------
# Model tests
# ---------------------------------------------------------------------------


class TestModels:
    def test_icp_defaults(self):
        icp = IdealCustomerProfile()
        assert icp.company_size_min == 10
        assert icp.company_size_max == 5000
        assert icp.industry == []

    def test_prospect_score_bounds(self):
        with pytest.raises(Exception):
            Prospect(company_name="X", icp_match_score=1.5)

        with pytest.raises(Exception):
            Prospect(company_name="X", icp_match_score=-0.1)

    def test_prospect_valid(self, sample_prospect):
        assert sample_prospect.icp_match_score == 0.85
        assert sample_prospect.company_name == "Acme Corp"

    def test_bant_score_bounds(self):
        with pytest.raises(Exception):
            BANTScore(budget=11, authority=5, need=5, timeline=5)

    def test_bant_score_valid(self):
        bant = BANTScore(budget=7, authority=6, need=9, timeline=6)
        assert bant.budget == 7

    def test_pipeline_stage_enum(self):
        assert PipelineStage.PROSPECTING.value == "prospecting"
        assert PipelineStage.CLOSED_WON.value == "closed_won"

    def test_sales_pipeline_request_defaults(self, sample_icp, sample_prospect):
        req = SalesPipelineRequest(
            product_name="AcmeSales",
            value_proposition="Close more deals faster",
            icp=sample_icp,
        )
        assert req.entry_stage == PipelineStage.PROSPECTING
        assert req.max_prospects == 5
        assert req.existing_prospects == []

    def test_max_prospects_bounds(self, sample_icp):
        with pytest.raises(Exception):
            SalesPipelineRequest(
                product_name="X",
                value_proposition="Y",
                icp=sample_icp,
                max_prospects=25,
            )


# ---------------------------------------------------------------------------
# Parser helper tests
# ---------------------------------------------------------------------------


class TestParsers:
    def test_parse_json_valid(self):
        result = _parse_json('{"key": "value"}', {})
        assert result == {"key": "value"}

    def test_parse_json_array(self):
        result = _parse_json('[{"a": 1}]', [])
        assert result == [{"a": 1}]

    def test_parse_json_invalid_returns_fallback(self):
        result = _parse_json("not valid json", {"fallback": True})
        assert result == {"fallback": True}

    def test_parse_json_empty_returns_fallback(self):
        result = _parse_json("", [])
        assert result == []

    def test_parse_json_strips_markdown_fences(self):
        raw = '```json\n{"key": 1}\n```'
        result = _parse_json(raw, {})
        assert result == {"key": 1}

    def test_prospects_from_json_valid(self, sample_prospect):
        data = json.dumps([sample_prospect.model_dump()])
        prospects = _prospects_from_json(data)
        assert len(prospects) == 1
        assert prospects[0].company_name == "Acme Corp"

    def test_prospects_from_json_invalid(self):
        prospects = _prospects_from_json("not json at all")
        assert prospects == []

    def test_prospects_from_json_empty_array(self):
        prospects = _prospects_from_json("[]")
        assert prospects == []


# ---------------------------------------------------------------------------
# Agent stub tests (no strands SDK required)
# ---------------------------------------------------------------------------


class TestAgentStubs:
    """Verify agents work correctly in stub mode (no strands SDK)."""

    def test_prospector_returns_parseable_json(self, orchestrator, sample_icp):
        raw = orchestrator.prospector.prospect(
            sample_icp.model_dump_json(), "TestProduct", "We help X", 3, ""
        )
        data = json.loads(raw)
        assert isinstance(data, list)
        assert len(data) > 0
        assert "company_name" in data[0]
        assert "icp_match_score" in data[0]

    def test_outreach_returns_parseable_json(self, orchestrator, sample_prospect):
        raw = orchestrator.outreach.generate_sequence(
            sample_prospect.model_dump_json(), "TestProduct", "We help X", "", ""
        )
        data = json.loads(raw)
        assert "email_sequence" in data
        assert "call_script" in data
        assert "linkedin_message" in data

    def test_qualifier_returns_parseable_json(self, orchestrator, sample_prospect):
        raw = orchestrator.qualifier.qualify(
            sample_prospect.model_dump_json(), "TestProduct", "We help X", ""
        )
        data = json.loads(raw)
        assert "bant" in data
        assert "meddic" in data
        assert "overall_score" in data
        assert 0.0 <= data["overall_score"] <= 1.0

    def test_nurture_returns_parseable_json(self, orchestrator, sample_prospect):
        raw = orchestrator.nurture.build_sequence(
            sample_prospect.model_dump_json(), "TestProduct", "We help X", 90
        )
        data = json.loads(raw)
        assert "touchpoints" in data
        assert "re_engagement_triggers" in data
        assert isinstance(data["touchpoints"], list)

    def test_discovery_returns_parseable_json(self, orchestrator, sample_prospect):
        raw = orchestrator.discovery.prepare(
            sample_prospect.model_dump_json(), "{}", "TestProduct", "We help X"
        )
        data = json.loads(raw)
        assert "spin_questions" in data
        assert "situation" in data["spin_questions"]
        assert "demo_agenda" in data

    def test_proposal_returns_parseable_json(self, orchestrator, sample_prospect):
        raw = orchestrator.proposal.write(
            sample_prospect.model_dump_json(), "TestProduct", "We help X", 25000.0, "", "", ""
        )
        data = json.loads(raw)
        assert "executive_summary" in data
        assert "roi_model" in data
        assert data["roi_model"]["annual_cost_usd"] == 25000.0

    def test_closer_returns_parseable_json(self, orchestrator, sample_prospect):
        raw = orchestrator.closer.develop_strategy(
            sample_prospect.model_dump_json(), "{}", "TestProduct", "We help X"
        )
        data = json.loads(raw)
        assert "recommended_close_technique" in data
        assert "objection_handlers" in data

    def test_coach_returns_parseable_json(self, orchestrator, sample_prospect):
        raw = orchestrator.coach.review(
            json.dumps([sample_prospect.model_dump()]), "TestProduct", ""
        )
        data = json.loads(raw)
        assert "deal_risk_signals" in data
        assert "coaching_summary" in data


# ---------------------------------------------------------------------------
# Orchestrator integration tests
# ---------------------------------------------------------------------------


class TestOrchestrator:
    def test_prospect_only(self, orchestrator, sample_icp):
        prospects = orchestrator.prospect_only(sample_icp, "TestProduct", "We help X", 3, "")
        assert isinstance(prospects, list)
        assert len(prospects) > 0
        assert all(hasattr(p, "company_name") for p in prospects)

    def test_outreach_only(self, orchestrator, sample_prospect):
        sequences = orchestrator.outreach_only(
            [sample_prospect], "TestProduct", "We help X", [], ""
        )
        assert len(sequences) == 1
        assert sequences[0].prospect.company_name == "Acme Corp"

    def test_qualify_only(self, orchestrator, sample_prospect):
        score = orchestrator.qualify_only(sample_prospect, "TestProduct", "We help X", "")
        assert score is not None
        assert 0.0 <= score.overall_score <= 1.0
        assert score.value_creation_level in (1, 2, 3, 4)

    def test_nurture_only(self, orchestrator, sample_prospect):
        sequences = orchestrator.nurture_only([sample_prospect], "TestProduct", "We help X", 60)
        assert len(sequences) == 1
        assert sequences[0].duration_days > 0

    def test_coach_only(self, orchestrator, sample_prospect):
        report = orchestrator.coach_only([sample_prospect], "TestProduct", "")
        assert report is not None
        assert report.prospects_reviewed >= 0
        assert isinstance(report.deal_risk_signals, list)

    def test_should_run_logic(self, orchestrator):
        assert orchestrator._should_run(PipelineStage.PROSPECTING, PipelineStage.PROSPECTING)
        assert orchestrator._should_run(PipelineStage.OUTREACH, PipelineStage.PROSPECTING)
        assert not orchestrator._should_run(PipelineStage.PROSPECTING, PipelineStage.OUTREACH)
        assert orchestrator._should_run(PipelineStage.PROPOSAL, PipelineStage.PROPOSAL)

    def test_full_pipeline_from_prospecting(self, orchestrator, sample_icp):
        request = SalesPipelineRequest(
            product_name="TestProduct",
            value_proposition="We help sales teams close more deals faster",
            icp=sample_icp,
            max_prospects=1,
        )
        stages_called = []

        def on_update(stage: str, pct: int) -> None:
            stages_called.append(stage)

        result = orchestrator.run(request, job_id="test-job-001", update_cb=on_update)

        assert isinstance(result, SalesPipelineResult)
        assert result.job_id == "test-job-001"
        assert len(result.prospects) > 0
        assert len(result.outreach_sequences) > 0
        assert len(result.qualified_leads) > 0
        assert "prospecting" in stages_called

    def test_full_pipeline_with_existing_prospects(self, orchestrator, sample_prospect, sample_icp):
        request = SalesPipelineRequest(
            product_name="TestProduct",
            value_proposition="We help sales teams close more deals faster",
            icp=sample_icp,
            entry_stage=PipelineStage.OUTREACH,
            existing_prospects=[sample_prospect],
        )
        result = orchestrator.run(request, job_id="test-job-002")
        # Should skip prospecting, start at outreach
        assert len(result.outreach_sequences) > 0

    def test_pipeline_stops_gracefully_with_no_prospects(self, orchestrator, sample_icp):
        request = SalesPipelineRequest(
            product_name="TestProduct",
            value_proposition="We help sales teams close more deals faster",
            icp=sample_icp,
            entry_stage=PipelineStage.OUTREACH,
            existing_prospects=[],
        )
        # Patch the outreach stage to skip since no prospects
        result = orchestrator.run(request, job_id="test-job-003")
        assert result.summary != ""


# ---------------------------------------------------------------------------
# API endpoint tests
# ---------------------------------------------------------------------------


class TestAPI:
    def test_health(self, api_client):
        response = api_client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert "strands_sdk" in data

    def test_prospect_endpoint(self, api_client, sample_icp):
        payload = {
            "icp": sample_icp.model_dump(),
            "product_name": "TestProduct",
            "value_proposition": "We help sales teams close faster",
            "max_prospects": 2,
        }
        response = api_client.post("/sales/prospect", json=payload)
        assert response.status_code == 200
        data = response.json()
        assert "prospects" in data
        assert "count" in data
        assert data["count"] >= 0

    def test_outreach_endpoint(self, api_client, sample_prospect):
        payload = {
            "prospects": [sample_prospect.model_dump()],
            "product_name": "TestProduct",
            "value_proposition": "We help X",
        }
        response = api_client.post("/sales/outreach", json=payload)
        assert response.status_code == 200
        data = response.json()
        assert "sequences" in data

    def test_qualify_endpoint(self, api_client, sample_prospect):
        payload = {
            "prospect": sample_prospect.model_dump(),
            "product_name": "TestProduct",
            "value_proposition": "We help X",
            "call_notes": "",
        }
        response = api_client.post("/sales/qualify", json=payload)
        assert response.status_code == 200
        data = response.json()
        assert "overall_score" in data
        assert "recommended_action" in data

    def test_nurture_endpoint(self, api_client, sample_prospect):
        payload = {
            "prospects": [sample_prospect.model_dump()],
            "product_name": "TestProduct",
            "value_proposition": "We help X",
            "duration_days": 30,
        }
        response = api_client.post("/sales/nurture", json=payload)
        assert response.status_code == 200
        data = response.json()
        assert "sequences" in data

    def test_proposal_endpoint(self, api_client, sample_prospect):
        payload = {
            "prospect": sample_prospect.model_dump(),
            "product_name": "TestProduct",
            "value_proposition": "We help X",
            "annual_cost_usd": 24000.0,
        }
        response = api_client.post("/sales/proposal", json=payload)
        assert response.status_code == 200
        data = response.json()
        assert "executive_summary" in data
        assert "roi_model" in data

    def test_coaching_endpoint(self, api_client, sample_prospect):
        payload = {
            "prospects": [sample_prospect.model_dump()],
            "product_name": "TestProduct",
            "pipeline_context": "",
        }
        response = api_client.post("/sales/coaching", json=payload)
        assert response.status_code == 200
        data = response.json()
        assert "coaching_summary" in data
        assert "deal_risk_signals" in data

    def test_pipeline_run_returns_job_id(self, api_client, sample_icp):
        payload = {
            "product_name": "TestProduct",
            "value_proposition": "We help sales teams close more deals faster",
            "icp": sample_icp.model_dump(),
            "max_prospects": 1,
        }
        response = api_client.post("/sales/pipeline/run", json=payload)
        assert response.status_code == 200
        data = response.json()
        assert "job_id" in data
        assert "status" in data

    def test_pipeline_status_not_found(self, api_client):
        response = api_client.get("/sales/pipeline/status/nonexistent-job-id")
        assert response.status_code == 404

    def test_pipeline_list_jobs(self, api_client):
        response = api_client.get("/sales/pipeline/jobs")
        assert response.status_code == 200
        assert isinstance(response.json(), list)

    def test_cancel_nonexistent_job(self, api_client):
        response = api_client.post("/sales/pipeline/job/nonexistent/cancel")
        assert response.status_code == 404

    def test_delete_nonexistent_job(self, api_client):
        response = api_client.delete("/sales/pipeline/job/nonexistent")
        assert response.status_code == 404

    def test_delete_active_job_returns_409(self, api_client, sample_icp):
        """Deleting a running job must be rejected with 409 — cancel first."""
        payload = {
            "product_name": "TestProduct",
            "value_proposition": "We help sales teams close more deals faster",
            "icp": sample_icp.model_dump(),
            "max_prospects": 1,
        }
        run_resp = api_client.post("/sales/pipeline/run", json=payload)
        assert run_resp.status_code == 200
        job_id = run_resp.json()["job_id"]
        # Job starts as running/pending — delete should fail
        del_resp = api_client.delete(f"/sales/pipeline/job/{job_id}")
        assert del_resp.status_code == 409


# ---------------------------------------------------------------------------
# Outcome store tests
# ---------------------------------------------------------------------------


class TestOutcomeStore:
    """Test file-backed outcome persistence in isolation using a temp directory."""

    @pytest.fixture(autouse=True)
    def _tmp_cache(self, tmp_path, monkeypatch):
        """Redirect all store I/O to a temporary directory."""
        import sales_team.outcome_store as store

        monkeypatch.setattr(store, "_CACHE_ROOT", tmp_path / "outcomes")
        monkeypatch.setattr(store, "_INSIGHTS_PATH", tmp_path / "insights" / "current.json")

    def test_record_and_load_stage_outcome(self):
        from sales_team.outcome_store import load_stage_outcomes, record_stage_outcome

        outcome = StageOutcome(
            company_name="TestCo",
            stage=PipelineStage.OUTREACH,
            outcome=OutcomeResult.CONVERTED,
            subject_line_used="Saw your Series B — quick thought",
            email_touch_number=2,
        )
        saved = record_stage_outcome(outcome)

        assert saved.outcome_id != ""
        assert saved.recorded_at != ""

        loaded = load_stage_outcomes()
        assert len(loaded) == 1
        assert loaded[0].company_name == "TestCo"
        assert loaded[0].outcome == OutcomeResult.CONVERTED
        assert loaded[0].subject_line_used == "Saw your Series B — quick thought"

    def test_record_and_load_deal_outcome(self):
        from sales_team.outcome_store import load_deal_outcomes, record_deal_outcome

        outcome = DealOutcome(
            company_name="WinCo",
            industry="SaaS",
            deal_size_usd=48000.0,
            final_stage_reached=PipelineStage.NEGOTIATION,
            result=OutcomeResult.WON,
            win_factor="Champion + EB both engaged from discovery",
            close_technique_used=CloseType.SUMMARY,
            stages_completed=[
                PipelineStage.PROSPECTING,
                PipelineStage.OUTREACH,
                PipelineStage.QUALIFICATION,
            ],
            icp_match_score=0.88,
            qualification_score=0.79,
            sales_cycle_days=34,
        )
        saved = record_deal_outcome(outcome)

        assert saved.outcome_id != ""
        loaded = load_deal_outcomes()
        assert len(loaded) == 1
        assert loaded[0].result == OutcomeResult.WON
        assert loaded[0].deal_size_usd == 48000.0

    def test_multiple_outcomes_sorted_newest_first(self):
        from sales_team.outcome_store import load_stage_outcomes, record_stage_outcome

        for i in range(3):
            record_stage_outcome(
                StageOutcome(
                    company_name=f"Co{i}",
                    stage=PipelineStage.QUALIFICATION,
                    outcome=OutcomeResult.STALLED,
                )
            )
        loaded = load_stage_outcomes()
        assert len(loaded) == 3

    def test_outcome_counts(self):
        from sales_team.outcome_store import (
            outcome_counts,
            record_stage_outcome,
        )

        assert outcome_counts()["stage_outcomes"] == 0
        record_stage_outcome(
            StageOutcome(
                company_name="A", stage=PipelineStage.OUTREACH, outcome=OutcomeResult.CONVERTED
            )
        )
        assert outcome_counts()["stage_outcomes"] == 1

    def test_save_and_load_insights(self):
        from sales_team.outcome_store import load_current_insights, save_insights

        insights = LearningInsights(
            total_outcomes_analyzed=10,
            win_rate=0.4,
            top_performing_industries=["SaaS"],
            insights_version=1,
        )
        save_insights(insights)
        loaded = load_current_insights()
        assert loaded is not None
        assert loaded.win_rate == 0.4
        assert loaded.insights_version == 1


# ---------------------------------------------------------------------------
# Heuristic learning engine tests
# ---------------------------------------------------------------------------


class TestHeuristicLearning:
    """Test heuristic insight computation without requiring Strands SDK."""

    def test_empty_outcomes(self):
        from sales_team.outcome_store import compute_heuristic_insights

        insights = compute_heuristic_insights([], [])
        assert insights.total_outcomes_analyzed == 0
        assert insights.win_rate == 0.0
        assert len(insights.actionable_recommendations) > 0

    def test_win_rate_calculation(self):
        from sales_team.outcome_store import compute_heuristic_insights

        deals = [
            DealOutcome(
                company_name=f"Co{i}",
                final_stage_reached=PipelineStage.NEGOTIATION,
                result=OutcomeResult.WON if i < 3 else OutcomeResult.LOST,
            )
            for i in range(5)
        ]
        insights = compute_heuristic_insights([], deals)
        assert insights.win_rate == pytest.approx(0.6)

    def test_top_industries_from_wins(self):
        from sales_team.outcome_store import compute_heuristic_insights

        deals = [
            DealOutcome(
                company_name=f"SaasCo{i}",
                industry="SaaS",
                final_stage_reached=PipelineStage.NEGOTIATION,
                result=OutcomeResult.WON,
            )
            for i in range(3)
        ] + [
            DealOutcome(
                company_name="FinCo",
                industry="FinTech",
                final_stage_reached=PipelineStage.NEGOTIATION,
                result=OutcomeResult.LOST,
            )
        ]
        insights = compute_heuristic_insights([], deals)
        assert "SaaS" in insights.top_performing_industries

    def test_common_objections_from_stage_outcomes(self):
        from sales_team.outcome_store import compute_heuristic_insights

        stages = [
            StageOutcome(
                company_name=f"Co{i}",
                stage=PipelineStage.NEGOTIATION,
                outcome=OutcomeResult.OBJECTION,
                objection_text="Price is too high",
            )
            for i in range(4)
        ]
        insights = compute_heuristic_insights(stages, [])
        assert "Price is too high" in insights.common_objections

    def test_best_close_techniques_from_wins(self):
        from sales_team.outcome_store import compute_heuristic_insights

        deals = [
            DealOutcome(
                company_name=f"Co{i}",
                final_stage_reached=PipelineStage.NEGOTIATION,
                result=OutcomeResult.WON,
                close_technique_used=CloseType.SUMMARY,
            )
            for i in range(3)
        ]
        insights = compute_heuristic_insights([], deals)
        assert "summary" in insights.best_close_techniques

    def test_stage_conversion_rates(self):
        from sales_team.outcome_store import compute_heuristic_insights

        stages = [
            StageOutcome(
                company_name=f"Co{i}", stage=PipelineStage.OUTREACH, outcome=OutcomeResult.CONVERTED
            )
            for i in range(3)
        ] + [
            StageOutcome(
                company_name=f"Co{i + 3}",
                stage=PipelineStage.OUTREACH,
                outcome=OutcomeResult.STALLED,
            )
            for i in range(1)
        ]
        insights = compute_heuristic_insights(stages, [])
        assert "outreach" in insights.stage_conversion_rates
        assert insights.stage_conversion_rates["outreach"] == pytest.approx(0.75)

    def test_insights_version_increments(self):
        from sales_team.outcome_store import compute_heuristic_insights

        i1 = compute_heuristic_insights([], [], current_version=0)
        i2 = compute_heuristic_insights([], [], current_version=i1.insights_version)
        assert i2.insights_version == i1.insights_version + 1


# ---------------------------------------------------------------------------
# LearningEngine integration tests
# ---------------------------------------------------------------------------


class TestLearningEngine:
    @pytest.fixture(autouse=True)
    def _tmp_cache(self, tmp_path, monkeypatch):
        import sales_team.outcome_store as store

        monkeypatch.setattr(store, "_CACHE_ROOT", tmp_path / "outcomes")
        monkeypatch.setattr(store, "_INSIGHTS_PATH", tmp_path / "insights" / "current.json")

    def test_refresh_empty_store(self):
        from sales_team.learning_engine import LearningEngine

        engine = LearningEngine()
        insights = engine.refresh()
        assert insights.total_outcomes_analyzed == 0
        assert len(insights.actionable_recommendations) > 0

    def test_refresh_with_outcomes(self):
        from sales_team.learning_engine import LearningEngine
        from sales_team.outcome_store import record_deal_outcome, record_stage_outcome

        record_stage_outcome(
            StageOutcome(
                company_name="A",
                stage=PipelineStage.OUTREACH,
                outcome=OutcomeResult.CONVERTED,
                subject_line_used="Funding trigger hook",
            )
        )
        record_deal_outcome(
            DealOutcome(
                company_name="A",
                industry="SaaS",
                final_stage_reached=PipelineStage.NEGOTIATION,
                result=OutcomeResult.WON,
                close_technique_used=CloseType.SUMMARY,
                sales_cycle_days=30,
            )
        )

        engine = LearningEngine()
        insights = engine.refresh()
        assert insights.total_outcomes_analyzed == 2
        assert insights.win_rate == 1.0
        assert insights.insights_version >= 1

    def test_refresh_persists_insights(self):
        from sales_team.learning_engine import LearningEngine
        from sales_team.outcome_store import load_current_insights

        engine = LearningEngine()
        engine.refresh()
        loaded = load_current_insights()
        assert loaded is not None
        assert loaded.insights_version >= 1

    def test_format_insights_empty(self):
        from sales_team.learning_engine import format_insights_for_prompt

        assert format_insights_for_prompt(None) == ""
        assert format_insights_for_prompt(LearningInsights()) == ""

    def test_format_insights_with_data(self):
        from sales_team.learning_engine import format_insights_for_prompt

        insights = LearningInsights(
            total_outcomes_analyzed=20,
            win_rate=0.55,
            winning_patterns=["Champion + EB both engaged"],
            common_objections=["Price too high"],
            best_close_techniques=["summary"],
            actionable_recommendations=["Focus on SaaS"],
            insights_version=3,
        )
        text = format_insights_for_prompt(insights)
        assert "20 past outcomes" in text
        assert "55%" in text
        assert "Champion + EB" in text
        assert "Price too high" in text
        assert "summary" in text


# ---------------------------------------------------------------------------
# Outcome + insights API endpoint tests
# ---------------------------------------------------------------------------


class TestOutcomeAPI:
    def test_record_stage_outcome(self, api_client, tmp_path, monkeypatch):
        import sales_team.outcome_store as store

        monkeypatch.setattr(store, "_CACHE_ROOT", tmp_path / "outcomes")
        monkeypatch.setattr(store, "_INSIGHTS_PATH", tmp_path / "insights" / "current.json")

        payload = {
            "company_name": "Acme Corp",
            "stage": "outreach",
            "outcome": "converted",
            "email_touch_number": 2,
            "subject_line_used": "Congrats on your Series B",
        }
        response = api_client.post("/sales/outcomes/stage", json=payload)
        assert response.status_code == 200
        data = response.json()
        assert "outcome_id" in data
        assert data["outcome_id"] != ""

    def test_record_deal_outcome_won(self, api_client, tmp_path, monkeypatch):
        import sales_team.outcome_store as store

        monkeypatch.setattr(store, "_CACHE_ROOT", tmp_path / "outcomes")
        monkeypatch.setattr(store, "_INSIGHTS_PATH", tmp_path / "insights" / "current.json")

        payload = {
            "company_name": "WinCo",
            "result": "won",
            "final_stage_reached": "negotiation",
            "deal_size_usd": 48000,
            "win_factor": "Strong champion + EB both on final call",
            "close_technique_used": "summary",
            "stages_completed": ["prospecting", "outreach", "qualification"],
            "sales_cycle_days": 28,
        }
        response = api_client.post("/sales/outcomes/deal", json=payload)
        assert response.status_code == 200
        data = response.json()
        assert "won" in data["message"]

    def test_outcome_summary(self, api_client):
        response = api_client.get("/sales/outcomes/summary")
        assert response.status_code == 200
        data = response.json()
        assert "stage_outcomes" in data
        assert "deal_outcomes" in data

    def test_insights_not_found_before_refresh(self, api_client, tmp_path, monkeypatch):
        import sales_team.outcome_store as store

        monkeypatch.setattr(store, "_CACHE_ROOT", tmp_path / "outcomes")
        monkeypatch.setattr(store, "_INSIGHTS_PATH", tmp_path / "insights" / "current.json")

        response = api_client.get("/sales/insights")
        assert response.status_code == 404

    def test_refresh_insights_endpoint(self, api_client, tmp_path, monkeypatch):
        import sales_team.outcome_store as store

        monkeypatch.setattr(store, "_CACHE_ROOT", tmp_path / "outcomes")
        monkeypatch.setattr(store, "_INSIGHTS_PATH", tmp_path / "insights" / "current.json")

        response = api_client.post("/sales/insights/refresh")
        assert response.status_code == 200
        data = response.json()
        assert "insights_version" in data
        assert data["insights_version"] >= 1

    def test_health_includes_learning_stats(self, api_client):
        response = api_client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert "stage_outcomes_recorded" in data
        assert "deal_outcomes_recorded" in data
        assert "insights_available" in data
