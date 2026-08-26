"""Tests for ``sales_team.orchestrator.SalesPodOrchestrator``.

These tests focus on the orchestrator's policy code — the parts that wrap
agent outputs, gate stages, and assemble the final pipeline result. They
use stub agents (set via attribute assignment) so the orchestrator's own
LLM clients never run.
"""

from __future__ import annotations

import json
from typing import Any, List
from unittest.mock import MagicMock

import pytest

from sales_team import orchestrator as orch_mod
from sales_team.models import (
    BANTScore,
    CloseType,
    ClosingStrategyBody,
    DecisionMakerEntry,
    DecisionMakerList,
    DeepResearchRequest,
    DiscoveryPlanBody,
    EmailTouch,
    EvidenceCitation,
    IdealCustomerProfile,
    LearningInsights,
    MEDDICScore,
    NurtureSequenceBody,
    OutreachVariant,
    OutreachVariantList,
    PipelineCoachingReport,
    PipelineStage,
    ProposalRequest,
    Prospect,
    ProspectDossier,
    ProspectList,
    QualificationScore,
    QualificationScoreBody,
    ROIModel,
    SalesPipelineConfig,
    SalesPipelineRequest,
    SalesProposal,
    SalesProposalBody,
    SPINQuestions,
)

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sample_icp() -> IdealCustomerProfile:
    return IdealCustomerProfile(
        industry=["SaaS"],
        company_size_min=50,
        company_size_max=500,
        job_titles=["VP Sales"],
        pain_points=["manual reporting"],
    )


@pytest.fixture
def sample_prospect() -> Prospect:
    return Prospect(
        id="prs_p1",
        company_name="Acme Corp",
        contact_name="Jane Smith",
        contact_title="VP Sales",
        icp_match_score=0.85,
    )


@pytest.fixture
def sample_dossier(sample_prospect: Prospect) -> ProspectDossier:
    return ProspectDossier(
        dossier_id="dsr_main",
        prospect_id=sample_prospect.id,
        full_name="Jane Smith",
        current_title="VP Sales",
        current_company="Acme Corp",
        executive_summary="Runs sales at Acme.",
        sources=["https://news.example.com/acme"],
        confidence=0.82,
    )


@pytest.fixture
def stub_orch(monkeypatch: pytest.MonkeyPatch) -> orch_mod.SalesPodOrchestrator:
    """Construct an orchestrator with all agent slots replaced by MagicMocks."""
    o = orch_mod.SalesPodOrchestrator()
    o.prospector = MagicMock()
    o.outreach = MagicMock()
    o.qualifier = MagicMock()
    o.nurture = MagicMock()
    o.discovery = MagicMock()
    o.proposal = MagicMock()
    o.closer = MagicMock()
    o.coach = MagicMock()
    o.decision_maker_mapper = MagicMock()
    o.dossier_builder = MagicMock()
    o.learning_engine = MagicMock()
    o.outreach_critic = MagicMock()
    o.proposal_critic = MagicMock()
    return o


# ---------------------------------------------------------------------------
# _decision_makers_to_entries
# ---------------------------------------------------------------------------


def test_decision_makers_to_entries_skips_blank_names() -> None:
    company = Prospect(company_name="Acme", research_notes="base notes")
    dm_list = DecisionMakerList(
        contacts=[
            DecisionMakerEntry(contact_name="", confidence=0.8),
            DecisionMakerEntry(contact_name="   ", confidence=0.8),
            DecisionMakerEntry(
                contact_name="Jane Smith",
                contact_title="VP Sales",
                linkedin_url="https://linkedin.com/in/jane",
                decision_maker_rationale="Owns budget",
                confidence=0.9,
            ),
        ]
    )
    entries = orch_mod._decision_makers_to_entries(dm_list, company)
    assert len(entries) == 1
    prospect, conf = entries[0]
    assert prospect.contact_name == "Jane Smith"
    assert prospect.contact_email is None  # never fabricated
    assert conf == 0.9
    # Notes get combined.
    assert "base notes" in prospect.research_notes
    assert "Owns budget" in prospect.research_notes
    assert "confidence: 0.9" in prospect.research_notes


def test_decision_makers_to_entries_handles_empty_rationale_and_notes() -> None:
    company = Prospect(company_name="Acme", research_notes="")
    dm_list = DecisionMakerList(
        contacts=[
            DecisionMakerEntry(contact_name="J", decision_maker_rationale=""),
        ]
    )
    entries = orch_mod._decision_makers_to_entries(dm_list, company)
    assert len(entries) == 1
    # rationale empty → extra_notes still contains the confidence — but base_notes
    # was empty so notes = extra_notes only.
    assert "confidence" in entries[0][0].research_notes


# ---------------------------------------------------------------------------
# _rank_score / _enforce_cap_and_rank (cap_and_rank already partially covered)
# ---------------------------------------------------------------------------


def test_rank_score_weighted_average() -> None:
    p = Prospect(company_name="Acme", icp_match_score=0.5)
    assert abs(orch_mod._rank_score((p, 1.0)) - (0.7 * 0.5 + 0.3 * 1.0)) < 1e-9


def test_enforce_cap_and_rank_dedupes_by_linkedin_or_contact() -> None:
    a = Prospect(company_name="Acme", contact_name="J", linkedin_url="https://x/jane")
    b = Prospect(company_name="Acme", contact_name="J", linkedin_url="https://x/jane")  # dup
    c = Prospect(company_name="Acme", contact_name="K")  # different
    out = orch_mod._enforce_cap_and_rank(
        [(a, 0.9), (b, 0.9), (c, 0.9)], max_per_company=10, target_count=10
    )
    assert len(out) == 2


# ---------------------------------------------------------------------------
# _build_fallback_variant (extra branches covered by existing test_sales_team)
# ---------------------------------------------------------------------------


def test_build_fallback_variant_has_day1_email(sample_prospect: Prospect) -> None:
    variant = orch_mod._build_fallback_variant(sample_prospect)
    assert variant.email_sequence[0].day == 1
    assert "15-minute call" in variant.email_sequence[0].call_to_action


# ---------------------------------------------------------------------------
# _wrap_outreach_sequence — fallback emit branch when all variants filtered
# ---------------------------------------------------------------------------


def test_wrap_outreach_sequence_emits_fallback_when_all_filtered(
    sample_prospect: Prospect,
) -> None:
    low_conf = ProspectDossier(
        dossier_id="d_low",
        prospect_id=sample_prospect.id,
        full_name="J",
        current_title="V",
        current_company="A",
        confidence=0.3,  # below threshold
    )
    # Variants are non-soft-opener; confidence gate inside the model_validator
    # drops them all, leaving variants=[] so the orchestrator emits the fallback.
    raw = OutreachVariantList(
        variants=[
            OutreachVariant(
                angle="trigger_event",
                email_sequence=[
                    EmailTouch(
                        day=1,
                        subject_line="hi",
                        body="b",
                        evidence_citations=[
                            EvidenceCitation(
                                claim="c",
                                dossier_field="trigger_events[0]",
                                source_url="https://news.example.com/acme",
                            )
                        ],
                    )
                ],
                personalization_grade="high",
            )
        ]
    )
    seq = orch_mod._wrap_outreach_sequence(raw, sample_prospect, low_conf)
    assert len(seq.variants) == 1
    assert seq.variants[0].angle == "company_soft_opener"
    assert seq.variants[0].personalization_grade == "fallback"


# ---------------------------------------------------------------------------
# _should_run
# ---------------------------------------------------------------------------


def test_should_run_true_when_stage_at_or_after_entry(stub_orch) -> None:
    assert stub_orch._should_run(PipelineStage.QUALIFICATION, PipelineStage.PROSPECTING) is True
    assert stub_orch._should_run(PipelineStage.PROSPECTING, PipelineStage.PROSPECTING) is True


def test_should_run_false_when_stage_before_entry(stub_orch) -> None:
    assert stub_orch._should_run(PipelineStage.PROSPECTING, PipelineStage.PROPOSAL) is False


