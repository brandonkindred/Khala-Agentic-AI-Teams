"""Tests for the outreach + proposal LLM-as-judge critics.

Each critic is exercised through a programmable :class:`CannedLLMClient` so
the suite has no Strands or live-LLM dependency.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from llm_service.interface import LLMClient
from sales_team.agents import OutreachAgent, ProposalAgent
from sales_team.critics import (
    OutreachCriticAgent,
    ProposalCriticAgent,
    format_critic_feedback,
)
from sales_team.models import (
    BANTScore,
    CriticViolation,
    EmailTouch,
    EvidenceCitation,
    IdealCustomerProfile,
    MEDDICScore,
    OutreachSequence,
    OutreachVariant,
    Prospect,
    ProspectDossier,
    QualificationScore,
    ROIModel,
    SalesProposal,
)
from sales_team.orchestrator import SalesPodOrchestrator


class CannedLLMClient(LLMClient):
    """LLMClient that returns pre-programmed dicts in queue order.

    Mirrors the test helper in ``test_sales_team.py``. Re-defined locally to
    keep this file self-contained — both critic tests and orchestrator-level
    retry tests share this exact pattern.

    The critics make two calls per review, and this double records them in
    two SEPARATE lists — they are not aliases:

    * ``calls`` — the think=False ``complete_json`` formatting calls. These
      consume the programmed ``responses`` queue, so assertions about the
      *verdict* (and about the JSON-shape system prompt) belong here.
    * ``reasoning_calls`` — the think=True ``complete`` reasoning calls. These
      never touch the queue. The critic's built prompt (dossier, rubric,
      sequence/proposal payload) is sent here, so assertions about *prompt
      content* belong here, not in ``calls``.
    """

    def __init__(self, responses: List[Dict[str, Any]]) -> None:
        self._responses = list(responses)
        self.calls: List[Dict[str, Any]] = []
        self.reasoning_calls: List[Dict[str, Any]] = []

    def complete(
        self,
        prompt: str,
        *,
        objective: str,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        system_prompt: Optional[str] = None,
        tools: Optional[list] = None,
        think: bool | str | None = None,
    ) -> str:
        # The critics' think=True reasoning pass (complete_json_via_reasoning /
        # complete_validated_via_reasoning) calls this before the real verdict
        # call. Override the LLMClient base's default (which would otherwise
        # route through complete_json and silently consume a queued response)
        # with a harmless placeholder that doesn't touch the response queue —
        # ``self.calls`` and ``self._responses`` continue to track only the
        # verdict-producing complete_json calls these tests assert on. Recorded
        # separately in ``self.reasoning_calls`` for tests that need to inspect
        # the reasoning-pass prompt itself.
        self.reasoning_calls.append(
            {
                "prompt": prompt,
                "system_prompt": system_prompt,
                "think": think,
                "temperature": temperature,
                "objective": objective,
            }
        )
        return "Reasoning: proceeding as configured by the test fixture."

    def complete_json(
        self,
        prompt: str,
        *,
        temperature: float = 0.0,
        system_prompt: Optional[str] = None,
        tools: Optional[list] = None,
        think: bool = False,
        objective: str = "",
        **kwargs: Any,
    ) -> Dict[str, Any]:
        self.calls.append(
            {
                "prompt": prompt,
                "system_prompt": system_prompt,
                "temperature": temperature,
                "think": think,
                "objective": objective,
            }
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
        industry=["SaaS"],
        company_size_min=50,
        company_size_max=500,
        job_titles=["VP Sales"],
        pain_points=["manual reporting", "long sales cycles"],
    )


@pytest.fixture()
def sample_prospect() -> Prospect:
    return Prospect(
        id="prs_critic_test",
        company_name="Acme Corp",
        contact_name="Jane Smith",
        contact_title="VP Sales",
        icp_match_score=0.85,
    )


@pytest.fixture()
def sample_dossier(sample_prospect: Prospect) -> ProspectDossier:
    return ProspectDossier(
        dossier_id="dsr_critic_test",
        prospect_id=sample_prospect.id,
        full_name="Jane Smith",
        current_title="VP Sales",
        current_company="Acme Corp",
        executive_summary="Jane runs sales at Acme Corp.",
        trigger_events=["Series B funding"],
        sources=["https://news.example.com/acme-series-b"],
        confidence=0.82,
    )


@pytest.fixture()
def sample_sequence(sample_prospect: Prospect, sample_dossier: ProspectDossier) -> OutreachSequence:
    return OutreachSequence(
        prospect=sample_prospect,
        dossier_id=sample_dossier.dossier_id,
        dossier_confidence=sample_dossier.confidence,
        variants=[
            OutreachVariant(
                angle="trigger_event",
                email_sequence=[
                    EmailTouch(
                        day=1,
                        subject_line="Series B at Acme",
                        body="Saw the Series B announcement — congrats.",
                        call_to_action="Open to a 15-min call next week?",
                        evidence_citations=[
                            EvidenceCitation(
                                claim="Series B funding",
                                dossier_field="trigger_events[0]",
                                source_url="https://news.example.com/acme-series-b",
                            )
                        ],
                    )
                ],
                personalization_grade="high",
            )
        ],
    )


@pytest.fixture()
def sample_proposal(sample_prospect: Prospect) -> SalesProposal:
    return SalesProposal(
        prospect=sample_prospect,
        executive_summary="Tighten Acme's outbound motion in 90 days.",
        situation_analysis="Manual reporting blocks the SDR team from prospecting.",
        proposed_solution="Automated reporting pack + cadence library.",
        roi_model=ROIModel(
            annual_cost_usd=25000.0,
            estimated_annual_benefit_usd=100000.0,
            payback_months=3.0,
            roi_percentage=300.0,
            assumptions=["20 SDRs save 4h/week"],
        ),
        next_steps=["Buyer signs SOW within 7 days", "Vendor kicks off implementation week 1"],
    )


@pytest.fixture()
def sample_qualification(sample_prospect: Prospect) -> QualificationScore:
    return QualificationScore(
        prospect=sample_prospect,
        bant=BANTScore(budget=8, authority=9, need=8, timeline=7),
        meddic=MEDDICScore(
            metrics_identified=True,
            economic_buyer_known=True,
            decision_criteria_understood=True,
            decision_process_mapped=True,
            identify_pain=True,
            champion_found=True,
        ),
        overall_score=0.82,
        value_creation_level=3,
        recommended_action="advance",
    )


# ---------------------------------------------------------------------------
# OutreachCriticAgent
# ---------------------------------------------------------------------------


def _pass_outreach_report() -> Dict[str, Any]:
    return {"status": "PASS", "approved": True, "violations": [], "rubric_version": "v1"}


def _fail_outreach_report(rule_id: str, description: str = "violation") -> Dict[str, Any]:
    return {
        "status": "FAIL",
        "approved": False,
        "violations": [
            {
                "rule_id": rule_id,
                "severity": "must_fix",
                "section": "variants[0].day1",
                "evidence_quote": "...",
                "description": description,
                "suggested_fix": "Fix it.",
            }
        ],
        "rubric_version": "v1",
    }


class TestOutreachCriticAgent:
    """Regression tests for OutreachCriticAgent's two-call review flow."""

    def test_pass_report_marks_approved(
        self,
        sample_sequence: OutreachSequence,
        sample_dossier: ProspectDossier,
        sample_icp: IdealCustomerProfile,
    ) -> None:
        """A passing sequence yields an approved report and prose-only reasoning prompts."""
        llm = CannedLLMClient([_pass_outreach_report()])
        critic = OutreachCriticAgent(llm_client=llm)

        report = critic.review(sample_sequence, sample_dossier, sample_icp)

        assert report.status == "PASS"
        assert report.approved is True
        assert report.violations == []
        assert llm.reasoning_calls and "Outreach Reviewer" in (
            llm.reasoning_calls[0]["system_prompt"] or ""
        )
        # Locks in the two-pass contract: the reasoning pass runs with
        # think=True, the formatting pass with think=False.
        assert llm.reasoning_calls[0]["think"] is True
        assert llm.calls and llm.calls[0]["think"] is False
        # Reasoning user + system prompts must stay free of JSON-only
        # directives — those belong on the formatting pass only.
        reasoning_prompt = llm.reasoning_calls[0]["prompt"] or ""
        reasoning_system = llm.reasoning_calls[0]["system_prompt"] or ""
        for text in (reasoning_prompt, reasoning_system):
            assert "Return JSON only" not in text
            assert "No markdown fences" not in text
            assert "Return ONLY a JSON object" not in text

    def test_fail_with_fabricated_citation(
        self,
        sample_sequence: OutreachSequence,
        sample_dossier: ProspectDossier,
        sample_icp: IdealCustomerProfile,
    ) -> None:
        llm = CannedLLMClient(
            [_fail_outreach_report("outreach.citation.fabricated", "Cited URL not in dossier")]
        )
        critic = OutreachCriticAgent(llm_client=llm)

        report = critic.review(sample_sequence, sample_dossier, sample_icp)

        assert report.status == "FAIL"
        assert report.approved is False
        assert report.must_fix_count() == 1
        assert report.violations[0].rule_id == "outreach.citation.fabricated"

    def test_fail_with_subject_too_long(
        self,
        sample_sequence: OutreachSequence,
        sample_dossier: ProspectDossier,
        sample_icp: IdealCustomerProfile,
    ) -> None:
        llm = CannedLLMClient(
            [_fail_outreach_report("outreach.day1.subject_length", "Subject is 78 chars")]
        )
        critic = OutreachCriticAgent(llm_client=llm)
        report = critic.review(sample_sequence, sample_dossier, sample_icp)
        assert report.violations[0].rule_id == "outreach.day1.subject_length"

    def test_invariant_forces_approved_false_on_must_fix(
        self,
        sample_sequence: OutreachSequence,
        sample_dossier: ProspectDossier,
        sample_icp: IdealCustomerProfile,
    ) -> None:
        # Model lies: claims PASS+approved while listing a must_fix violation.
        bogus = {
            "status": "PASS",
            "approved": True,
            "violations": [
                {
                    "rule_id": "outreach.day1.cta",
                    "severity": "must_fix",
                    "description": "no CTA",
                    "suggested_fix": "add one",
                }
            ],
            "rubric_version": "v1",
        }
        llm = CannedLLMClient([bogus])
        critic = OutreachCriticAgent(llm_client=llm)

        report = critic.review(sample_sequence, sample_dossier, sample_icp)

        # Critic enforces approved == (status == 'PASS' AND no must_fix) AND
        # reconciles status itself — a must_fix violation forces FAIL, not
        # just approved=False (a status="PASS"/approved=False report would
        # itself violate the documented invariant).
        assert report.approved is False
        assert report.status == "FAIL"

    def test_parse_failure_falls_back_to_fail(
        self,
        sample_sequence: OutreachSequence,
        sample_dossier: ProspectDossier,
        sample_icp: IdealCustomerProfile,
    ) -> None:
        # Three invalid payloads — initial + 2 correction attempts all fail
        # validation, complete_validated raises, critic catches and returns FAIL.
        invalid = {"not_a_real_field": True}
        llm = CannedLLMClient([invalid, invalid, invalid])
        critic = OutreachCriticAgent(llm_client=llm)

        report = critic.review(sample_sequence, sample_dossier, sample_icp)

        assert report.status == "FAIL"
        assert report.approved is False
        assert report.notes is not None and "parseable JSON" in report.notes
        assert len(llm.reasoning_calls) == 1
        # ``llm.calls`` is formatting-only (see CannedLLMClient). The formatting
        # pass retries on every invalid payload: 1 initial +
        # correction_attempts=2 corrective resends, all queued as invalid.
        assert len(llm.calls) == 3


