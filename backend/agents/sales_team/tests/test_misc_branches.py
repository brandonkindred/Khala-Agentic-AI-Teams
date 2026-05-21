"""Targeted tests for small, isolated branches not naturally covered elsewhere:

* ``EmailTouch`` citation validator with non-None context but no
  ``dossier_source_urls`` key.
* ``agents._with_insights`` whitespace-only fallback branch.
* Outreach / proposal critic prompts when dossier exceeds the char cap
  (truncation marker appears).
* ``ProposalCriticAgent`` invariant override (LLM says approved=True but
  must_fix > 0 → critic flips approved to False).
* ``format_critic_feedback`` notes-appended branch.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from llm_service.interface import LLMClient
from sales_team.agents import _with_insights
from sales_team.critics import (
    OutreachCriticAgent,
    ProposalCriticAgent,
    format_critic_feedback,
)
from sales_team.critics.outreach_critic import _DOSSIER_CHAR_CAP
from sales_team.models import (
    BANTScore,
    CriticViolation,
    EmailTouch,
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


class _CannedLLM(LLMClient):
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
        self.calls.append({"prompt": prompt})
        return self._responses.pop(0)


# ---------------------------------------------------------------------------
# EmailTouch validator: context present but no source URLs registered.
# ---------------------------------------------------------------------------


def test_email_touch_keeps_citations_when_context_missing_source_urls() -> None:
    """When validation context exists but lacks ``dossier_source_urls``,
    the validator must return the citations unchanged (no stripping)."""
    ctx: Dict[str, Any] = {"some_other_key": True}
    touch = EmailTouch.model_validate(
        {
            "day": 1,
            "subject_line": "s",
            "body": "b",
            "evidence_citations": [
                {"claim": "c", "dossier_field": "f", "source_url": "https://anywhere.com"}
            ],
        },
        context=ctx,
    )
    assert len(touch.evidence_citations) == 1
    assert touch.evidence_citations[0].source_url == "https://anywhere.com"


# ---------------------------------------------------------------------------
# agents._with_insights: empty / whitespace-only insights.
# ---------------------------------------------------------------------------


def test_with_insights_returns_base_when_insights_is_none() -> None:
    assert _with_insights("base prompt", None) == "base prompt"


def test_with_insights_returns_base_when_insights_is_whitespace() -> None:
    assert _with_insights("base prompt", "   \n  ") == "base prompt"


def test_with_insights_prepends_when_insights_is_substantive() -> None:
    out = _with_insights("base prompt", "context")
    assert out.startswith("context")
    assert "base prompt" in out


# ---------------------------------------------------------------------------
# Critic _build_prompt — dossier truncation branch.
# ---------------------------------------------------------------------------


def _huge_dossier() -> ProspectDossier:
    """Build a dossier whose JSON exceeds the 12_000-char cap."""
    big_text = "lorem ipsum " * 2000  # ~24k chars
    return ProspectDossier(
        prospect_id="prs_big",
        full_name="Jane",
        current_title="VP",
        current_company="Acme",
        executive_summary=big_text,
    )


def _sequence(prospect: Prospect, dossier_id: str) -> OutreachSequence:
    return OutreachSequence(
        prospect=prospect,
        dossier_id=dossier_id,
        dossier_confidence=0.8,
        variants=[
            OutreachVariant(
                angle="company_soft_opener",
                personalization_grade="fallback",
                email_sequence=[EmailTouch(day=1, subject_line="s", body="b")],
            )
        ],
    )


def test_outreach_critic_truncates_oversize_dossier_in_prompt() -> None:
    prospect = Prospect(id="prs_x", company_name="Acme")
    dossier = _huge_dossier()
    icp = IdealCustomerProfile(industry=["SaaS"])
    seq = _sequence(prospect, dossier.dossier_id or "d_x")
    llm = _CannedLLM(
        [{"status": "PASS", "approved": True, "violations": [], "rubric_version": "v1"}]
    )
    critic = OutreachCriticAgent(llm_client=llm)
    critic.review(seq, dossier, icp)
    # The critic's prompt should contain the truncation marker because the
    # dossier JSON exceeds the cap.
    assert any("dossier truncated" in (c["prompt"] or "") for c in llm.calls)
    # And the prompt should be capped near _DOSSIER_CHAR_CAP for the dossier slice.
    assert _DOSSIER_CHAR_CAP < len(llm.calls[0]["prompt"])  # full prompt > cap, sanity


def test_outreach_critic_emits_no_dossier_marker_when_dossier_none() -> None:
    """If the outreach critic is called without a dossier, the prompt
    embeds the explicit '(no dossier supplied)' marker."""
    prospect = Prospect(id="prs_x", company_name="Acme")
    icp = IdealCustomerProfile(industry=["SaaS"])
    seq = _sequence(prospect, "dsr_x")
    llm = _CannedLLM(
        [{"status": "PASS", "approved": True, "violations": [], "rubric_version": "v1"}]
    )
    critic = OutreachCriticAgent(llm_client=llm)
    critic.review(seq, None, icp)
    assert any("(no dossier supplied)" in (c["prompt"] or "") for c in llm.calls)


def test_proposal_critic_truncates_oversize_dossier_in_prompt() -> None:
    prospect = Prospect(id="prs_p", company_name="Acme")
    dossier = _huge_dossier()
    proposal = SalesProposal(
        prospect=prospect,
        executive_summary="s",
        situation_analysis="s",
        proposed_solution="s",
        roi_model=ROIModel(
            annual_cost_usd=1.0,
            estimated_annual_benefit_usd=1.0,
            payback_months=12.0,
            roi_percentage=0.0,
        ),
    )
    qual = QualificationScore(
        prospect=prospect,
        bant=BANTScore(budget=5, authority=5, need=5, timeline=5),
        meddic=MEDDICScore(),
        overall_score=0.5,
        value_creation_level=2,
        recommended_action="advance",
    )
    llm = _CannedLLM(
        [{"status": "PASS", "approved": True, "violations": [], "rubric_version": "v1"}]
    )
    critic = ProposalCriticAgent(llm_client=llm)
    critic.review(proposal, dossier, qual)
    assert any("dossier truncated" in (c["prompt"] or "") for c in llm.calls)


# ---------------------------------------------------------------------------
# ProposalCriticAgent invariant override (line 131).
# ---------------------------------------------------------------------------


def test_proposal_critic_overrides_approved_when_model_lies() -> None:
    """The model says approved=True but lists a must_fix violation — the
    critic must flip ``approved`` to False."""
    prospect = Prospect(id="prs_p", company_name="Acme")
    proposal = SalesProposal(
        prospect=prospect,
        executive_summary="s",
        situation_analysis="s",
        proposed_solution="s",
        roi_model=ROIModel(
            annual_cost_usd=1.0,
            estimated_annual_benefit_usd=1.0,
            payback_months=12.0,
            roi_percentage=0.0,
        ),
    )
    qual = QualificationScore(
        prospect=prospect,
        bant=BANTScore(budget=5, authority=5, need=5, timeline=5),
        meddic=MEDDICScore(),
        overall_score=0.5,
        value_creation_level=2,
        recommended_action="advance",
    )
    bogus = {
        "status": "PASS",
        "approved": True,
        "violations": [
            {
                "rule_id": "proposal.roi.arithmetic",
                "severity": "must_fix",
                "description": "off by 50%",
                "suggested_fix": "recompute",
            }
        ],
        "rubric_version": "v1",
    }
    critic = ProposalCriticAgent(llm_client=_CannedLLM([bogus]))
    report = critic.review(proposal, None, qual)
    assert report.approved is False


# ---------------------------------------------------------------------------
# format_critic_feedback — notes branch.
# ---------------------------------------------------------------------------


def test_format_critic_feedback_appends_notes_when_provided() -> None:
    violations = [
        CriticViolation(
            rule_id="r",
            severity="must_fix",
            description="d",
            suggested_fix="f",
        )
    ]
    out = format_critic_feedback(violations, notes="LLM hint: bound CTA to dossier")
    assert "Critic notes:" in out
    assert "LLM hint" in out