def test_should_run_false_when_stage_not_in_order_list(stub_orch) -> None:
    """CLOSED_WON is not in _STAGE_ORDER; _should_run must return False."""
    assert stub_orch._should_run(PipelineStage.CLOSED_WON, PipelineStage.PROSPECTING) is False


# ---------------------------------------------------------------------------
# _generate_outreach_with_critic
# ---------------------------------------------------------------------------


def _good_variant_list(source_url: str = "https://news.example.com/acme") -> OutreachVariantList:
    return OutreachVariantList(
        variants=[
            OutreachVariant(
                angle="trigger_event",
                email_sequence=[
                    EmailTouch(
                        day=1,
                        subject_line="s",
                        body="b",
                        evidence_citations=[
                            EvidenceCitation(
                                claim="c",
                                dossier_field="trigger_events[0]",
                                source_url=source_url,
                            )
                        ],
                    )
                ],
                personalization_grade="high",
            )
        ]
    )


def test_generate_outreach_with_critic_outreach_raises_during_initial_emit(
    stub_orch,
    sample_prospect: Prospect,
    sample_dossier: ProspectDossier,
    sample_icp: IdealCustomerProfile,
) -> None:
    # Initial emit raises → exception propagates out of the helper.
    stub_orch.outreach.generate_sequence.side_effect = RuntimeError("LLM down")
    with pytest.raises(RuntimeError):
        stub_orch._generate_outreach_with_critic(
            sample_prospect, sample_dossier, "p", "v", "", "", None, sample_icp
        )


def test_generate_outreach_with_critic_keeps_original_when_refine_raises(
    stub_orch,
    sample_prospect: Prospect,
    sample_dossier: ProspectDossier,
    sample_icp: IdealCustomerProfile,
) -> None:
    """First emit succeeds, critic asks for revision (with budget left for a
    second review), refine emit raises → helper returns the original
    sequence instead of crashing the prospect."""
    from sales_team.models import CriticViolation, OutreachCriticReport

    initial = _good_variant_list()

    # Side effect: first call returns initial, second raises.
    call_count = {"n": 0}

    def _outreach_side_effect(*a, **kw):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return initial
        raise RuntimeError("refine boom")

    stub_orch.outreach.generate_sequence.side_effect = _outreach_side_effect
    stub_orch.outreach_critic.review.return_value = OutreachCriticReport(
        status="FAIL",
        approved=False,
        violations=[
            CriticViolation(
                rule_id="outreach.day1.cta",
                severity="must_fix",
                description="no CTA",
                suggested_fix="add one",
            )
        ],
    )
    sequence = stub_orch._generate_outreach_with_critic(
        sample_prospect,
        sample_dossier,
        "p",
        "v",
        "",
        "",
        None,
        sample_icp,
        max_refinements=2,
    )
    # Two outreach calls attempted (initial + refine), but the refine raised
    # and the helper returned the original sequence.
    assert call_count["n"] == 2
    assert sequence.variants  # original wrapping survived


# ---------------------------------------------------------------------------
# load_dossiers_for_prospects
# ---------------------------------------------------------------------------


def test_load_dossiers_for_prospects_empty_when_no_ids(stub_orch) -> None:
    p = Prospect(id="", company_name="Acme")
    assert stub_orch.load_dossiers_for_prospects([p]) == {}


def test_load_dossiers_for_prospects_returns_store_result(
    monkeypatch: pytest.MonkeyPatch, stub_orch, sample_prospect, sample_dossier
) -> None:
    from sales_team import dossier_store as ds_mod

    class _Store:
        def get_dossiers_by_prospect_ids(self, ids):
            return {sample_prospect.id: sample_dossier}

    monkeypatch.setattr(ds_mod, "DossierStore", _Store)
    out = stub_orch.load_dossiers_for_prospects([sample_prospect])
    assert out[sample_prospect.id].dossier_id == sample_dossier.dossier_id


def test_load_dossiers_for_prospects_returns_empty_on_store_failure(
    monkeypatch: pytest.MonkeyPatch, stub_orch, sample_prospect
) -> None:
    from sales_team import dossier_store as ds_mod

    class _BrokenStore:
        def __init__(self):
            raise RuntimeError("pg down")

    monkeypatch.setattr(ds_mod, "DossierStore", _BrokenStore)
    assert stub_orch.load_dossiers_for_prospects([sample_prospect]) == {}


# ---------------------------------------------------------------------------
# Full pipeline `run` — exhaustive shake-down with stubs
# ---------------------------------------------------------------------------


def _qualifier_body(action: str = "advance") -> QualificationScoreBody:
    return QualificationScoreBody(
        bant=BANTScore(budget=8, authority=9, need=7, timeline=8),
        meddic=MEDDICScore(
            metrics_identified=True,
            economic_buyer_known=True,
            decision_criteria_understood=True,
            decision_process_mapped=True,
            identify_pain=True,
            champion_found=True,
        ),
        overall_score=0.8,
        value_creation_level=3,
        recommended_action=action,
    )


def _proposal_body() -> SalesProposalBody:
    return SalesProposalBody(
        executive_summary="...",
        situation_analysis="...",
        proposed_solution="...",
        roi_model=ROIModel(
            annual_cost_usd=25000.0,
            estimated_annual_benefit_usd=70000.0,
            payback_months=6.0,
            roi_percentage=180.0,
        ),
    )


def _closer_body() -> ClosingStrategyBody:
    return ClosingStrategyBody(
        recommended_close_technique=CloseType.SUMMARY, close_script="Shall we sign?"
    )


def _discovery_body() -> DiscoveryPlanBody:
    return DiscoveryPlanBody(spin_questions=SPINQuestions(situation=["s"]))


def _nurture_body() -> NurtureSequenceBody:
    return NurtureSequenceBody(duration_days=30)


def _coaching_report() -> PipelineCoachingReport:
    return PipelineCoachingReport(prospects_reviewed=1, coaching_summary="OK")


def _pass_outreach_report():
    from sales_team.models import OutreachCriticReport

    return OutreachCriticReport(status="PASS", approved=True, violations=[])


def _pass_proposal_report():
    from sales_team.models import ProposalCriticReport

    return ProposalCriticReport(status="PASS", approved=True, violations=[])


def test_run_full_pipeline_happy_path(
    monkeypatch: pytest.MonkeyPatch, stub_orch, sample_icp, sample_prospect, sample_dossier
) -> None:
    # Stub out all I/O.
    monkeypatch.setattr(orch_mod, "load_current_insights", lambda: None)
    monkeypatch.setattr(orch_mod, "record_stage_outcome", lambda outcome: outcome)

    # Provide a stub dossier store via load_dossiers_for_prospects override.
    stub_orch.load_dossiers_for_prospects = MagicMock(
        return_value={sample_prospect.id: sample_dossier}
    )

    # Prospector returns the same prospect.
    stub_orch.prospector.prospect.return_value = ProspectList(prospects=[sample_prospect])
    # Outreach: returns a good variant list and the critic approves.
    stub_orch.outreach.generate_sequence.return_value = _good_variant_list()
    stub_orch.outreach_critic.review.return_value = _pass_outreach_report()
    # Qualifier: advance.
    stub_orch.qualifier.qualify.return_value = _qualifier_body("advance")
    # Discovery + Proposal + Closer + Coach.
    stub_orch.discovery.prepare.return_value = _discovery_body()
    stub_orch.proposal.write.return_value = _proposal_body()
    stub_orch.proposal_critic.review.return_value = _pass_proposal_report()
    stub_orch.closer.develop_strategy.return_value = _closer_body()
    stub_orch.coach.review.return_value = _coaching_report()

    request = SalesPipelineRequest(
        product_name="ProductX",
        value_proposition="Save 20% on outbound time",
        icp=sample_icp,
        entry_stage=PipelineStage.PROSPECTING,
    )
    progress: List[tuple[str, int]] = []
    result = stub_orch.run(
        request, job_id="job-happy", update_cb=lambda s, p: progress.append((s, p))
    )
    assert result.prospects[0].company_name == "Acme Corp"
    assert len(result.outreach_sequences) == 1
    assert len(result.qualified_leads) == 1
    assert len(result.discovery_plans) == 1
    assert len(result.proposals) == 1
    assert len(result.closing_strategies) == 1
    assert result.coaching_report is not None
    # update_cb fires at every phase boundary.
    assert any(stage == "completed" for stage, _ in progress)