# ---------------------------------------------------------------------------
# ProposalCriticAgent
# ---------------------------------------------------------------------------


def _pass_proposal_report() -> Dict[str, Any]:
    return {"status": "PASS", "approved": True, "violations": [], "rubric_version": "v1"}


def _fail_proposal_report(rule_id: str) -> Dict[str, Any]:
    return {
        "status": "FAIL",
        "approved": False,
        "violations": [
            {
                "rule_id": rule_id,
                "severity": "must_fix",
                "section": "roi_model" if "roi" in rule_id else "overall",
                "description": "violation",
                "suggested_fix": "Fix it.",
            }
        ],
        "rubric_version": "v1",
    }


class TestProposalCriticAgent:
    """Regression tests for ProposalCriticAgent's two-call review flow."""

    def test_pass_report_marks_approved(
        self,
        sample_proposal: SalesProposal,
        sample_dossier: ProspectDossier,
        sample_qualification: QualificationScore,
    ) -> None:
        """A passing proposal yields an approved report and prose-only reasoning prompts."""
        llm = CannedLLMClient([_pass_proposal_report()])
        critic = ProposalCriticAgent(llm_client=llm)

        report = critic.review(sample_proposal, sample_dossier, sample_qualification)

        assert report.status == "PASS"
        assert report.approved is True
        reasoning_system = llm.reasoning_calls[0]["system_prompt"] or ""
        assert "Proposal Reviewer" in reasoning_system
        # Regression guard: the reasoning pass must stay prose-only — a JSON
        # directive here would outrank its "answer in structured prose"
        # instruction and collapse the two-call split back to one.
        reasoning_prompt = llm.reasoning_calls[0]["prompt"] or ""
        for text in (reasoning_prompt, reasoning_system):
            assert "Return JSON only" not in text
            assert "No markdown fences" not in text
            assert "Return ONLY a JSON object" not in text

    def test_fail_with_broken_roi_math(
        self,
        sample_proposal: SalesProposal,
        sample_dossier: ProspectDossier,
        sample_qualification: QualificationScore,
    ) -> None:
        llm = CannedLLMClient([_fail_proposal_report("proposal.roi.arithmetic")])
        critic = ProposalCriticAgent(llm_client=llm)
        report = critic.review(sample_proposal, sample_dossier, sample_qualification)
        assert report.violations[0].rule_id == "proposal.roi.arithmetic"
        assert report.approved is False

    def test_fail_with_unfounded_claim(
        self,
        sample_proposal: SalesProposal,
        sample_dossier: ProspectDossier,
        sample_qualification: QualificationScore,
    ) -> None:
        llm = CannedLLMClient([_fail_proposal_report("proposal.claims.founded")])
        critic = ProposalCriticAgent(llm_client=llm)
        report = critic.review(sample_proposal, sample_dossier, sample_qualification)
        assert report.violations[0].rule_id == "proposal.claims.founded"

    def test_parse_failure_falls_back_to_fail(
        self,
        sample_proposal: SalesProposal,
        sample_dossier: ProspectDossier,
        sample_qualification: QualificationScore,
    ) -> None:
        invalid = {"missing_status": True}
        llm = CannedLLMClient([invalid, invalid, invalid])
        critic = ProposalCriticAgent(llm_client=llm)

        report = critic.review(sample_proposal, sample_dossier, sample_qualification)

        assert report.status == "FAIL"
        assert report.notes is not None and "parseable JSON" in report.notes
        assert len(llm.reasoning_calls) == 1
        # ``llm.calls`` is formatting-only (see CannedLLMClient). The formatting
        # pass retries on every invalid payload: 1 initial +
        # correction_attempts=2 corrective resends, all queued as invalid.
        assert len(llm.calls) == 3

    def test_invariant_forces_status_fail_on_must_fix(
        self,
        sample_proposal: SalesProposal,
        sample_dossier: ProspectDossier,
        sample_qualification: QualificationScore,
    ) -> None:
        # Model lies: claims PASS+approved while listing a must_fix violation.
        bogus = {
            "status": "PASS",
            "approved": True,
            "violations": [
                {
                    "rule_id": "proposal.roi.arithmetic",
                    "severity": "must_fix",
                    "section": "roi_model",
                    "description": "payback_months doesn't reconcile",
                    "suggested_fix": "recompute payback_months",
                }
            ],
            "rubric_version": "v1",
        }
        llm = CannedLLMClient([bogus])
        critic = ProposalCriticAgent(llm_client=llm)

        report = critic.review(sample_proposal, sample_dossier, sample_qualification)

        # Critic reconciles BOTH status and approved from the violations —
        # not just approved (a status="PASS"/approved=False report would
        # itself violate the documented invariant on ProposalCriticReport).
        assert report.status == "FAIL"
        assert report.approved is False

    def test_handles_missing_dossier_and_qualification(
        self, sample_proposal: SalesProposal
    ) -> None:
        """Missing dossier/qualification render as placeholders on the reasoning pass only."""
        llm = CannedLLMClient([_pass_proposal_report()])
        critic = ProposalCriticAgent(llm_client=llm)
        report = critic.review(sample_proposal, None, None)
        assert report.approved is True
        # The dossier-fallback text is in the reasoning-pass prompt (the raw
        # _build_prompt output); the formatting call only sees its prose.
        assert "(no dossier supplied)" in llm.reasoning_calls[0]["prompt"]
        assert "(no qualification supplied)" in llm.reasoning_calls[0]["prompt"]
        # ``llm.calls`` tracks ONLY formatting ``complete_json`` passes (see
        # CannedLLMClient docstring); ``[0]`` is the first formatting prompt.
        # Formatting user prompt is transcribe-only — source placeholders must
        # not leak into it (CannedLLM.complete returns fixed prose, not the
        # reasoning prompt, so a leak here means the formatting path was fed
        # the dossier/qualification material directly).
        formatting_prompt = llm.calls[0]["prompt"] or ""
        assert "(no dossier supplied)" not in formatting_prompt
        assert "(no qualification supplied)" not in formatting_prompt
        assert "--- DOSSIER ---" not in formatting_prompt
        assert "--- QUALIFICATION ---" not in formatting_prompt


