"""Tests for the AI Sales Team pod after the llm_service migration.

These tests drive each agent with a ``CannedLLMClient`` that returns
pre-programmed structured responses, so the suite needs neither Strands nor
a live LLM provider on the path.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from llm_service.interface import LLMClient
from sales_team.agents import (
    CloserAgent,
    DecisionMakerMapperAgent,
    DiscoveryAgent,
    DossierBuilderAgent,
    LeadQualifierAgent,
    NurtureAgent,
    OutreachAgent,
    ProposalAgent,
    ProspectorAgent,
    SalesCoachAgent,
)
from sales_team.learning_engine import LearningEngine
from sales_team.llm import SALES_ROLES, get_sales_llm_client, sales_agent_key
from sales_team.models import (
    PERSONALIZATION_CONFIDENCE_THRESHOLD,
    CloseType,
    ClosingStrategy,
    ClosingStrategyBody,
    DealOutcome,
    DealResult,
    DecisionMakerList,
    DiscoveryPlan,
    DiscoveryPlanBody,
    EmailTouch,
    EvidenceCitation,
    IdealCustomerProfile,
    LearningInsights,
    NurtureSequence,
    NurtureSequenceBody,
    OutcomeResult,
    OutreachSequence,
    OutreachVariant,
    OutreachVariantList,
    PipelineCoachingReport,
    PipelineStage,
    Prospect,
    ProspectDossier,
    ProspectList,
    QualificationScore,
    QualificationScoreBody,
    SalesProposal,
    SalesProposalBody,
    StageOutcome,
)
from sales_team.orchestrator import (
    _build_fallback_variant,
    _enforce_cap_and_rank,
    _wrap_outreach_sequence,
)

# ---------------------------------------------------------------------------
# CannedLLMClient — a tiny, programmable LLMClient for tests
# ---------------------------------------------------------------------------


class CannedLLMClient(LLMClient):
    """LLMClient that returns pre-programmed dicts from a list, in order.

    Each call to ``complete_json`` pops the next response off the queue and
    returns it as a dict. Tests program expected responses in setup, then
    assert on agent output. This gives richer control than the shared
    ``DummyLLMClient`` whose pattern-match fallbacks don't understand sales
    prompts.
    """

    def __init__(self, responses: List[Dict[str, Any]]) -> None:
        self._responses = list(responses)
        self.calls: List[Dict[str, Any]] = []

    def complete_json(
        self,
        prompt: str,
        *,
        temperature: float = 0.0,
        system_prompt: Optional[str] = None,
        tools: Optional[list] = None,
        think: bool = False,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        self.calls.append(
            {"prompt": prompt, "system_prompt": system_prompt, "temperature": temperature}
        )
        if not self._responses:
            raise AssertionError(
                "CannedLLMClient queue exhausted — test programmed fewer responses than calls"
            )
        return self._responses.pop(0)


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
        pain_points=["manual reporting", "long sales cycles"],
        budget_range_usd="$20k–$80k/yr",
    )


@pytest.fixture()
def sample_prospect() -> Prospect:
    return Prospect(
        id="prs_testprospect",
        company_name="Acme Corp",
        website="https://acme.example.com",
        contact_name="Jane Smith",
        contact_title="VP of Sales",
        linkedin_url="https://linkedin.com/in/jane-smith-example",
        company_size_estimate="200–500",
        industry="SaaS",
        icp_match_score=0.85,
        research_notes="Recently raised Series B.",
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
        executive_summary="Jane runs sales at Acme Corp, a Series-B SaaS company.",
        trigger_events=["Acme Corp announced Series B funding ($40M)"],
        conversation_hooks=["Series B funding → need for pipeline scale"],
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
        executive_summary="Light research — only company-level trigger known.",
        trigger_events=["Acme expanding into EMEA"],
        sources=["https://news.example.com/acme-emea"],
        confidence=0.35,
    )


# ---------------------------------------------------------------------------
# llm.py factory
# ---------------------------------------------------------------------------


class TestSalesLlmFactory:
    def test_all_roles_have_canonical_keys(self) -> None:
        for role in SALES_ROLES:
            assert sales_agent_key(role) == f"sales.{role}"

    def test_unknown_role_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown sales role"):
            sales_agent_key("not_a_real_role")

    def test_get_sales_llm_client_returns_llm_client(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LLM_PROVIDER", "dummy")
        client = get_sales_llm_client("prospector")
        assert hasattr(client, "complete_json")


# ---------------------------------------------------------------------------
# Validators promoted from orchestrator glue
# ---------------------------------------------------------------------------


class TestEmailTouchCitationValidator:
    def test_strips_unverified_urls_and_flags_context(self) -> None:
        ctx: Dict[str, Any] = {
            "dossier_source_urls": {"https://allowed.com"},
            "citations_stripped": False,
        }
        touch = EmailTouch.model_validate(
            {
                "day": 1,
                "subject_line": "hi",
                "body": "hi",
                "evidence_citations": [
                    {
                        "claim": "c1",
                        "dossier_field": "publications[0]",
                        "source_url": "https://allowed.com",
                    },
                    {
                        "claim": "c2",
                        "dossier_field": "publications[1]",
                        "source_url": "https://bad.com",
                    },
                ],
            },
            context=ctx,
        )
        assert len(touch.evidence_citations) == 1
        assert touch.evidence_citations[0].source_url == "https://allowed.com"
        assert ctx["citations_stripped"] is True

    def test_no_context_keeps_everything(self) -> None:
        touch = EmailTouch.model_validate(
            {
                "day": 1,
                "subject_line": "s",
                "body": "b",
                "evidence_citations": [
                    {"claim": "c", "dossier_field": "f", "source_url": "https://anywhere.com"}
                ],
            }
        )
        assert len(touch.evidence_citations) == 1


class TestOutreachVariantValidator:
    def test_grade_downgrades_to_low_when_citations_stripped(self) -> None:
        ctx: Dict[str, Any] = {
            "dossier_source_urls": {"https://ok.com"},
            "citations_stripped": False,
        }
        variant = OutreachVariant.model_validate(
            {
                "angle": "trigger_event",
                "email_sequence": [
                    {
                        "day": 1,
                        "subject_line": "s",
                        "body": "b",
                        "evidence_citations": [
                            {
                                "claim": "c",
                                "dossier_field": "trigger_events[0]",
                                "source_url": "https://ok.com",
                            },
                            {
                                "claim": "bad",
                                "dossier_field": "x",
                                "source_url": "https://bad.com",
                            },
                        ],
                    }
                ],
                "personalization_grade": "high",
            },
            context=ctx,
        )
        assert variant.personalization_grade == "low"

    def test_day1_missing_citations_forces_fallback(self) -> None:
        variant = OutreachVariant.model_validate(
            {
                "angle": "trigger_event",
                "email_sequence": [
                    {"day": 1, "subject_line": "s", "body": "b", "evidence_citations": []}
                ],
                "personalization_grade": "high",
            }
        )
        assert variant.personalization_grade == "fallback"

    def test_soft_opener_without_citations_keeps_its_grade(self) -> None:
        variant = OutreachVariant.model_validate(
            {
                "angle": "company_soft_opener",
                "email_sequence": [
                    {"day": 1, "subject_line": "s", "body": "b", "evidence_citations": []}
                ],
                "personalization_grade": "fallback",
            }
        )
        assert variant.personalization_grade == "fallback"


class TestOutreachSequenceConfidenceGate:
    def test_drops_non_soft_opener_when_dossier_confidence_low(
        self, sample_prospect: Prospect
    ) -> None:
        seq = OutreachSequence.model_validate(
            {
                "prospect": sample_prospect.model_dump(),
                "dossier_id": "d1",
                "dossier_confidence": 0.4,
                "variants": [
                    {
                        "angle": "company_soft_opener",
                        "personalization_grade": "fallback",
                        "email_sequence": [
                            {
                                "day": 1,
                                "subject_line": "s",
                                "body": "b",
                                "evidence_citations": [],
                            }
                        ],
                    },
                    {
                        "angle": "trigger_event",
                        "personalization_grade": "fallback",
                        "email_sequence": [
                            {
                                "day": 1,
                                "subject_line": "s",
                                "body": "b",
                                "evidence_citations": [],
                            }
                        ],
                    },
                ],
            }
        )
        assert [v.angle for v in seq.variants] == ["company_soft_opener"]

    def test_keeps_all_variants_when_confidence_meets_threshold(
        self, sample_prospect: Prospect
    ) -> None:
        seq = OutreachSequence.model_validate(
            {
                "prospect": sample_prospect.model_dump(),
                "dossier_id": "d1",
                "dossier_confidence": PERSONALIZATION_CONFIDENCE_THRESHOLD + 0.1,
                "variants": [
                    {
                        "angle": "company_soft_opener",
                        "personalization_grade": "fallback",
                        "email_sequence": [
                            {
                                "day": 1,
                                "subject_line": "s",
                                "body": "b",
                                "evidence_citations": [],
                            }
                        ],
                    },
                    {
                        "angle": "trigger_event",
                        "personalization_grade": "high",
                        "email_sequence": [
                            {
                                "day": 1,
                                "subject_line": "s",
                                "body": "b",
                                "evidence_citations": [
                                    {
                                        "claim": "c",
                                        "dossier_field": "trigger_events[0]",
                                        "source_url": "https://x.com",
                                    }
                                ],
                            }
                        ],
                    },
                ],
            }
        )
        assert len(seq.variants) == 2


# ---------------------------------------------------------------------------
# Individual agents — each driven by CannedLLMClient
# ---------------------------------------------------------------------------


def _prospect_payload(company: str, score: float = 0.8) -> Dict[str, Any]:
    return {
        "company_name": company,
        "icp_match_score": score,
        "industry": "SaaS",
        "research_notes": "fit",
        "trigger_events": ["Series B"],
    }


class TestProspectorAgent:
    def test_prospect_returns_typed_list(self, sample_icp: IdealCustomerProfile) -> None:
        client = CannedLLMClient(
            [{"prospects": [_prospect_payload("Acme"), _prospect_payload("Beta")]}]
        )
        agent = ProspectorAgent(llm_client=client)
        result = agent.prospect(sample_icp.model_dump_json(), "ProductX", "vp", 5, "company_ctx")
        assert isinstance(result, ProspectList)
        assert [p.company_name for p in result.prospects] == ["Acme", "Beta"]
        assert "Sales Development Representative" in client.calls[0]["system_prompt"]

    def test_prospect_companies_uses_same_schema(self, sample_icp: IdealCustomerProfile) -> None:
        client = CannedLLMClient([{"prospects": [_prospect_payload("GlobalCo")]}])
        agent = ProspectorAgent(llm_client=client)
        result = agent.prospect_companies(sample_icp.model_dump_json(), "ProductX", "vp", 3, "ctx")
        assert isinstance(result, ProspectList)
        assert result.prospects[0].company_name == "GlobalCo"


class TestDecisionMakerMapperAgent:
    def test_returns_typed_decision_maker_list(self, sample_icp: IdealCustomerProfile) -> None:
        client = CannedLLMClient(
            [
                {
                    "contacts": [
                        {
                            "contact_name": "Jane Smith",
                            "contact_title": "VP Sales",
                            "linkedin_url": "https://linkedin.com/in/jane",
                            "contact_email": None,
                            "decision_maker_rationale": "Owns the sales budget",
                            "confidence": 0.9,
                        }
                    ]
                }
            ]
        )
        agent = DecisionMakerMapperAgent(llm_client=client)
        company_json = json.dumps(_prospect_payload("Acme"))
        result = agent.map_contacts(company_json, sample_icp.model_dump_json(), "ProductX", "vp")
        assert isinstance(result, DecisionMakerList)
        assert result.contacts[0].contact_name == "Jane Smith"
        assert result.contacts[0].confidence == 0.9


class TestDossierBuilderAgent:
    def test_returns_typed_prospect_dossier(self, sample_prospect: Prospect) -> None:
        client = CannedLLMClient(
            [
                {
                    "prospect_id": sample_prospect.id,
                    "full_name": "Jane Smith",
                    "current_title": "VP Sales",
                    "current_company": sample_prospect.company_name,
                    "executive_summary": "VP of Sales at Acme.",
                    "sources": ["https://a.com"],
                    "confidence": 0.7,
                }
            ]
        )
        agent = DossierBuilderAgent(llm_client=client)
        result = agent.build(sample_prospect.model_dump_json(), "ProductX", "vp")
        assert isinstance(result, ProspectDossier)
        assert result.full_name == "Jane Smith"


class TestOutreachAgent:
    def test_validators_run_with_dossier_context(
        self, sample_prospect: Prospect, sample_dossier: ProspectDossier
    ) -> None:
        client = CannedLLMClient(
            [
                {
                    "variants": [
                        {
                            "angle": "trigger_event",
                            "email_sequence": [
                                {
                                    "day": 1,
                                    "subject_line": "Series B",
                                    "body": "Saw your Series B",
                                    "evidence_citations": [
                                        {
                                            "claim": "Series B",
                                            "dossier_field": "trigger_events[0]",
                                            "source_url": (
                                                "https://techcrunch.com/2026/acme-series-b"
                                            ),
                                        },
                                        {
                                            "claim": "fake",
                                            "dossier_field": "x",
                                            "source_url": "https://not-in-dossier.com",
                                        },
                                    ],
                                }
                            ],
                            "rationale": "strong trigger",
                            "personalization_grade": "high",
                        }
                    ]
                }
            ]
        )
        agent = OutreachAgent(llm_client=client)
        result = agent.generate_sequence(
            sample_prospect.model_dump_json(),
            sample_dossier,
            "ProductX",
            "vp",
            "",
            "",
        )
        assert isinstance(result, OutreachVariantList)
        variant = result.variants[0]
        assert len(variant.email_sequence[0].evidence_citations) == 1
        assert variant.personalization_grade == "low"


class TestLeadQualifierAgent:
    def test_returns_typed_body(self, sample_prospect: Prospect) -> None:
        client = CannedLLMClient(
            [
                {
                    "bant": {"budget": 8, "authority": 9, "need": 7, "timeline": 8},
                    "meddic": {
                        "metrics_identified": True,
                        "economic_buyer_known": True,
                        "decision_criteria_understood": True,
                        "decision_process_mapped": True,
                        "identify_pain": True,
                        "champion_found": True,
                    },
                    "overall_score": 0.85,
                    "value_creation_level": 3,
                    "recommended_action": "advance",
                    "qualification_notes": "Hot lead.",
                }
            ]
        )
        agent = LeadQualifierAgent(llm_client=client)
        body = agent.qualify(sample_prospect.model_dump_json(), "ProductX", "vp", "")
        assert isinstance(body, QualificationScoreBody)
        qs = QualificationScore(prospect=sample_prospect, **body.model_dump())
        assert qs.bant.authority == 9
        assert qs.recommended_action == "advance"


class TestNurtureAgent:
    def test_returns_typed_body(self, sample_prospect: Prospect) -> None:
        client = CannedLLMClient(
            [
                {
                    "duration_days": 90,
                    "touchpoints": [
                        {
                            "day": 7,
                            "channel": "email",
                            "content_type": "case study",
                            "message": "Check this out",
                            "goal": "build trust",
                        }
                    ],
                    "re_engagement_triggers": ["Series C funding"],
                    "content_recommendations": ["Benchmark report"],
                }
            ]
        )
        agent = NurtureAgent(llm_client=client)
        body = agent.build_sequence(sample_prospect.model_dump_json(), "ProductX", "vp", 90)
        assert isinstance(body, NurtureSequenceBody)
        seq = NurtureSequence(prospect=sample_prospect, **body.model_dump())
        assert len(seq.touchpoints) == 1


class TestDiscoveryAgent:
    def test_returns_typed_body(self, sample_prospect: Prospect) -> None:
        client = CannedLLMClient(
            [
                {
                    "spin_questions": {
                        "situation": ["s1"],
                        "problem": ["p1"],
                        "implication": ["i1"],
                        "need_payoff": ["n1"],
                    },
                    "challenger_insight": "Counterintuitive data…",
                    "demo_agenda": ["intro", "demo"],
                    "expected_objections": ["pricing"],
                    "success_criteria_for_call": "EB confirmed",
                }
            ]
        )
        agent = DiscoveryAgent(llm_client=client)
        body = agent.prepare(sample_prospect.model_dump_json(), "{}", "ProductX", "vp")
        assert isinstance(body, DiscoveryPlanBody)
        plan = DiscoveryPlan(prospect=sample_prospect, **body.model_dump())
        assert plan.spin_questions.situation == ["s1"]


class TestProposalAgent:
    def test_returns_typed_body(self, sample_prospect: Prospect) -> None:
        client = CannedLLMClient(
            [
                {
                    "executive_summary": "Proposal.",
                    "situation_analysis": "…",
                    "proposed_solution": "…",
                    "roi_model": {
                        "annual_cost_usd": 25000.0,
                        "estimated_annual_benefit_usd": 70000.0,
                        "payback_months": 6.0,
                        "roi_percentage": 180.0,
                        "assumptions": ["assume a"],
                    },
                    "investment_table": "…",
                    "implementation_timeline": "…",
                    "risk_mitigation": "…",
                    "next_steps": ["sign"],
                    "custom_sections": [],
                }
            ]
        )
        agent = ProposalAgent(llm_client=client)
        body = agent.write(sample_prospect.model_dump_json(), "ProductX", "vp", 25000.0, "", "", "")
        assert isinstance(body, SalesProposalBody)
        proposal = SalesProposal(prospect=sample_prospect, **body.model_dump())
        assert proposal.roi_model.payback_months == 6.0


class TestCloserAgent:
    def test_returns_typed_body(self, sample_prospect: Prospect) -> None:
        client = CannedLLMClient(
            [
                {
                    "recommended_close_technique": "summary",
                    "close_script": "Shall we sign?",
                    "objection_handlers": [
                        {"objection": "price", "response": "ROI", "feel_felt_found": None}
                    ],
                    "urgency_framing": "Q-end",
                    "walk_away_criteria": "no budget",
                    "emotional_intelligence_notes": "analytical",
                }
            ]
        )
        agent = CloserAgent(llm_client=client)
        body = agent.develop_strategy(sample_prospect.model_dump_json(), "{}", "ProductX", "vp")
        assert isinstance(body, ClosingStrategyBody)
        strat = ClosingStrategy(prospect=sample_prospect, **body.model_dump())
        assert strat.recommended_close_technique == CloseType.SUMMARY


class TestSalesCoachAgent:
    def test_returns_full_coaching_report(self, sample_prospect: Prospect) -> None:
        client = CannedLLMClient(
            [
                {
                    "prospects_reviewed": 1,
                    "deal_risk_signals": [
                        {
                            "signal": "single-threaded",
                            "severity": "high",
                            "recommended_action": "multi-thread",
                        }
                    ],
                    "talk_listen_ratio_advice": "43/57",
                    "velocity_insights": "slow qual",
                    "forecast_category": "pipeline",
                    "top_priority_deals": ["Acme"],
                    "recommended_next_actions": ["book EB call"],
                    "coaching_summary": "…",
                }
            ]
        )
        agent = SalesCoachAgent(llm_client=client)
        report = agent.review(
            json.dumps([sample_prospect.model_dump()]), "ProductX", "pipeline ctx"
        )
        assert isinstance(report, PipelineCoachingReport)
        assert report.deal_risk_signals[0].severity == "high"


# ---------------------------------------------------------------------------
# Orchestrator helpers — wrap typed agent outputs + emit fallbacks
# ---------------------------------------------------------------------------


class TestOrchestratorOutreachWrap:
    def test_wrap_emits_fallback_when_low_confidence_drops_everything(
        self, sample_prospect: Prospect, low_confidence_dossier: ProspectDossier
    ) -> None:
        raw = OutreachVariantList(
            variants=[
                OutreachVariant(
                    angle="trigger_event",
                    email_sequence=[
                        EmailTouch(
                            day=1,
                            subject_line="Series B",
                            body="Saw…",
                            evidence_citations=[
                                EvidenceCitation(
                                    claim="c",
                                    dossier_field="trigger_events[0]",
                                    source_url="https://news.example.com/acme-emea",
                                )
                            ],
                        )
                    ],
                    personalization_grade="high",
                )
            ]
        )
        seq = _wrap_outreach_sequence(raw, sample_prospect, low_confidence_dossier)
        assert len(seq.variants) == 1
        assert seq.variants[0].angle == "company_soft_opener"
        assert seq.variants[0].personalization_grade == "fallback"

    def test_wrap_keeps_variants_when_confidence_high(
        self, sample_prospect: Prospect, sample_dossier: ProspectDossier
    ) -> None:
        raw = OutreachVariantList(
            variants=[
                OutreachVariant(
                    angle="trigger_event",
                    email_sequence=[
                        EmailTouch(
                            day=1,
                            subject_line="Series B",
                            body="Saw…",
                            evidence_citations=[
                                EvidenceCitation(
                                    claim="Series B",
                                    dossier_field="trigger_events[0]",
                                    source_url=("https://techcrunch.com/2026/acme-series-b"),
                                )
                            ],
                        )
                    ],
                    personalization_grade="high",
                )
            ]
        )
        seq = _wrap_outreach_sequence(raw, sample_prospect, sample_dossier)
        assert [v.angle for v in seq.variants] == ["trigger_event"]


class TestOrchestratorFallbackVariant:
    def test_fallback_uses_company_name(self, sample_prospect: Prospect) -> None:
        variant = _build_fallback_variant(sample_prospect)
        assert variant.angle == "company_soft_opener"
        assert variant.personalization_grade == "fallback"
        assert sample_prospect.company_name in variant.email_sequence[0].body


class TestOrchestratorCapAndRank:
    def test_cap_enforced_per_company_and_ranked(self) -> None:
        prospects = [
            (Prospect(company_name="Acme", contact_name="A1", icp_match_score=0.9), 0.9),
            (Prospect(company_name="Acme", contact_name="A2", icp_match_score=0.9), 0.8),
            (Prospect(company_name="Acme", contact_name="A3", icp_match_score=0.9), 0.3),
            (Prospect(company_name="Beta", contact_name="B1", icp_match_score=0.5), 0.9),
        ]
        result = _enforce_cap_and_rank(prospects, max_per_company=2, target_count=10)
        acme_names = [p.contact_name for p in result if p.company_name == "Acme"]
        assert len(acme_names) == 2
        assert "A3" not in acme_names


# ---------------------------------------------------------------------------
# LearningEngine
# ---------------------------------------------------------------------------


class TestLearningEngine:
    def test_empty_outcomes_returns_empty_insights_without_llm(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        saved: Dict[str, Any] = {}

        def fake_save(insights: LearningInsights) -> None:
            saved["insights"] = insights

        monkeypatch.setattr("sales_team.learning_engine.save_insights", fake_save)
        monkeypatch.setattr("sales_team.learning_engine.load_current_insights", lambda: None)
        client = CannedLLMClient([])  # empty — must NOT be called
        engine = LearningEngine(llm_client=client)
        result = engine.refresh(stage_outcomes=[], deal_outcomes=[])
        assert result.total_outcomes_analyzed == 0
        assert saved["insights"] is result
        assert client.calls == []

    def test_refresh_builds_insights_from_llm_body(self, monkeypatch: pytest.MonkeyPatch) -> None:
        saved: Dict[str, Any] = {}
        monkeypatch.setattr(
            "sales_team.learning_engine.save_insights",
            lambda insights: saved.setdefault("insights", insights),
        )
        monkeypatch.setattr("sales_team.learning_engine.load_current_insights", lambda: None)

        stage = StageOutcome(
            company_name="Acme",
            stage=PipelineStage.PROSPECTING,
            outcome=OutcomeResult.CONVERTED,
        )
        deal = DealOutcome(
            company_name="Acme",
            final_stage_reached=PipelineStage.CLOSED_WON,
            result=DealResult.WON,
        )
        client = CannedLLMClient(
            [
                {
                    "total_outcomes_analyzed": 2,
                    "win_rate": 0.5,
                    "winning_patterns": ["multi-threaded"],
                    "actionable_recommendations": ["Multi-thread earlier"],
                }
            ]
        )
        engine = LearningEngine(llm_client=client)
        insights = engine.refresh(stage_outcomes=[stage], deal_outcomes=[deal])
        assert insights.total_outcomes_analyzed == 2
        assert insights.win_rate == 0.5
        assert insights.winning_patterns == ["multi-threaded"]
        assert insights.insights_version == 1
        assert insights.generated_at


# ---------------------------------------------------------------------------
# Migration sanity — make sure the old Strands surface is completely gone
# ---------------------------------------------------------------------------


class TestMigrationCompleteness:
    """Guardrails to prevent the old raw-Strands glue from sneaking back in."""

    _SALES_DIR = Path(__file__).resolve().parent.parent

    def _sales_source(self, filename: str) -> str:
        return (self._SALES_DIR / filename).read_text()

    @pytest.mark.parametrize("filename", ["agents.py", "orchestrator.py", "learning_engine.py"])
    def test_no_raw_strands_imports(self, filename: str) -> None:
        src = self._sales_source(filename)
        assert "from strands" not in src, f"{filename} still imports strands"
        assert re.search(r"^import strands", src, re.MULTILINE) is None, (
            f"{filename} still imports strands"
        )
        assert "StrandsAgent" not in src, f"{filename} still references StrandsAgent"

    def test_legacy_parse_helpers_removed(self) -> None:
        src = self._sales_source("orchestrator.py")
        for name in (
            "def _parse_json",
            "def _prospects_from_json",
            "def _decision_makers_from_json",
            "def _dossier_from_json",
            "def _qual_from_json",
            "def _outreach_from_json",
            "def _nurture_from_json",
            "def _discovery_from_json",
            "def _proposal_from_json",
            "def _closing_from_json",
            "def _coaching_from_json",
            "def _verify_citations",
        ):
            assert name not in src, f"orchestrator.py still defines {name}"

    def test_sales_team_uses_llm_service(self) -> None:
        for filename in ("agents.py", "learning_engine.py"):
            src = self._sales_source(filename)
            assert re.search(r"^from llm_service import", src, re.MULTILINE), (
                f"{filename} does not import from llm_service"
            )