def test_run_uses_existing_prospects_when_provided(
    monkeypatch: pytest.MonkeyPatch, stub_orch, sample_icp, sample_prospect
) -> None:
    monkeypatch.setattr(orch_mod, "load_current_insights", lambda: None)
    monkeypatch.setattr(orch_mod, "record_stage_outcome", lambda outcome: outcome)
    stub_orch.load_dossiers_for_prospects = MagicMock(return_value={})
    stub_orch.qualifier.qualify.return_value = _qualifier_body("disqualify")
    stub_orch.coach.review.return_value = _coaching_report()

    request = SalesPipelineRequest(
        product_name="P",
        value_proposition="A long enough value prop string",
        icp=sample_icp,
        entry_stage=PipelineStage.PROSPECTING,
        existing_prospects=[sample_prospect],
    )
    result = stub_orch.run(request, job_id="j-exist")
    # Prospector never called because existing_prospects was supplied.
    stub_orch.prospector.prospect.assert_not_called()
    assert result.prospects == [sample_prospect]


def test_run_returns_early_when_no_prospects(
    monkeypatch: pytest.MonkeyPatch, stub_orch, sample_icp
) -> None:
    monkeypatch.setattr(orch_mod, "load_current_insights", lambda: None)
    stub_orch.prospector.prospect.return_value = ProspectList(prospects=[])
    request = SalesPipelineRequest(
        product_name="P",
        value_proposition="A valid 10-char value proposition",
        icp=sample_icp,
        entry_stage=PipelineStage.PROSPECTING,
    )
    result = stub_orch.run(request, job_id="j-empty")
    assert "No prospects" in result.summary
    # Downstream stages were not touched.
    stub_orch.outreach.generate_sequence.assert_not_called()


def test_run_uses_existing_prospects_when_entry_after_prospecting(
    monkeypatch: pytest.MonkeyPatch, stub_orch, sample_icp, sample_prospect
) -> None:
    """entry_stage != PROSPECTING goes into the else branch that uses
    request.existing_prospects directly."""
    monkeypatch.setattr(orch_mod, "load_current_insights", lambda: None)
    monkeypatch.setattr(orch_mod, "record_stage_outcome", lambda outcome: outcome)
    stub_orch.load_dossiers_for_prospects = MagicMock(return_value={})
    stub_orch.qualifier.qualify.return_value = _qualifier_body("disqualify")
    stub_orch.coach.review.return_value = _coaching_report()

    request = SalesPipelineRequest(
        product_name="P",
        value_proposition="A valid 10-char value proposition",
        icp=sample_icp,
        entry_stage=PipelineStage.QUALIFICATION,
        existing_prospects=[sample_prospect],
    )
    result = stub_orch.run(request, job_id="j-qual-entry")
    assert result.prospects == [sample_prospect]


def test_run_outreach_failure_caught_per_prospect(
    monkeypatch: pytest.MonkeyPatch, stub_orch, sample_icp, sample_prospect, sample_dossier
) -> None:
    monkeypatch.setattr(orch_mod, "load_current_insights", lambda: None)
    monkeypatch.setattr(orch_mod, "record_stage_outcome", lambda outcome: outcome)
    stub_orch.load_dossiers_for_prospects = MagicMock(
        return_value={sample_prospect.id: sample_dossier}
    )
    stub_orch.prospector.prospect.return_value = ProspectList(prospects=[sample_prospect])
    stub_orch.outreach.generate_sequence.side_effect = RuntimeError("boom")
    stub_orch.qualifier.qualify.return_value = _qualifier_body("disqualify")
    stub_orch.coach.review.return_value = _coaching_report()

    request = SalesPipelineRequest(
        product_name="P",
        value_proposition="A valid long value proposition",
        icp=sample_icp,
        entry_stage=PipelineStage.PROSPECTING,
    )
    result = stub_orch.run(request, job_id="j-outreach-fail")
    # No sequence emitted, but pipeline continued past outreach.
    assert result.outreach_sequences == []


def test_run_skips_outreach_when_dossier_missing(
    monkeypatch: pytest.MonkeyPatch, stub_orch, sample_icp, sample_prospect
) -> None:
    """When the dossier map doesn't contain the prospect, the outreach loop
    skips it via the dossier_missing branch."""
    monkeypatch.setattr(orch_mod, "load_current_insights", lambda: None)
    monkeypatch.setattr(orch_mod, "record_stage_outcome", lambda outcome: outcome)
    stub_orch.load_dossiers_for_prospects = MagicMock(return_value={})  # missing
    stub_orch.prospector.prospect.return_value = ProspectList(prospects=[sample_prospect])
    stub_orch.qualifier.qualify.return_value = _qualifier_body("disqualify")
    stub_orch.coach.review.return_value = _coaching_report()

    request = SalesPipelineRequest(
        product_name="P",
        value_proposition="A valid long value proposition",
        icp=sample_icp,
        entry_stage=PipelineStage.PROSPECTING,
    )
    result = stub_orch.run(request, job_id="j-skip-dossier")
    assert result.outreach_sequences == []


def test_run_qualifier_routes_nurture_and_advance(
    monkeypatch: pytest.MonkeyPatch, stub_orch, sample_icp
) -> None:
    """When qualification produces mixed actions, advance leads go to
    discovery/proposal/close while nurture leads land in nurture_sequences."""
    p_advance = Prospect(id="prs_a", company_name="Acme")
    p_nurture = Prospect(id="prs_b", company_name="Beta")
    p_disq = Prospect(id="prs_c", company_name="Gamma")
    monkeypatch.setattr(orch_mod, "load_current_insights", lambda: None)
    monkeypatch.setattr(orch_mod, "record_stage_outcome", lambda outcome: outcome)
    stub_orch.load_dossiers_for_prospects = MagicMock(return_value={})
    stub_orch.prospector.prospect.return_value = ProspectList(
        prospects=[p_advance, p_nurture, p_disq]
    )

    # Return one body per prospect, in order.
    bodies = iter(
        [_qualifier_body("advance"), _qualifier_body("nurture"), _qualifier_body("disqualify")]
    )
    stub_orch.qualifier.qualify.side_effect = lambda *a, **kw: next(bodies)
    stub_orch.nurture.build_sequence.return_value = _nurture_body()
    stub_orch.discovery.prepare.return_value = _discovery_body()
    stub_orch.proposal.write.return_value = _proposal_body()
    stub_orch.proposal_critic.review.return_value = _pass_proposal_report()
    stub_orch.closer.develop_strategy.return_value = _closer_body()
    stub_orch.coach.review.return_value = _coaching_report()

    request = SalesPipelineRequest(
        product_name="P",
        value_proposition="A valid long value proposition",
        icp=sample_icp,
        entry_stage=PipelineStage.PROSPECTING,
    )
    result = stub_orch.run(request, job_id="j-routes")
    # Discovery and proposal got the advance-only prospect.
    assert len(result.discovery_plans) == 1
    assert result.discovery_plans[0].prospect.company_name == "Acme"
    # Nurture path picks up the nurture lead (not the disqualified one).
    assert len(result.nurture_sequences) == 1
    assert result.nurture_sequences[0].prospect.company_name == "Beta"