# ---------------------------------------------------------------------------
# format_critic_feedback
# ---------------------------------------------------------------------------


class TestFormatCriticFeedback:
    """Unit tests for format_critic_feedback severity ordering and rendering."""

    def test_empty_violations_returns_notes_or_default(self) -> None:
        """Empty violation list falls back to notes text or a default rejection line."""
        assert format_critic_feedback([], notes=None).startswith("Critic rejected")
        assert format_critic_feedback([], notes="parse failure") == "parse failure"

    def test_sorts_must_fix_first(self) -> None:
        """must_fix violations are listed before should_fix and consider."""
        violations = [
            CriticViolation(
                rule_id="rule.consider",
                severity="consider",
                description="advisory",
                suggested_fix="fix",
            ),
            CriticViolation(
                rule_id="rule.must_fix",
                severity="must_fix",
                description="blocker",
                suggested_fix="fix",
            ),
            CriticViolation(
                rule_id="rule.should_fix",
                severity="should_fix",
                description="nice",
                suggested_fix="fix",
            ),
        ]
        text = format_critic_feedback(violations)
        idx_must = text.index("rule.must_fix")
        idx_should = text.index("rule.should_fix")
        idx_consider = text.index("rule.consider")
        assert idx_must < idx_should < idx_consider

    def test_includes_evidence_quote_and_section(self) -> None:
        """Rendered feedback includes section path, evidence quote, and suggested fix."""
        violations = [
            CriticViolation(
                rule_id="outreach.day1.subject_length",
                severity="must_fix",
                section="variants[0].day1",
                evidence_quote="A really long subject line",
                description="too long",
                suggested_fix="tighten",
            )
        ]
        text = format_critic_feedback(violations)
        assert "[variants[0].day1]" in text
        assert "evidence" in text and "really long subject line" in text
        assert "fix: tighten" in text


