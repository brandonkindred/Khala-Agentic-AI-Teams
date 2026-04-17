"""Tests for the AI Sales Team pod — including outcome tracking and learning loop.

The strands SDK is a hard dependency. Tests require it to be installed.
"""

from __future__ import annotations

import json
from typing import Dict

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
    ProspectDossier,
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
        id="prs_testprospect",
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
def sample_dossier(sample_prospect: Prospect) -> ProspectDossier:
    return ProspectDossier(
        dossier_id="dsr_testdossier1",
        prospect_id=sample_prospect.id,
        full_name=sample_prospect.contact_name or "Jane Smith",
        current_title=sample_prospect.contact_title or "VP of Sales",
        current_company=sample_prospect.company_name,
        linkedin_url=sample_prospect.linkedin_url,
        executive_summary=(
            "Jane runs sales at Acme Corp, a Series-B SaaS company. She has spoken "
            "publicly about ramping AE teams and improving pipeline visibility."
        ),
        trigger_events=[
            "Acme Corp announced Series B funding ($40M)",
            "Hiring 10 AEs per LinkedIn headcount",
        ],
        conversation_hooks=[
            "Recent QCon talk on ramping AE teams",
            "Series B funding → need for pipeline scale",
        ],
        sources=[
            "https://techcrunch.com/2026/acme-series-b",
            "https://qcon.example.com/2025/talks/jane-smith-ramp-ae",
        ],
        confidence=0.82,
    )