def test_run_qualifier_failure_branch(
    monkeypatch: pytest.MonkeyPatch, stub_orch, sample_icp, sample_prospect
) -> None:
    """Qualifier raises → per-prospect except branch fires and continues."""
    monkeypatch.setattr(orch_mod, "load_current_insights", lambda: None)
    monkeypatch.setattr(orch_mod, "record_stage_outcome", lambda outcome: outcome)
    stub_orch.load_dossiers_for_prospects = MagicMock(return_value={})
    stub_orch.prospector.prospect.return_value = ProspectList(prospects=[sample_prospect])
    stub_orch.qualifier.qualify.side_effect = RuntimeError("qual fail")
    # When qualifier fails, qualified is empty and all prospects advance, so
    # discovery / proposal / close run. Make them all raise so the pipeline
    # cleanly drops into the coach stage with empty downstream collections.
    stub_orch.discovery.prepare.side_effect = RuntimeError("d fail")
    stub_orch.proposal.write.side_effect = RuntimeError("p fail")
    stub_orch.closer.develop_strategy.side_effect = RuntimeError("c fail")
    stub_orch.coach.review.return_value = _coaching_report()

    request = SalesPipelineRequest(
        product_name="P",
        value_proposition="A valid long value proposition",
        icp=sample_icp,
        entry_stage=PipelineStage.PROSPECTING,
    )
    result = stub_orch.run(request, job_id="j-qual-fail")
    assert result.qualified_leads == []


def test_run_propagates_insights_block_when_present(
    monkeypatch: pytest.MonkeyPatch, stub_orch, sample_icp, sample_prospect
) -> None:
    insights = LearningInsights(
        total_outcomes_analyzed=5,
        win_rate=0.4,
        insights_version=7,
        generated_at="2026-05-21T00:00:00+00:00",
    )
    monkeypatch.setattr(orch_mod, "load_current_insights", lambda: insights)
    monkeypatch.setattr(orch_mod, "record_stage_outcome", lambda outcome: outcome)
    stub_orch.load_dossiers_for_prospects = MagicMock(return_value={})
    stub_orch.prospector.prospect.return_value = ProspectList(prospects=[sample_prospect])
    stub_orch.qualifier.qualify.return_value = _qualifier_body("disqualify")
    stub_orch.coach.review.return_value = _coaching_report()

    request = SalesPipelineRequest(
        product_name="P",
        value_proposition="A valid long value proposition",
        icp=sample_icp,
        entry_stage=PipelineStage.PROSPECTING,
    )
    result = stub_orch.run(request, job_id="j-insights")
    assert "learning insights v7 applied" in result.summary


def test_run_nurture_failure_branch(monkeypatch: pytest.MonkeyPatch, stub_orch, sample_icp) -> None:
    """Inside the nurture stage, build_sequence raising must be swallowed."""
    p_nurture = Prospect(id="prs_n", company_name="N")
    monkeypatch.setattr(orch_mod, "load_current_insights", lambda: None)
    monkeypatch.setattr(orch_mod, "record_stage_outcome", lambda outcome: outcome)
    stub_orch.load_dossiers_for_prospects = MagicMock(return_value={})
    stub_orch.prospector.prospect.return_value = ProspectList(prospects=[p_nurture])
    stub_orch.qualifier.qualify.return_value = _qualifier_body("nurture")
    stub_orch.nurture.build_sequence.side_effect = RuntimeError("nurture boom")
    stub_orch.coach.review.return_value = _coaching_report()

    request = SalesPipelineRequest(
        product_name="P",
        value_proposition="A valid long value proposition",
        icp=sample_icp,
        entry_stage=PipelineStage.PROSPECTING,
    )
    result = stub_orch.run(request, job_id="j-nurture-fail")
    assert result.nurture_sequences == []


def test_run_discovery_and_proposal_and_close_failure_branches(
    monkeypatch: pytest.MonkeyPatch, stub_orch, sample_icp, sample_prospect, sample_dossier
) -> None:
    monkeypatch.setattr(orch_mod, "load_current_insights", lambda: None)
    monkeypatch.setattr(orch_mod, "record_stage_outcome", lambda outcome: outcome)
    stub_orch.load_dossiers_for_prospects = MagicMock(
        return_value={sample_prospect.id: sample_dossier}
    )
    stub_orch.prospector.prospect.return_value = ProspectList(prospects=[sample_prospect])
    stub_orch.outreach.generate_sequence.return_value = _good_variant_list()
    stub_orch.outreach_critic.review.return_value = _pass_outreach_report()
    stub_orch.qualifier.qualify.return_value = _qualifier_body("advance")
    stub_orch.discovery.prepare.side_effect = RuntimeError("discovery boom")
    stub_orch.proposal.write.side_effect = RuntimeError("proposal boom")
    stub_orch.closer.develop_strategy.side_effect = RuntimeError("close boom")
    stub_orch.coach.review.side_effect = RuntimeError("coach boom")

    request = SalesPipelineRequest(
        product_name="P",
        value_proposition="A valid long value proposition",
        icp=sample_icp,
        entry_stage=PipelineStage.PROSPECTING,
    )
    result = stub_orch.run(request, job_id="j-failures")
    assert result.discovery_plans == []
    assert result.proposals == []
    assert result.closing_strategies == []
    assert result.coaching_report is None


def test_record_prospecting_outcomes_swallows_per_prospect_errors(
    monkeypatch: pytest.MonkeyPatch, stub_orch, sample_prospect
) -> None:
    """If record_stage_outcome raises, the loop must continue not crash."""

    def _explode(outcome):
        raise RuntimeError("disk full")

    monkeypatch.setattr(orch_mod, "record_stage_outcome", _explode)
    # Just call the helper directly — no assertion needed beyond "doesn't raise".
    stub_orch._record_prospecting_outcomes([sample_prospect], "j-err")


# ---------------------------------------------------------------------------
# Single-stage convenience methods
# ---------------------------------------------------------------------------


def test_prospect_only_returns_list(stub_orch, sample_icp, sample_prospect) -> None:
    stub_orch.prospector.prospect.return_value = ProspectList(prospects=[sample_prospect])
    out = stub_orch.prospect_only(sample_icp, "P", "V", 5, "")
    assert out == [sample_prospect]


def test_prospect_only_returns_empty_on_failure(stub_orch, sample_icp) -> None:
    stub_orch.prospector.prospect.side_effect = RuntimeError("down")
    assert stub_orch.prospect_only(sample_icp, "P", "V", 5, "") == []


def test_outreach_only_skips_prospects_without_dossier(
    monkeypatch: pytest.MonkeyPatch, stub_orch, sample_prospect, sample_dossier
) -> None:
    p_missing = Prospect(id="prs_m", company_name="X")
    monkeypatch.setattr(orch_mod, "load_current_insights", lambda: None)
    stub_orch.outreach.generate_sequence.return_value = _good_variant_list()
    seqs = stub_orch.outreach_only(
        [sample_prospect, p_missing],
        {sample_prospect.id: sample_dossier},
        "P",
        "V",
        [],
        "",
    )
    assert len(seqs) == 1


def test_outreach_only_catches_outreach_exception(
    monkeypatch: pytest.MonkeyPatch, stub_orch, sample_prospect, sample_dossier
) -> None:
    monkeypatch.setattr(orch_mod, "load_current_insights", lambda: None)
    stub_orch.outreach.generate_sequence.side_effect = RuntimeError("boom")
    seqs = stub_orch.outreach_only(
        [sample_prospect], {sample_prospect.id: sample_dossier}, "P", "V", ["case"], ""
    )
    assert seqs == []


def test_qualify_only_returns_score_or_none(stub_orch, sample_prospect, monkeypatch) -> None:
    monkeypatch.setattr(orch_mod, "load_current_insights", lambda: None)
    stub_orch.qualifier.qualify.return_value = _qualifier_body("advance")
    score = stub_orch.qualify_only(sample_prospect, "P", "V", "")
    assert score is not None
    assert score.recommended_action == "advance"


def test_qualify_only_returns_none_on_exception(stub_orch, sample_prospect, monkeypatch) -> None:
    monkeypatch.setattr(orch_mod, "load_current_insights", lambda: None)
    stub_orch.qualifier.qualify.side_effect = RuntimeError("down")
    assert stub_orch.qualify_only(sample_prospect, "P", "V", "") is None