# ---------------------------------------------------------------------------
# Orchestrator-level: critic-gated emit + one-shot refinement
# ---------------------------------------------------------------------------


def _good_variant_payload(
    source_url: str = "https://news.example.com/acme-series-b",
) -> Dict[str, Any]:
    return {
        "variants": [
            {
                "angle": "trigger_event",
                "email_sequence": [
                    {
                        "day": 1,
                        "subject_line": "Series B at Acme",
                        "body": "Saw the Series B — congrats.",
                        "call_to_action": "Open to a 15-minute call?",
                        "evidence_citations": [
                            {
                                "claim": "Series B funding",
                                "dossier_field": "trigger_events[0]",
                                "source_url": source_url,
                            }
                        ],
                    }
                ],
                "rationale": "Trigger event grounded in dossier source.",
                "personalization_grade": "high",
            }
        ]
    }


def _good_proposal_body() -> Dict[str, Any]:
    return {
        "executive_summary": "Tighten Acme's outbound motion in 90 days.",
        "situation_analysis": "Manual reporting blocks the SDR team.",
        "proposed_solution": "Automated reporting + cadence library.",
        "roi_model": {
            "annual_cost_usd": 25000.0,
            "estimated_annual_benefit_usd": 100000.0,
            "payback_months": 3.0,
            "roi_percentage": 300.0,
            "assumptions": ["20 SDRs save 4h/week"],
        },
        "investment_table": "Year 1: $25k",
        "implementation_timeline": "8 weeks",
        "risk_mitigation": "Phased rollout",
        "next_steps": ["Buyer signs SOW within 7 days"],
        "custom_sections": [],
    }