@pytest.fixture()
def low_confidence_dossier(sample_prospect: Prospect) -> ProspectDossier:
    return ProspectDossier(
        dossier_id="dsr_lowconf",
        prospect_id=sample_prospect.id,
        full_name=sample_prospect.contact_name or "Jane Smith",
        current_title=sample_prospect.contact_title or "VP of Sales",
        current_company=sample_prospect.company_name,
        confidence=0.35,
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
        # Upper bound was raised from 20 to 100 to support deep-research runs.
        # 100 should be accepted; 101 should fail.
        SalesPipelineRequest(
            product_name="X",
            value_proposition="Y",
            icp=sample_icp,
            max_prospects=100,
        )
        with pytest.raises(Exception):
            SalesPipelineRequest(
                product_name="X",
                value_proposition="Y",
                icp=sample_icp,
                max_prospects=101,
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

    def test_outreach_returns_parseable_json(self, orchestrator, sample_prospect, sample_dossier):
        raw = orchestrator.outreach.generate_sequence(
            sample_prospect.model_dump_json(),
            sample_dossier,
            "TestProduct",
            "We help X",
            "",
            "",
        )
        data = json.loads(raw)
        assert "variants" in data
        assert isinstance(data["variants"], list)
        assert data["variants"], "expected at least one variant"
        variant = data["variants"][0]
        assert "angle" in variant
        assert "email_sequence" in variant
        assert "personalization_grade" in variant

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

    def test_outreach_only(self, orchestrator, sample_prospect, sample_dossier):
        sequences = orchestrator.outreach_only(
            [sample_prospect],
            {sample_prospect.id: sample_dossier},
            "TestProduct",
            "We help X",
            [],
            "",
        )
        assert len(sequences) == 1
        assert sequences[0].prospect.company_name == "Acme Corp"
        assert sequences[0].dossier_id == sample_dossier.dossier_id
        assert sequences[0].dossier_confidence == sample_dossier.confidence
        assert sequences[0].variants, "expected at least one variant"

    def test_outreach_only_skips_prospects_without_dossier(self, orchestrator, sample_prospect):
        sequences = orchestrator.outreach_only(
            [sample_prospect], {}, "TestProduct", "We help X", [], ""
        )
        assert sequences == []

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
# ---------------------------------------------------------------------------
# Dossier rendering (agents._render_dossier_for_prompt)
# ---------------------------------------------------------------------------


class TestDossierRendering:
    def test_renders_full_dossier(self, sample_dossier):
        from sales_team.agents import _render_dossier_for_prompt

        block = _render_dossier_for_prompt(sample_dossier)
        assert "Prospect Dossier" in block
        assert f"confidence: {sample_dossier.confidence:.2f}" in block
        assert sample_dossier.full_name in block
        assert sample_dossier.executive_summary in block
        # All trigger events rendered
        for ev in sample_dossier.trigger_events:
            assert ev in block
        # Sources section present so the model knows which URLs it may cite
        assert "Sources" in block
        for url in sample_dossier.sources:
            assert url in block

    def test_omits_empty_sections(self, low_confidence_dossier):
        from sales_team.agents import _render_dossier_for_prompt

        block = _render_dossier_for_prompt(low_confidence_dossier)
        # Identity + confidence still appear
        assert "Identity" in block
        assert "confidence: 0.35" in block
        # But none of the empty-list sections
        assert "Trigger Events" not in block
        assert "Publications" not in block
        assert "Conversation Hooks" not in block
        assert "Sources" not in block

    def test_truncates_long_lists(self, sample_prospect):
        from sales_team.agents import _DOSSIER_LIST_TOP_K, _render_dossier_for_prompt
        from sales_team.models import ProspectDossier

        big = ProspectDossier(
            prospect_id=sample_prospect.id,
            full_name="Jane",
            current_title="VP",
            current_company="Acme",
            trigger_events=[f"trigger_{i}" for i in range(20)],
            confidence=0.9,
        )
        block = _render_dossier_for_prompt(big)
        # Only the first _DOSSIER_LIST_TOP_K triggers appear
        for i in range(_DOSSIER_LIST_TOP_K):
            assert f"trigger_{i}" in block
        assert f"trigger_{_DOSSIER_LIST_TOP_K}" not in block


# ---------------------------------------------------------------------------
# Outreach parser + confidence gate + citation verifier
# ---------------------------------------------------------------------------


class TestOutreachParser:
    def test_high_confidence_keeps_grounded_variant(self, sample_prospect, sample_dossier):
        from sales_team.orchestrator import _outreach_from_json

        raw = json.dumps(
            {
                "variants": [
                    {
                        "angle": "trigger_event",
                        "email_sequence": [
                            {
                                "day": 1,
                                "subject_line": "Quick note on Acme's Series B",
                                "body": "Congrats on the Series B.",
                                "personalization_tokens": ["first_name"],
                                "call_to_action": "Open to 15 min next week?",
                                "evidence_citations": [
                                    {
                                        "claim": "Acme's Series B funding",
                                        "dossier_field": "trigger_events[0]",
                                        "source_url": sample_dossier.sources[0],
                                        "strength": "strong",
                                    }
                                ],
                            }
                        ],
                        "call_script": "...",
                        "linkedin_message": "...",
                        "rationale": "trigger event is strongest signal",
                        "personalization_grade": "high",
                    }
                ]
            }
        )
        seq = _outreach_from_json(raw, sample_prospect, sample_dossier)
        assert seq is not None
        assert len(seq.variants) == 1
        v = seq.variants[0]
        assert v.angle == "trigger_event"
        assert v.personalization_grade == "high"
        assert v.email_sequence[0].evidence_citations[0].source_url == sample_dossier.sources[0]

    def test_unverified_url_is_stripped_and_grade_downgraded(self, sample_prospect, sample_dossier):
        from sales_team.orchestrator import _outreach_from_json

        raw = json.dumps(
            {
                "variants": [
                    {
                        "angle": "trigger_event",
                        "email_sequence": [
                            {
                                "day": 1,
                                "subject_line": "Series B",
                                "body": "Congrats!",
                                "evidence_citations": [
                                    {
                                        "claim": "made up",
                                        "dossier_field": "trigger_events[0]",
                                        "source_url": "https://evil.example.com/fake",
                                        "strength": "strong",
                                    },
                                    {
                                        "claim": "real",
                                        "dossier_field": "trigger_events[0]",
                                        "source_url": sample_dossier.sources[0],
                                        "strength": "strong",
                                    },
                                ],
                            }
                        ],
                        "personalization_grade": "high",
                    }
                ]
            }
        )
        seq = _outreach_from_json(raw, sample_prospect, sample_dossier)
        assert seq is not None
        v = seq.variants[0]
        # Unverified URL stripped, verified one kept
        urls = [c.source_url for c in v.email_sequence[0].evidence_citations]
        assert "https://evil.example.com/fake" not in urls
        assert sample_dossier.sources[0] in urls
        # Grade downgraded because a citation was stripped
        assert v.personalization_grade == "low"

    def test_non_fallback_with_no_citations_forced_to_fallback(
        self, sample_prospect, sample_dossier
    ):
        from sales_team.orchestrator import _outreach_from_json

        raw = json.dumps(
            {
                "variants": [
                    {
                        "angle": "trigger_event",
                        "email_sequence": [
                            {
                                "day": 1,
                                "subject_line": "No citations",
                                "body": "This pretends to be personalized but isn't.",
                                "evidence_citations": [],
                            }
                        ],
                        "personalization_grade": "high",
                    }
                ]
            }
        )
        seq = _outreach_from_json(raw, sample_prospect, sample_dossier)
        assert seq is not None
        assert seq.variants[0].personalization_grade == "fallback"

    def test_low_confidence_collapses_to_company_soft_opener(
        self, sample_prospect, low_confidence_dossier
    ):
        from sales_team.orchestrator import _outreach_from_json

        raw = json.dumps(
            {
                "variants": [
                    {
                        "angle": "trigger_event",
                        "email_sequence": [
                            {
                                "day": 1,
                                "subject_line": "Should be overridden",
                                "body": "Personal claims the dossier doesn't support.",
                                "evidence_citations": [],
                            }
                        ],
                        "personalization_grade": "high",
                    },
                    {
                        "angle": "thought_leadership",
                        "email_sequence": [],
                        "personalization_grade": "medium",
                    },
                ]
            }
        )
        seq = _outreach_from_json(raw, sample_prospect, low_confidence_dossier)
        assert seq is not None
        # All non-fallback angles dropped; a synthesized fallback remains.
        assert all(v.angle == "company_soft_opener" for v in seq.variants)
        assert all(v.personalization_grade == "fallback" for v in seq.variants)

    def test_empty_variants_yields_fallback(self, sample_prospect, sample_dossier):
        from sales_team.orchestrator import _outreach_from_json

        raw = json.dumps({"variants": []})
        seq = _outreach_from_json(raw, sample_prospect, sample_dossier)
        assert seq is not None
        assert len(seq.variants) == 1
        assert seq.variants[0].angle == "company_soft_opener"
        assert seq.variants[0].personalization_grade == "fallback"


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
        # When no dossier exists for the prospect, the endpoint returns an
        # empty sequences list plus the prospect id in skipped_prospect_ids —
        # rather than 500ing or silently fabricating personalization.
        assert "sequences" in data
        assert "skipped_prospect_ids" in data
        assert data["count"] == len(data["sequences"])
        if not data["sequences"]:
            assert sample_prospect.id in data["skipped_prospect_ids"]

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


# ---------------------------------------------------------------------------
# Deep-research prospecting tests
# ---------------------------------------------------------------------------


from sales_team.models import (  # noqa: E402
    DeepResearchRequest,
    DeepResearchResult,
)
from sales_team.orchestrator import _enforce_cap_and_rank  # noqa: E402


class TestDeepResearchModels:
    def test_prospect_dossier_round_trip(self):
        dossier = ProspectDossier(
            prospect_id="prs_abc123",
            full_name="Jane Smith",
            current_title="VP of Data",
            current_company="Acme Corp",
            executive_summary="Owns the data platform and buys tooling for it.",
            confidence=0.8,
        )
        payload = dossier.model_dump(mode="json")
        restored = ProspectDossier.model_validate(payload)
        assert restored.full_name == "Jane Smith"
        assert restored.current_company == "Acme Corp"
        assert restored.confidence == 0.8

    def test_deep_research_request_bounds(self, sample_icp):
        # 10..100 inclusive are valid.
        DeepResearchRequest(
            product_name="P",
            value_proposition="we help teams do X",
            icp=sample_icp,
            target_prospects=100,
        )
        with pytest.raises(Exception):
            DeepResearchRequest(
                product_name="P",
                value_proposition="we help teams do X",
                icp=sample_icp,
                target_prospects=101,
            )
        with pytest.raises(Exception):
            DeepResearchRequest(
                product_name="P",
                value_proposition="we help teams do X",
                icp=sample_icp,
                target_prospects=9,
            )

    def test_max_per_company_default_is_two(self, sample_icp):
        req = DeepResearchRequest(
            product_name="P",
            value_proposition="we help teams do X",
            icp=sample_icp,
        )
        assert req.max_per_company == 2


class TestCapAndRank:
    def _make(
        self, company: str, name: str, score: float, confidence: float = 0.5
    ) -> tuple[Prospect, float]:
        p = Prospect(
            company_name=company,
            contact_name=name,
            icp_match_score=score,
            linkedin_url=f"https://linkedin.com/in/{name.lower().replace(' ', '-')}",
        )
        return p, confidence

    def test_cap_enforces_max_per_company(self):
        entries: list = []
        for i in range(10):  # 10 prospects at Acme
            entries.append(self._make("Acme", f"Person {i}", 0.9, confidence=0.5))
        for i in range(5):  # 5 at Beta
            entries.append(self._make("Beta", f"Person {i}", 0.8, confidence=0.5))

        result = _enforce_cap_and_rank(entries, max_per_company=2, target_count=100)
        # Acme should contribute at most 2, Beta at most 2 → 4 total.
        assert len(result) == 4
        counts: Dict[str, int] = {}
        for p in result:
            counts[p.company_name] = counts.get(p.company_name, 0) + 1
        assert all(c <= 2 for c in counts.values())

    def test_cap_trims_to_target(self):
        # 25 companies × 5 contacts each = 125 candidates; cap=2 → 50 keepers;
        # target_count=30 should trim to 30.
        entries: list = []
        for c in range(25):
            for i in range(5):
                entries.append(self._make(f"Company{c}", f"Person {c}-{i}", 0.5 + (i * 0.01)))
        result = _enforce_cap_and_rank(entries, max_per_company=2, target_count=30)
        assert len(result) == 30
        counts: Dict[str, int] = {}
        for p in result:
            counts[p.company_name] = counts.get(p.company_name, 0) + 1
        assert all(c <= 2 for c in counts.values())

    def test_cap_dedupes_exact_duplicates(self):
        entry = self._make("Acme", "Jane Smith", 0.9, confidence=0.5)
        result = _enforce_cap_and_rank([entry, entry, entry], max_per_company=2, target_count=10)
        assert len(result) == 1

    def test_rank_preserves_highest_fit_per_company(self):
        low = self._make("Acme", "Low", 0.3, confidence=0.5)
        mid = self._make("Acme", "Mid", 0.6, confidence=0.5)
        hi = self._make("Acme", "High", 0.9, confidence=0.5)
        result = _enforce_cap_and_rank([low, mid, hi], max_per_company=2, target_count=10)
        names = {p.contact_name for p in result}
        # Top 2 of 3 by icp_match_score should survive.
        assert names == {"Mid", "High"}


class TestDeepResearchOrchestrator:
    def test_deep_research_happy_path(self, monkeypatch, sample_icp):
        """End-to-end with all three agents monkeypatched to return fixed JSON."""
        orchestrator = SalesPodOrchestrator()

        # Agent 1: return 60 companies across 30 unique names (2 contacts/company target).
        def _fake_prospect_companies(*_args, **_kwargs):
            companies = []
            for i in range(60):
                companies.append(
                    {
                        "company_name": f"Company {i // 2}",  # repeats — 30 unique
                        "website": f"https://company{i // 2}.example.com",
                        "industry": "SaaS",
                        "company_size_estimate": "200-500",
                        "icp_match_score": 0.7,
                        "research_notes": "matches ICP",
                        "trigger_events": ["recent funding"],
                    }
                )
            return json.dumps(companies)

        # Agent 2: return 2 decision-makers per company.
        counter = {"n": 0}

        def _fake_map_contacts(*_args, **_kwargs):
            n = counter["n"]
            counter["n"] += 1
            return json.dumps(
                [
                    {
                        "contact_name": f"Decision Maker {n}-A",
                        "contact_title": "VP Operations",
                        "linkedin_url": f"https://linkedin.com/in/dm-{n}-a",
                        "contact_email": None,
                        "decision_maker_rationale": "holds budget authority for tooling",
                        "confidence": 0.8,
                    },
                    {
                        "contact_name": f"Decision Maker {n}-B",
                        "contact_title": "Director of Ops",
                        "linkedin_url": f"https://linkedin.com/in/dm-{n}-b",
                        "contact_email": None,
                        "decision_maker_rationale": "champion role on prior vendor deals",
                        "confidence": 0.7,
                    },
                ]
            )

        # Agent 3: return a valid dossier.
        def _fake_build_dossier(prospect_json, *_args, **_kwargs):
            p = json.loads(prospect_json)
            return json.dumps(
                {
                    "prospect_id": p.get("id", "prs_unknown"),
                    "full_name": p.get("contact_name", "Unknown"),
                    "current_title": p.get("contact_title", "Unknown"),
                    "current_company": p.get("company_name", "Unknown"),
                    "executive_summary": "Seasoned operator in SaaS ops.",
                    "career_history": [
                        {"company": "Prev Inc", "title": "Director", "start": "2019", "end": "2022"}
                    ],
                    "publications": [],
                    "topics_of_interest": ["ops efficiency"],
                    "sources": ["https://linkedin.com/in/example"],
                    "confidence": 0.75,
                }
            )

        monkeypatch.setattr(orchestrator.prospector, "prospect_companies", _fake_prospect_companies)
        monkeypatch.setattr(orchestrator.decision_maker_mapper, "map_contacts", _fake_map_contacts)
        monkeypatch.setattr(orchestrator.dossier_builder, "build", _fake_build_dossier)

        request = DeepResearchRequest(
            product_name="AcmeOps",
            value_proposition="Cuts ops review time by 80%",
            icp=sample_icp,
            target_prospects=50,
            max_per_company=2,
        )
        result = orchestrator.deep_research_only(request, persist=False)

        assert isinstance(result, DeepResearchResult)
        assert result.total_prospects == 50
        assert len(result.entries) == 50

        # No company should appear more than twice.
        from collections import Counter

        company_counts = Counter(e.prospect.company_name for e in result.entries)
        assert all(c <= 2 for c in company_counts.values())

        # Every entry must have a dossier reference and a well-formed URL.
        for e in result.entries:
            assert e.dossier_id
            assert e.dossier_url == f"/api/sales/dossiers/{e.dossier_id}"
            assert e.prospect.dossier_id == e.dossier_id
            assert e.prospect.id

    def test_deep_research_handles_empty_company_shortlist(self, monkeypatch, sample_icp):
        orchestrator = SalesPodOrchestrator()
        monkeypatch.setattr(orchestrator.prospector, "prospect_companies", lambda *a, **kw: "[]")
        request = DeepResearchRequest(
            product_name="X",
            value_proposition="we help teams do X",
            icp=sample_icp,
            target_prospects=10,
        )
        result = orchestrator.deep_research_only(request, persist=False)
        assert result.total_prospects == 0
        assert result.entries == []
        assert "No companies" in result.notes


class TestDeepResearchAPI:
    def test_get_dossier_unknown_id_returns_404(self, api_client, monkeypatch):
        """Missing dossiers return 404 — regardless of backing store availability."""
        # Force the store to return None for any id.
        import sales_team.api.main as api_main

        class _FakeStore:
            def get_dossier(self, _id):
                return None

        # The route imports DossierStore lazily; patch the module attribute it imports from.
        import sales_team.dossier_store as ds

        monkeypatch.setattr(ds, "DossierStore", _FakeStore)
        response = api_client.get("/sales/dossiers/dsr_nonexistent")
        assert response.status_code == 404

        # Avoid unused import complaints in case of future refactors.
        assert api_main is not None

    def test_deep_research_endpoint_monkeypatched(self, api_client, monkeypatch, sample_icp):
        import sales_team.api.main as api_main

        fake_result = DeepResearchResult(
            list_id="plst_test",
            product_name="AcmeOps",
            generated_at="2026-04-17T00:00:00+00:00",
            total_prospects=0,
            companies_represented=0,
            entries=[],
            notes="monkeypatched",
        )

        captured: Dict[str, object] = {}

        class _FakeOrch:
            def deep_research_only(self, req, dossier_url_builder=None, persist=True):
                # The route handler must pass a URL builder so that emitted
                # URLs match the actual registered route (including mount prefix).
                captured["builder"] = dossier_url_builder
                captured["req"] = req
                captured["persist"] = persist
                return fake_result

        monkeypatch.setattr(api_main, "SalesPodOrchestrator", _FakeOrch)

        response = api_client.post(
            "/sales/prospect/deep-research",
            json={
                "product_name": "AcmeOps",
                "value_proposition": "Cuts ops review time by 80%",
                "icp": sample_icp.model_dump(),
                "target_prospects": 10,
                "max_per_company": 2,
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body["list_id"] == "plst_test"
        assert body["total_prospects"] == 0

        # The handler must have passed a URL builder that resolves against
        # the app's actual route ("get_dossier"). In the TestClient, routes
        # are registered at /sales/..., so url_for includes that path.
        builder = captured["builder"]
        assert callable(builder)
        built = builder("dsr_abc123")
        assert "/sales/dossiers/dsr_abc123" in built

    def test_get_dossier_runtime_store_failure_returns_503(self, api_client, monkeypatch):
        """Runtime failures from the store (not just import failures) map to 503."""
        import sales_team.dossier_store as ds

        class _ExplodingStore:
            def get_dossier(self, _id):
                raise RuntimeError("postgres unreachable")

        monkeypatch.setattr(ds, "DossierStore", _ExplodingStore)
        response = api_client.get("/sales/dossiers/dsr_anything")
        assert response.status_code == 503
        assert "unavailable" in response.json()["detail"].lower()

    def test_list_prospect_lists_runtime_store_failure_returns_503(self, api_client, monkeypatch):
        import sales_team.dossier_store as ds

        class _ExplodingStore:
            def list_prospect_lists(self, limit: int = 50):
                raise RuntimeError("postgres unreachable")

        monkeypatch.setattr(ds, "DossierStore", _ExplodingStore)
        response = api_client.get("/sales/prospect-lists")
        assert response.status_code == 503