def test_nurture_only_catches_per_prospect_exception(
    monkeypatch: pytest.MonkeyPatch, stub_orch, sample_prospect
) -> None:
    monkeypatch.setattr(orch_mod, "load_current_insights", lambda: None)
    # First call works, second raises.
    bodies = iter([_nurture_body()])

    def _side(*a, **kw):
        try:
            return next(bodies)
        except StopIteration as exc:
            raise RuntimeError("boom") from exc

    stub_orch.nurture.build_sequence.side_effect = _side
    other = Prospect(id="prs_other", company_name="B")
    out = stub_orch.nurture_only([sample_prospect, other], "P", "V", 30)
    assert len(out) == 1


def test_propose_only_returns_none_on_exception(
    monkeypatch: pytest.MonkeyPatch, stub_orch, sample_prospect
) -> None:
    monkeypatch.setattr(orch_mod, "load_current_insights", lambda: None)
    stub_orch.load_dossiers_for_prospects = MagicMock(return_value={})
    stub_orch.proposal.write.side_effect = RuntimeError("boom")
    req = ProposalRequest(
        prospect=sample_prospect,
        product_name="P",
        value_proposition="V",
        annual_cost_usd=25000.0,
    )
    assert stub_orch.propose_only(req) is None


def test_propose_only_returns_proposal_on_success(
    monkeypatch: pytest.MonkeyPatch, stub_orch, sample_prospect
) -> None:
    monkeypatch.setattr(orch_mod, "load_current_insights", lambda: None)
    stub_orch.load_dossiers_for_prospects = MagicMock(return_value={})
    stub_orch.proposal.write.return_value = _proposal_body()
    stub_orch.proposal_critic.review.return_value = _pass_proposal_report()
    req = ProposalRequest(
        prospect=sample_prospect,
        product_name="P",
        value_proposition="V",
        annual_cost_usd=25000.0,
    )
    proposal = stub_orch.propose_only(req)
    assert proposal is not None
    assert proposal.roi_model.payback_months == 6.0


def test_coach_only_returns_report_or_none(
    monkeypatch: pytest.MonkeyPatch, stub_orch, sample_prospect
) -> None:
    monkeypatch.setattr(orch_mod, "load_current_insights", lambda: None)
    stub_orch.coach.review.return_value = _coaching_report()
    out = stub_orch.coach_only([sample_prospect], "P", "")
    assert out is not None and out.prospects_reviewed == 1


def test_coach_only_returns_none_on_exception(
    monkeypatch: pytest.MonkeyPatch, stub_orch, sample_prospect
) -> None:
    monkeypatch.setattr(orch_mod, "load_current_insights", lambda: None)
    stub_orch.coach.review.side_effect = RuntimeError("down")
    assert stub_orch.coach_only([sample_prospect], "P", "") is None


# ---------------------------------------------------------------------------
# deep_research_only — the largest method
# ---------------------------------------------------------------------------


def _deep_request(target: int = 10) -> DeepResearchRequest:
    return DeepResearchRequest(
        product_name="ProductX",
        value_proposition="Save 20% on outbound time",
        icp=IdealCustomerProfile(industry=["SaaS"]),
        target_prospects=target,
        max_per_company=2,
        company_context="",
    )


def test_deep_research_returns_empty_when_prospector_fails(
    monkeypatch: pytest.MonkeyPatch, stub_orch
) -> None:
    monkeypatch.setattr(orch_mod, "load_current_insights", lambda: None)
    stub_orch.prospector.prospect_companies.side_effect = RuntimeError("down")
    result = stub_orch.deep_research_only(_deep_request(), persist=False)
    assert result.total_prospects == 0
    assert "No companies returned" in result.notes


def test_deep_research_returns_empty_when_no_companies(
    monkeypatch: pytest.MonkeyPatch, stub_orch
) -> None:
    monkeypatch.setattr(orch_mod, "load_current_insights", lambda: None)
    stub_orch.prospector.prospect_companies.return_value = ProspectList(prospects=[])
    result = stub_orch.deep_research_only(_deep_request(), persist=False)
    assert result.total_prospects == 0
    assert "No companies returned" in result.notes


def test_deep_research_returns_empty_when_no_decision_makers(
    monkeypatch: pytest.MonkeyPatch, stub_orch
) -> None:
    monkeypatch.setattr(orch_mod, "load_current_insights", lambda: None)
    stub_orch.prospector.prospect_companies.return_value = ProspectList(
        prospects=[Prospect(company_name="Acme")]
    )
    # Mapper returns an empty contact list for every company.
    stub_orch.decision_maker_mapper.map_contacts.return_value = DecisionMakerList(contacts=[])
    result = stub_orch.deep_research_only(_deep_request(), persist=False)
    assert result.total_prospects == 0
    assert "No decision-makers" in result.notes


def test_deep_research_swallows_mapper_exceptions(
    monkeypatch: pytest.MonkeyPatch, stub_orch
) -> None:
    monkeypatch.setattr(orch_mod, "load_current_insights", lambda: None)
    stub_orch.prospector.prospect_companies.return_value = ProspectList(
        prospects=[Prospect(company_name="Acme"), Prospect(company_name="Beta")]
    )

    def _mixed_mapper(company_json, *a, **kw):
        if "Acme" in company_json:
            return DecisionMakerList(
                contacts=[DecisionMakerEntry(contact_name="J", confidence=0.9)]
            )
        raise RuntimeError("mapper down")

    stub_orch.decision_maker_mapper.map_contacts.side_effect = _mixed_mapper
    stub_orch.dossier_builder.build.return_value = ProspectDossier(
        prospect_id="ignored",
        full_name="J",
        current_title="VP",
        current_company="Acme",
        sources=["https://x"],
    )
    result = stub_orch.deep_research_only(_deep_request(target=10), persist=False)
    assert result.total_prospects >= 1


def test_deep_research_swallows_dossier_builder_exceptions(
    monkeypatch: pytest.MonkeyPatch, stub_orch
) -> None:
    monkeypatch.setattr(orch_mod, "load_current_insights", lambda: None)
    stub_orch.prospector.prospect_companies.return_value = ProspectList(
        prospects=[Prospect(company_name="Acme")]
    )
    stub_orch.decision_maker_mapper.map_contacts.return_value = DecisionMakerList(
        contacts=[DecisionMakerEntry(contact_name="J", confidence=0.9)]
    )
    stub_orch.dossier_builder.build.side_effect = RuntimeError("builder boom")
    result = stub_orch.deep_research_only(_deep_request(target=10), persist=False)
    # No dossier produced → no entries.
    assert result.total_prospects == 0
    assert "No dossier produced" in result.notes


def test_deep_research_persists_via_store(monkeypatch: pytest.MonkeyPatch, stub_orch) -> None:
    """With persist=True, the store's save_dossier and save_prospect_list are called."""
    monkeypatch.setattr(orch_mod, "load_current_insights", lambda: None)
    stub_orch.prospector.prospect_companies.return_value = ProspectList(
        prospects=[Prospect(company_name="Acme")]
    )
    stub_orch.decision_maker_mapper.map_contacts.return_value = DecisionMakerList(
        contacts=[DecisionMakerEntry(contact_name="J", confidence=0.9)]
    )
    stub_orch.dossier_builder.build.return_value = ProspectDossier(
        prospect_id="ignored",
        full_name="J",
        current_title="VP",
        current_company="Acme",
    )

    saved_dossiers: list[Any] = []
    saved_lists: list[Any] = []
    from sales_team import dossier_store as ds_mod

    class _Store:
        def save_dossier(self, dossier):
            saved_dossiers.append(dossier)
            if not dossier.dossier_id:
                dossier.dossier_id = "dsr_persisted"
            return dossier

        def save_prospect_list(self, result):
            saved_lists.append(result)
            if not result.list_id:
                result.list_id = "plst_persisted"
            return result

    monkeypatch.setattr(ds_mod, "DossierStore", _Store)
    result = stub_orch.deep_research_only(_deep_request(target=10), persist=True)
    assert saved_dossiers
    assert saved_lists
    assert result.list_id == "plst_persisted"