class TestOrchestratorCriticRefinement:
    """End-to-end test of the critic-gated emit -> wrap -> review -> refine loop."""

    def _orchestrator_with_canned_clients(
        self, outreach_llm: CannedLLMClient, critic_llm: CannedLLMClient
    ) -> SalesPodOrchestrator:
        orch = SalesPodOrchestrator()
        orch.outreach = OutreachAgent(llm_client=outreach_llm)
        orch.outreach_critic = OutreachCriticAgent(llm_client=critic_llm)
        return orch

    def test_outreach_default_budget_reviews_once_and_never_regenerates(
        self,
        sample_prospect: Prospect,
        sample_dossier: ProspectDossier,
        sample_icp: IdealCustomerProfile,
    ) -> None:
        """With the shipped default max_refinements=1, the review budget is
        spent entirely on the one review call — there is no budget left to
        check a regenerated draft, so the initial (reviewed) sequence is
        returned as-is rather than an unreviewed regeneration."""
        outreach_llm = CannedLLMClient([_good_variant_payload()])  # initial emit only
        critic_llm = CannedLLMClient(
            [
                _fail_outreach_report("outreach.day1.cta", "missing CTA"),  # rejects initial
            ]
        )
        orch = self._orchestrator_with_canned_clients(outreach_llm, critic_llm)

        sequence = orch._generate_outreach_with_critic(
            sample_prospect,
            sample_dossier,
            "Acme Pipeline",
            "Lift outbound velocity",
            "Beta Inc saw 3x demos",
            "Acme is expanding into EMEA",
            None,
            sample_icp,
        )

        # One outreach emit + one critic review (reasoning + formatting pass) —
        # no regeneration, since there's no budget left to review its output.
        assert len(outreach_llm.calls) == 1
        assert len(critic_llm.calls) == 1
        assert len(critic_llm.reasoning_calls) == 1
        assert sequence.variants

    def test_outreach_refines_once_when_budget_allows_a_second_review(
        self,
        sample_prospect: Prospect,
        sample_dossier: ProspectDossier,
        sample_icp: IdealCustomerProfile,
    ) -> None:
        """With max_refinements=2, a FAIL on the first review leaves budget
        for a second review, so the agent regenerates once and that
        regenerated draft is itself reviewed before being returned."""
        outreach_llm = CannedLLMClient(
            [
                _good_variant_payload(),  # initial emit
                _good_variant_payload(),  # refined emit
            ]
        )
        critic_llm = CannedLLMClient(
            [
                _fail_outreach_report("outreach.day1.cta", "missing CTA"),  # rejects initial
                _pass_outreach_report(),  # approves the refined regeneration
            ]
        )
        orch = self._orchestrator_with_canned_clients(outreach_llm, critic_llm)

        sequence = orch._generate_outreach_with_critic(
            sample_prospect,
            sample_dossier,
            "Acme Pipeline",
            "Lift outbound velocity",
            "Beta Inc saw 3x demos",
            "Acme is expanding into EMEA",
            None,
            sample_icp,
            max_refinements=2,
        )

        # Two outreach emits + two critic reviews — the regenerated draft was
        # itself reviewed (and approved) before being returned.
        assert len(outreach_llm.calls) == 2
        assert len(critic_llm.calls) == 2
        assert len(critic_llm.reasoning_calls) == 2
        assert sequence.variants
        # Refined emit's prompt carries the critic feedback we returned.
        assert "Reviewer feedback to address" in outreach_llm.calls[1]["prompt"]
        assert "outreach.day1.cta" in outreach_llm.calls[1]["prompt"]

    def test_outreach_skips_refinement_when_critic_approves(
        self,
        sample_prospect: Prospect,
        sample_dossier: ProspectDossier,
        sample_icp: IdealCustomerProfile,
    ) -> None:
        outreach_llm = CannedLLMClient([_good_variant_payload()])
        critic_llm = CannedLLMClient([_pass_outreach_report()])
        orch = self._orchestrator_with_canned_clients(outreach_llm, critic_llm)

        orch._generate_outreach_with_critic(
            sample_prospect,
            sample_dossier,
            "p",
            "vp",
            "",
            "",
            None,
            sample_icp,
        )

        # No refinement attempt — only the initial emit + one critic review
        # (reasoning + formatting pass).
        assert len(outreach_llm.calls) == 1
        assert len(critic_llm.calls) == 1
        assert len(critic_llm.reasoning_calls) == 1

    def test_outreach_skips_critic_when_icp_missing(
        self, sample_prospect: Prospect, sample_dossier: ProspectDossier
    ) -> None:
        outreach_llm = CannedLLMClient([_good_variant_payload()])
        critic_llm = CannedLLMClient([])  # would AssertionError if called
        orch = self._orchestrator_with_canned_clients(outreach_llm, critic_llm)

        orch._generate_outreach_with_critic(
            sample_prospect,
            sample_dossier,
            "p",
            "vp",
            "",
            "",
            None,
            None,  # icp=None — outreach_only-style call
        )

        assert len(outreach_llm.calls) == 1
        assert len(critic_llm.calls) == 0
        assert len(critic_llm.reasoning_calls) == 0

    def test_proposal_default_budget_reviews_once_and_never_regenerates(
        self,
        sample_prospect: Prospect,
        sample_dossier: ProspectDossier,
        sample_qualification: QualificationScore,
    ) -> None:
        """With the shipped default max_refinements=1, the review budget is
        spent entirely on the one review call — there is no budget left to
        check a regenerated draft, so the initial (reviewed) proposal is
        returned as-is rather than an unreviewed regeneration."""
        proposal_llm = CannedLLMClient([_good_proposal_body()])  # initial emit only
        critic_llm = CannedLLMClient(
            [_fail_proposal_report("proposal.next_steps.concrete")]  # rejects initial
        )
        orch = SalesPodOrchestrator()
        orch.proposal = ProposalAgent(llm_client=proposal_llm)
        orch.proposal_critic = ProposalCriticAgent(llm_client=critic_llm)

        proposal = orch._generate_proposal_with_critic(
            sample_prospect,
            "Acme Pipeline",
            "Lift outbound velocity",
            25000.0,
            "Discovery: needs reporting",
            "Beta Inc saw 3x demos",
            "Acme is expanding into EMEA",
            None,
            sample_dossier,
            sample_qualification,
        )

        # One proposal emit + one critic review — no regeneration, since
        # there's no budget left to review its output.
        assert len(proposal_llm.calls) == 1
        assert len(critic_llm.calls) == 1
        assert len(critic_llm.reasoning_calls) == 1
        assert proposal.executive_summary

    def test_proposal_refines_once_when_budget_allows_a_second_review(
        self,
        sample_prospect: Prospect,
        sample_dossier: ProspectDossier,
        sample_qualification: QualificationScore,
    ) -> None:
        """With max_refinements=2, a FAIL on the first review leaves budget
        for a second review, so the agent regenerates once and that
        regenerated draft is itself reviewed before being returned."""
        proposal_llm = CannedLLMClient(
            [
                _good_proposal_body(),  # initial
                _good_proposal_body(),  # refined
            ]
        )
        critic_llm = CannedLLMClient(
            [
                _fail_proposal_report("proposal.next_steps.concrete"),  # rejects initial
                _pass_proposal_report(),  # approves the refined regeneration
            ]
        )
        orch = SalesPodOrchestrator()
        orch.proposal = ProposalAgent(llm_client=proposal_llm)
        orch.proposal_critic = ProposalCriticAgent(llm_client=critic_llm)

        proposal = orch._generate_proposal_with_critic(
            sample_prospect,
            "Acme Pipeline",
            "Lift outbound velocity",
            25000.0,
            "Discovery: needs reporting",
            "Beta Inc saw 3x demos",
            "Acme is expanding into EMEA",
            None,
            sample_dossier,
            sample_qualification,
            max_refinements=2,
        )

        # Two proposal emits + two critic reviews — the regenerated draft was
        # itself reviewed (and approved) before being returned.
        assert len(proposal_llm.calls) == 2
        assert len(critic_llm.calls) == 2
        assert len(critic_llm.reasoning_calls) == 2
        assert proposal.executive_summary
        assert "Reviewer feedback to address" in proposal_llm.calls[1]["prompt"]
        assert "proposal.next_steps.concrete" in proposal_llm.calls[1]["prompt"]

    def test_proposal_keeps_original_when_refine_emit_raises(
        self,
        sample_prospect: Prospect,
        sample_dossier: ProspectDossier,
        sample_qualification: QualificationScore,
    ) -> None:
        # Only one good response queued — the refine call will raise AssertionError
        # from the canned client; the critic-gated helper must catch and return
        # the original proposal rather than crash the prospect's slot. Budget of
        # 2 is required so the loop actually reaches the regenerate step — at
        # budget 1 the loop now breaks (budget-exhausted) before ever calling
        # write() a second time, so this path would otherwise go untested.
        proposal_llm = CannedLLMClient([_good_proposal_body()])
        critic_llm = CannedLLMClient([_fail_proposal_report("proposal.roi.arithmetic")])
        orch = SalesPodOrchestrator()
        orch.proposal = ProposalAgent(llm_client=proposal_llm)
        orch.proposal_critic = ProposalCriticAgent(llm_client=critic_llm)

        proposal = orch._generate_proposal_with_critic(
            sample_prospect,
            "p",
            "vp",
            25000.0,
            "",
            "",
            "",
            None,
            sample_dossier,
            sample_qualification,
            max_refinements=2,
        )

        # Original proposal returned despite refine failure.
        assert proposal.executive_summary == "Tighten Acme's outbound motion in 90 days."