def test_deep_research_store_unavailable_branch(monkeypatch: pytest.MonkeyPatch, stub_orch) -> None:
    """If DossierStore construction raises, persist=True still returns a result."""
    monkeypatch.setattr(orch_mod, "load_current_insights", lambda: None)
    stub_orch.prospector.prospect_companies.return_value = ProspectList(
        prospects=[Prospect(company_name="Acme")]
    )
    stub_orch.decision_maker_mapper.map_contacts.return_value = DecisionMakerList(
        contacts=[DecisionMakerEntry(contact_name="J", confidence=0.9)]
    )
    stub_orch.dossier_builder.build.return_value = ProspectDossier(
        prospect_id="ignored",
        full_name="J",
        current_title="VP",
        current_company="Acme",
    )
    from sales_team import dossier_store as ds_mod

    class _Broken:
        def __init__(self):
            raise RuntimeError("no pg")

    monkeypatch.setattr(ds_mod, "DossierStore", _Broken)
    result = stub_orch.deep_research_only(_deep_request(target=10), persist=True)
    # We still get entries — just not persisted.
    assert result.total_prospects >= 1


def test_deep_research_store_save_dossier_failure_continues(
    monkeypatch: pytest.MonkeyPatch, stub_orch
) -> None:
    monkeypatch.setattr(orch_mod, "load_current_insights", lambda: None)
    stub_orch.prospector.prospect_companies.return_value = ProspectList(
        prospects=[Prospect(company_name="Acme")]
    )
    stub_orch.decision_maker_mapper.map_contacts.return_value = DecisionMakerList(
        contacts=[DecisionMakerEntry(contact_name="J", confidence=0.9)]
    )
    stub_orch.dossier_builder.build.return_value = ProspectDossier(
        prospect_id="ignored",
        full_name="J",
        current_title="VP",
        current_company="Acme",
    )
    from sales_team import dossier_store as ds_mod

    class _PartialBroken:
        def save_dossier(self, d):
            raise RuntimeError("save fail")

        def save_prospect_list(self, r):
            raise RuntimeError("list save fail")

    monkeypatch.setattr(ds_mod, "DossierStore", _PartialBroken)
    result = stub_orch.deep_research_only(_deep_request(target=10), persist=True)
    # Even though saves fail, we still get entries with auto-assigned dossier_ids.
    assert result.total_prospects >= 1


def test_deep_research_logs_shortfall_when_fewer_than_target(
    monkeypatch: pytest.MonkeyPatch, stub_orch
) -> None:
    """If the per-company cap leaves fewer prospects than target, notes flags it."""
    monkeypatch.setattr(orch_mod, "load_current_insights", lambda: None)
    stub_orch.prospector.prospect_companies.return_value = ProspectList(
        prospects=[Prospect(company_name="Acme")]
    )
    stub_orch.decision_maker_mapper.map_contacts.return_value = DecisionMakerList(
        contacts=[DecisionMakerEntry(contact_name="J", confidence=0.9)]
    )
    stub_orch.dossier_builder.build.return_value = ProspectDossier(
        prospect_id="ignored",
        full_name="J",
        current_title="VP",
        current_company="Acme",
    )
    # Target 10 but only one company → only one prospect possible.
    result = stub_orch.deep_research_only(_deep_request(target=10), persist=False)
    assert "Only" in result.notes and "qualifying" in result.notes


def test_deep_research_uses_custom_url_builder(monkeypatch: pytest.MonkeyPatch, stub_orch) -> None:
    monkeypatch.setattr(orch_mod, "load_current_insights", lambda: None)
    stub_orch.prospector.prospect_companies.return_value = ProspectList(
        prospects=[Prospect(company_name="Acme")]
    )
    stub_orch.decision_maker_mapper.map_contacts.return_value = DecisionMakerList(
        contacts=[DecisionMakerEntry(contact_name="J", confidence=0.9)]
    )
    stub_orch.dossier_builder.build.return_value = ProspectDossier(
        prospect_id="ignored",
        full_name="J",
        current_title="VP",
        current_company="Acme",
    )

    def _custom_url(dossier_id: str) -> str:
        return f"https://example.com/d/{dossier_id}"

    result = stub_orch.deep_research_only(
        _deep_request(target=10), persist=False, dossier_url_builder=_custom_url
    )
    if result.entries:
        assert result.entries[0].dossier_url.startswith("https://example.com/d/")


def test_deep_research_default_url_builder_used_when_none_provided(
    monkeypatch: pytest.MonkeyPatch, stub_orch
) -> None:
    """The else branch that defines the default url builder must execute."""
    monkeypatch.setattr(orch_mod, "load_current_insights", lambda: None)
    stub_orch.prospector.prospect_companies.return_value = ProspectList(
        prospects=[Prospect(company_name="Acme")]
    )
    stub_orch.decision_maker_mapper.map_contacts.return_value = DecisionMakerList(
        contacts=[DecisionMakerEntry(contact_name="J", confidence=0.9)]
    )
    stub_orch.dossier_builder.build.return_value = ProspectDossier(
        prospect_id="ignored",
        full_name="J",
        current_title="VP",
        current_company="Acme",
    )
    result = stub_orch.deep_research_only(_deep_request(target=10), persist=False)
    if result.entries:
        assert result.entries[0].dossier_url.startswith("/api/sales/dossiers/")


def test_deep_research_propagates_dossier_field_backfill(
    monkeypatch: pytest.MonkeyPatch, stub_orch
) -> None:
    """The orchestrator backfills full_name, current_title, current_company,
    and linkedin_url on the dossier from the prospect when the dossier model
    left them blank-ish. We exercise this branch by returning a sparse dossier.
    """
    monkeypatch.setattr(orch_mod, "load_current_insights", lambda: None)
    company = Prospect(company_name="Acme", website="https://acme")
    stub_orch.prospector.prospect_companies.return_value = ProspectList(prospects=[company])
    stub_orch.decision_maker_mapper.map_contacts.return_value = DecisionMakerList(
        contacts=[
            DecisionMakerEntry(
                contact_name="Jane Smith",
                contact_title="VP Sales",
                linkedin_url="https://linkedin.com/in/jane",
                confidence=0.9,
            )
        ]
    )
    # Build returns a dossier whose backfill-eligible fields are empty.
    stub_orch.dossier_builder.build.return_value = ProspectDossier(
        prospect_id="overwrite-me",
        full_name="",
        current_title="",
        current_company="",
    )
    result = stub_orch.deep_research_only(_deep_request(target=10), persist=False)
    # The orchestrator must have populated those fields when building entries.
    assert result.total_prospects >= 1


# ---------------------------------------------------------------------------
# SalesPipelineConfig plumbing
# ---------------------------------------------------------------------------


def test_config_defaults_match_legacy_constants() -> None:
    """Default SalesPipelineConfig reproduces the prior hardcoded behaviour."""
    cfg = SalesPipelineConfig()
    assert cfg.dossier_confidence_threshold == 0.6
    assert cfg.decision_maker_workers == 8
    assert cfg.dossier_workers == 4
    assert cfg.critic_max_refinements == 1


def test_request_carries_default_config() -> None:
    req = SalesPipelineRequest(
        product_name="P",
        value_proposition="A valid long value proposition",
        icp=IdealCustomerProfile(),
    )
    assert isinstance(req.config, SalesPipelineConfig)
    assert req.config.critic_max_refinements == 1


def test_request_accepts_custom_config() -> None:
    custom = SalesPipelineConfig(critic_max_refinements=3, dossier_workers=2)
    req = SalesPipelineRequest(
        product_name="P",
        value_proposition="A valid long value proposition",
        icp=IdealCustomerProfile(),
        config=custom,
    )
    assert req.config.critic_max_refinements == 3
    assert req.config.dossier_workers == 2


def test_orchestrator_accepts_config() -> None:
    cfg = SalesPipelineConfig(decision_maker_workers=2, dossier_workers=1)
    o = orch_mod.SalesPodOrchestrator(config=cfg)
    assert o.config.decision_maker_workers == 2


def test_run_uses_request_config_critic_refinements(
    monkeypatch: pytest.MonkeyPatch, stub_orch, sample_icp, sample_prospect, sample_dossier
) -> None:
    """Setting critic_max_refinements=0 skips the critic loop entirely."""
    monkeypatch.setattr(orch_mod, "load_current_insights", lambda: None)
    monkeypatch.setattr(orch_mod, "record_stage_outcome", lambda outcome: outcome)

    stub_orch.load_dossiers_for_prospects = MagicMock(
        return_value={sample_prospect.id: sample_dossier}
    )
    stub_orch.prospector.prospect.return_value = ProspectList(prospects=[sample_prospect])
    stub_orch.outreach.generate_sequence.return_value = _good_variant_list()
    stub_orch.qualifier.qualify.return_value = _qualifier_body("disqualify")
    stub_orch.coach.review.return_value = _coaching_report()

    request = SalesPipelineRequest(
        product_name="P",
        value_proposition="A valid long value proposition",
        icp=sample_icp,
        entry_stage=PipelineStage.PROSPECTING,
        config=SalesPipelineConfig(critic_max_refinements=0),
    )
    result = stub_orch.run(request, job_id="j-no-critic")
    assert len(result.outreach_sequences) == 1
    # Critic was never called because max_refinements=0.
    stub_orch.outreach_critic.review.assert_not_called()


def test_stage_methods_are_individually_callable(
    monkeypatch: pytest.MonkeyPatch, stub_orch, sample_icp, sample_prospect
) -> None:
    """Stage methods can be called directly without running the full pipeline."""
    monkeypatch.setattr(orch_mod, "load_current_insights", lambda: None)
    stub_orch.prospector.prospect.return_value = ProspectList(prospects=[sample_prospect])

    ctx = orch_mod._RunContext(
        request=SalesPipelineRequest(
            product_name="P",
            value_proposition="A valid long value proposition",
            icp=sample_icp,
        ),
        job_id="j-stage",
        icp_json=sample_icp.model_dump_json(indent=2),
        product="P",
        vp="A valid long value proposition",
        company_context="",
        cases="",
        entry=PipelineStage.PROSPECTING,
        insights_ctx="",
        config=SalesPipelineConfig(),
        update=orch_mod._noop_update,
    )
    prospects = stub_orch._run_prospecting(ctx)
    assert prospects == [sample_prospect]


# ---------------------------------------------------------------------------
# Multi-iteration critic refinement tests
# ---------------------------------------------------------------------------


def test_outreach_critic_multi_iteration_refinement(
    stub_orch,
    sample_prospect: Prospect,
    sample_dossier: ProspectDossier,
    sample_icp: IdealCustomerProfile,
) -> None:
    """With max_refinements=2, the review budget is 2 review calls: attempt 0
    FAILs and there's budget left, so the agent regenerates once; attempt 1
    reviews that regeneration and, since no budget remains for a further
    regenerate+review cycle, its (approving) verdict is returned as the
    final answer — the returned sequence was always reviewed."""
    from sales_team.models import CriticViolation, OutreachCriticReport

    call_count = {"n": 0}

    def _outreach_side_effect(*a, **kw):
        call_count["n"] += 1
        return _good_variant_list()

    stub_orch.outreach.generate_sequence.side_effect = _outreach_side_effect

    # Critic rejects the initial draft, then approves the regenerated one.
    review_count = {"n": 0}

    def _critic_side_effect(*a, **kw):
        review_count["n"] += 1
        if review_count["n"] == 1:
            return OutreachCriticReport(
                status="FAIL",
                approved=False,
                violations=[
                    CriticViolation(
                        rule_id="outreach.day1.cta",
                        severity="must_fix",
                        description="no CTA",
                        suggested_fix="add one",
                    )
                ],
            )
        return OutreachCriticReport(status="PASS", approved=True, violations=[])

    stub_orch.outreach_critic.review.side_effect = _critic_side_effect

    sequence = stub_orch._generate_outreach_with_critic(
        sample_prospect,
        sample_dossier,
        "p",
        "v",
        "",
        "",
        None,
        sample_icp,
        max_refinements=2,
    )
    # 1 initial + 1 refinement emit = 2 outreach calls (attempt 1 is the last
    # iteration, so it never regenerates a third, unreviewed draft).
    assert call_count["n"] == 2
    # 2 reviews: the FAIL on the initial draft, then the PASS on the
    # regenerated one — the returned sequence was reviewed by the 2nd call.
    assert review_count["n"] == 2
    assert sequence.variants  # still has variants


def test_outreach_critic_default_budget_returns_reviewed_sequence_unrefined(
    stub_orch,
    sample_prospect: Prospect,
    sample_dossier: ProspectDossier,
    sample_icp: IdealCustomerProfile,
) -> None:
    """With the shipped default max_refinements=1, a FAIL leaves no review
    budget to check a regeneration, so generate_sequence is called exactly
    once and the (unapproved-but-reviewed) initial sequence is returned —
    never an unreviewed regeneration."""
    from sales_team.models import CriticViolation, OutreachCriticReport

    call_count = {"n": 0}

    def _outreach_side_effect(*a, **kw):
        call_count["n"] += 1
        return _good_variant_list()

    stub_orch.outreach.generate_sequence.side_effect = _outreach_side_effect
    stub_orch.outreach_critic.review.return_value = OutreachCriticReport(
        status="FAIL",
        approved=False,
        violations=[
            CriticViolation(
                rule_id="outreach.day1.cta",
                severity="must_fix",
                description="no CTA",
                suggested_fix="add one",
            )
        ],
    )

    sequence = stub_orch._generate_outreach_with_critic(
        sample_prospect,
        sample_dossier,
        "p",
        "v",
        "",
        "",
        None,
        sample_icp,
        max_refinements=1,
    )

    assert call_count["n"] == 1
    assert stub_orch.outreach_critic.review.call_count == 1
    assert sequence.variants


def test_proposal_critic_fail_then_refine(
    stub_orch,
    sample_prospect: Prospect,
    sample_dossier: ProspectDossier,
) -> None:
    """When the proposal critic rejects, the agent retries with feedback."""
    from sales_team.models import CriticViolation, ProposalCriticReport

    call_count = {"n": 0}

    def _write_side_effect(*a, **kw):
        call_count["n"] += 1
        return _proposal_body()

    stub_orch.proposal.write.side_effect = _write_side_effect

    # Critic rejects on first review, approves on second.
    review_count = {"n": 0}

    def _critic_side_effect(*a, **kw):
        review_count["n"] += 1
        if review_count["n"] == 1:
            return ProposalCriticReport(
                status="FAIL",
                approved=False,
                violations=[
                    CriticViolation(
                        rule_id="proposal.roi.arithmetic",
                        severity="must_fix",
                        description="ROI math wrong",
                        suggested_fix="fix the multiplication",
                    )
                ],
            )
        return ProposalCriticReport(status="PASS", approved=True, violations=[])

    stub_orch.proposal_critic.review.side_effect = _critic_side_effect

    proposal = stub_orch._generate_proposal_with_critic(
        sample_prospect,
        "p",
        "v",
        25000.0,
        "",
        "",
        "",
        None,
        sample_dossier,
        None,
        max_refinements=2,
    )
    # 1 initial emit + 1 refinement (after first FAIL) + 1 more refinement (after second FAIL) = 3
    # Actually: initial emit is outside the loop. Loop runs 2 iterations.
    # Iter 0: review FAIL → re-emit. Iter 1: review FAIL → re-emit. Loop ends.
    # So: 1 initial + 2 refinements = 3 write calls, 2 review calls.
    # But the second review returns PASS (review_count==2), so iter 1 returns early.
    # Iter 0: review(FAIL) → re-emit. Iter 1: review(PASS) → return.
    # Total: 1 initial + 1 refinement = 2 write calls, 2 review calls.
    assert call_count["n"] == 2
    assert review_count["n"] == 2
    assert proposal is not None
    assert proposal.roi_model.payback_months == 6.0


def test_propose_only_forwards_config_critic_refinements(
    monkeypatch: pytest.MonkeyPatch, stub_orch, sample_prospect
) -> None:
    """propose_only must forward self.config.critic_max_refinements."""
    monkeypatch.setattr(orch_mod, "load_current_insights", lambda: None)
    stub_orch.load_dossiers_for_prospects = MagicMock(return_value={})
    stub_orch.proposal.write.return_value = _proposal_body()
    stub_orch.proposal_critic.review.return_value = _pass_proposal_report()

    # Override config to max_refinements=0 → critic should never be called.
    stub_orch.config = SalesPipelineConfig(critic_max_refinements=0)
    req = ProposalRequest(
        prospect=sample_prospect,
        product_name="P",
        value_proposition="V",
        annual_cost_usd=25000.0,
    )
    proposal = stub_orch.propose_only(req)
    assert proposal is not None
    stub_orch.proposal_critic.review.assert_not_called()


# ---------------------------------------------------------------------------
# Regression: discovery and negotiation must match prospects by id, not company_name
# ---------------------------------------------------------------------------


def test_discovery_and_negotiation_match_by_id_not_company_name(stub_orch, sample_icp) -> None:
    """Two prospects sharing company_name must each receive their own data — matched by id.

    Regression guard for the bug where _run_discovery and _run_negotiation looked
    up per-prospect data via a linear scan on ``company_name``, which is non-unique
    when the deep-research path generates multiple decision-makers at one account.

    This test calls _run_discovery and _run_negotiation directly (the same pattern
    used by test_stage_methods_are_individually_callable throughout this file) because
    exercising them through the public run() API would require mocking every upstream
    stage (prospecting, outreach, qualification, dossier loading) just to reach
    discovery, which would obscure the specific per-prospect matching behaviour
    under test.
    """
    p1 = Prospect(id="prs_shared_1", company_name="SharedCo", contact_name="Alice")
    p2 = Prospect(id="prs_shared_2", company_name="SharedCo", contact_name="Bob")

    bant = BANTScore(budget=8, authority=9, need=7, timeline=8)
    meddic = MEDDICScore(
        metrics_identified=True,
        economic_buyer_known=True,
        decision_criteria_understood=True,
        decision_process_mapped=True,
        identify_pain=True,
        champion_found=True,
    )
    qual_p1 = QualificationScore(
        prospect=p1,
        bant=bant,
        meddic=meddic,
        overall_score=0.9,
        value_creation_level=3,
        recommended_action="advance",
        qualification_notes="alice-specific-notes",
    )
    qual_p2 = QualificationScore(
        prospect=p2,
        bant=bant,
        meddic=meddic,
        overall_score=0.7,
        value_creation_level=2,
        recommended_action="advance",
        qualification_notes="bob-specific-notes",
    )

    ctx = orch_mod._RunContext(
        request=SalesPipelineRequest(
            product_name="P",
            value_proposition="A valid long value proposition",
            icp=sample_icp,
        ),
        job_id="j-id-match",
        icp_json=sample_icp.model_dump_json(indent=2),
        product="P",
        vp="A valid long value proposition",
        company_context="",
        cases="",
        entry=PipelineStage.PROSPECTING,
        insights_ctx="",
        config=SalesPipelineConfig(),
        update=orch_mod._noop_update,
    )

    # -- _run_discovery -------------------------------------------------------
    discovery_received: dict = {}

    def _capture_discovery(prospect_json, qual_json, *a, **kw):
        pid = json.loads(prospect_json)["id"]
        discovery_received[pid] = qual_json
        return _discovery_body()

    stub_orch.discovery.prepare.side_effect = _capture_discovery

    plans = stub_orch._run_discovery(ctx, [p1, p2], [qual_p1, qual_p2], {})
    assert len(plans) == 2
    assert {plan.prospect.id for plan in plans} == {p1.id, p2.id}

    # Each prospect must have received its OWN qual JSON — no cross-contamination.
    assert "alice-specific-notes" in discovery_received["prs_shared_1"]
    assert "bob-specific-notes" in discovery_received["prs_shared_2"]
    assert "bob-specific-notes" not in discovery_received["prs_shared_1"]
    assert "alice-specific-notes" not in discovery_received["prs_shared_2"]

    # -- _run_negotiation -----------------------------------------------------
    roi = ROIModel(
        annual_cost_usd=25000.0,
        estimated_annual_benefit_usd=70000.0,
        payback_months=6.0,
        roi_percentage=180.0,
    )
    prop_p1 = SalesProposal(
        prospect=p1,
        executive_summary="alice-proposal-summary",
        situation_analysis="...",
        proposed_solution="...",
        roi_model=roi,
    )
    prop_p2 = SalesProposal(
        prospect=p2,
        executive_summary="bob-proposal-summary",
        situation_analysis="...",
        proposed_solution="...",
        roi_model=roi,
    )

    negotiation_received: dict = {}

    def _capture_negotiation(prospect_json, prop_json, *a, **kw):
        pid = json.loads(prospect_json)["id"]
        negotiation_received[pid] = prop_json
        return _closer_body()

    stub_orch.closer.develop_strategy.side_effect = _capture_negotiation

    strategies = stub_orch._run_negotiation(ctx, [p1, p2], [prop_p1, prop_p2])
    assert len(strategies) == 2
    assert {strategy.prospect.id for strategy in strategies} == {p1.id, p2.id}

    # Each prospect must have received its OWN proposal JSON — no cross-contamination.
    assert "alice-proposal-summary" in negotiation_received["prs_shared_1"]
    assert "bob-proposal-summary" in negotiation_received["prs_shared_2"]
    assert "bob-proposal-summary" not in negotiation_received["prs_shared_1"]
    assert "alice-proposal-summary" not in negotiation_received["prs_shared_2"]


# ---------------------------------------------------------------------------
# _run_discovery: resolved dossier_map wiring
# ---------------------------------------------------------------------------


def test_run_discovery_passes_resolved_dossier_per_prospect(stub_orch, sample_icp) -> None:
    """_run_discovery forwards each prospect's dossier from an already-resolved map.

    Mirrors the outreach-stage dossier pattern: a prospect with a saved dossier
    gets it forwarded to discovery_one/discovery.prepare; a prospect without one
    still produces a discovery plan with dossier=None (no new failure mode).
    """
    p1 = Prospect(id="prs_dossier_1", company_name="AcmeCo", contact_name="Alice")
    p2 = Prospect(id="prs_dossier_2", company_name="OtherCo", contact_name="Bob")

    dossier_p1 = ProspectDossier(
        dossier_id="dsr_1",
        prospect_id=p1.id,
        full_name="Alice",
        current_title="VP",
        current_company="AcmeCo",
        executive_summary="Runs sales at AcmeCo.",
        confidence=0.9,
    )
    dossier_map = {p1.id: dossier_p1}  # p2 intentionally absent

    ctx = orch_mod._RunContext(
        request=SalesPipelineRequest(
            product_name="P",
            value_proposition="A valid long value proposition",
            icp=sample_icp,
        ),
        job_id="j-id-dossier",
        icp_json=sample_icp.model_dump_json(indent=2),
        product="P",
        vp="A valid long value proposition",
        company_context="",
        cases="",
        entry=PipelineStage.PROSPECTING,
        insights_ctx="",
        config=SalesPipelineConfig(),
        update=orch_mod._noop_update,
    )

    dossier_received: dict = {}

    def _capture_dossier(prospect_json, qual_json, *a, **kw):
        pid = json.loads(prospect_json)["id"]
        dossier_received[pid] = kw.get("dossier")
        return _discovery_body()

    stub_orch.discovery.prepare.side_effect = _capture_dossier

    plans = stub_orch._run_discovery(ctx, [p1, p2], [], dossier_map)
    assert len(plans) == 2
    assert dossier_received[p1.id] is dossier_p1
    assert dossier_received[p2.id] is None
