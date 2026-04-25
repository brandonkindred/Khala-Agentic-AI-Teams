"""Independent LLM-as-judge pass over a generated :class:`SalesProposal`.

Today the orchestrator does ``SalesProposalBody`` -> ``SalesProposal`` with no
validation; this critic catches the classes of error a buyer would catch:
broken ROI math, line-item totals that don't reconcile, claims with no
backing case study, and pro-forma "next steps" that aren't really steps.

Same shape as :class:`OutreachCriticAgent` — uses
:func:`llm_service.complete_validated`, fail-closed FAIL on any LLM exception.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Optional

from llm_service import LLMClient, complete_validated

from ..llm import get_sales_llm_client
from ..models import (
    ProposalCriticReport,
    ProspectDossier,
    QualificationScore,
    SalesProposal,
)

logger = logging.getLogger(__name__)

_DOSSIER_CHAR_CAP = 12_000


_PROPOSAL_CRITIC_SYSTEM_PROMPT = """\
You are an independent Sales Proposal Reviewer. You did NOT write the \
proposal under review; your job is to score it against the rubric below \
using the supplied dossier and qualification score as authoritative context.

REJECT the proposal (status=FAIL) if ANY must_fix violation is present. For \
every violation, emit a CriticViolation with a concrete suggested_fix the \
agent can apply on retry.

RUBRIC (use these rule_id slugs verbatim):

 1. proposal.roi.arithmetic — the supplied roi_model numbers must internally \
    reconcile. Compute payback_months = annual_cost_usd * 12 / \
    estimated_annual_benefit_usd (when benefit > 0). If the proposal's \
    payback_months differs by more than 10% from the computed value, FAIL. \
    If estimated_annual_benefit_usd <= 0 but roi_percentage > 0, FAIL.

 2. proposal.investment_table.totals — if the investment_table lists \
    individual line-items with prices, the totals row must equal the sum of \
    line-items. Tolerance ±$1 for rounding. FAIL if missing or off.

 3. proposal.claims.founded — every quantitative claim ("our customers see \
    3x pipeline lift", "average payback under 90 days", "30% reduction in \
    churn") must either be marked as a ROI assumption OR be backed by one \
    of the case studies the agent was given. Generic qualitative claims are \
    OK; quantitative claims with no backing FAIL.

 4. proposal.discovery.referenced — at least one section \
    (situation_analysis, proposed_solution, or executive_summary) must \
    reference a concrete pain or metric from the qualification score's \
    `meddic` block (`identify_pain`, `metrics_identified`) or BANT need. A \
    proposal that ignores the discovery findings FAILs.

 5. proposal.next_steps.concrete — the next_steps list must contain at \
    least one entry that includes either a date / timeframe ("within 7 \
    days", "by 2026-05-15") or an explicit owner ("Buyer:", "Vendor:"). \
    "We will follow up soon" alone FAILs.

OUTPUT contract:
 - Output a SINGLE JSON object matching this schema:
   {
     "status": "PASS" | "FAIL",
     "approved": true | false,
     "violations": [
       {
         "rule_id": "<rubric rule_id>",
         "severity": "must_fix" | "should_fix" | "consider",
         "section": "<path within proposal, e.g. 'roi_model' or 'next_steps' or 'overall'>",
         "evidence_quote": "<quoted offending text under ~120 chars>",
         "description": "<what is wrong and why>",
         "suggested_fix": "<concrete instruction for the agent>"
       },
       ...
     ],
     "notes": "<optional short note>",
     "rubric_version": "v1"
   }
 - `status` is PASS only when no must_fix violations exist. `approved` must \
   equal (status == "PASS").
 - Return JSON only. No markdown fences. No prose outside the object.
"""


@dataclass
class ProposalCriticAgent:
    """Reviews a generated :class:`SalesProposal` against the proposal rubric."""

    llm_client: Optional[LLMClient] = None
    role: str = "Sales Proposal Reviewer"
    _llm: LLMClient = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._llm = self.llm_client or get_sales_llm_client("proposal_critic")

    def review(
        self,
        proposal: SalesProposal,
        dossier: Optional[ProspectDossier],
        qualification: Optional[QualificationScore],
    ) -> ProposalCriticReport:
        """Evaluate ``proposal`` and return a :class:`ProposalCriticReport`."""
        prompt = self._build_prompt(proposal, dossier, qualification)
        try:
            report = complete_validated(
                self._llm,
                prompt,
                schema=ProposalCriticReport,
                system_prompt=_PROPOSAL_CRITIC_SYSTEM_PROMPT,
                temperature=0.0,
                correction_attempts=2,
            )
        except Exception as exc:
            logger.warning("sales.proposal_critic.failed reason=%s", exc)
            return _fallback_proposal_report(str(exc))

        approved = report.status == "PASS" and report.must_fix_count() == 0
        if approved != report.approved:
            report = report.model_copy(update={"approved": approved})
        return report

    @staticmethod
    def _build_prompt(
        proposal: SalesProposal,
        dossier: Optional[ProspectDossier],
        qualification: Optional[QualificationScore],
    ) -> str:
        proposal_json = json.dumps(proposal.model_dump(mode="json"), indent=2)
        if dossier is not None:
            dossier_json = json.dumps(dossier.model_dump(mode="json"), indent=2)
            if len(dossier_json) > _DOSSIER_CHAR_CAP:
                dossier_json = dossier_json[:_DOSSIER_CHAR_CAP] + "\n…(dossier truncated)"
        else:
            dossier_json = "(no dossier supplied)"
        if qualification is not None:
            qual_json = json.dumps(qualification.model_dump(mode="json"), indent=2)
        else:
            qual_json = "(no qualification supplied)"
        return (
            "--- DOSSIER ---\n"
            f"{dossier_json}\n\n"
            "--- QUALIFICATION ---\n"
            f"{qual_json}\n\n"
            "--- PROPOSAL (JSON) ---\n"
            f"{proposal_json}\n\n"
            "--- TASK ---\n"
            "Evaluate the proposal against the rubric in your system prompt. "
            "Compute the ROI arithmetic yourself before flagging or clearing "
            "rule 1. Return a single ProposalCriticReport JSON object only."
        )


def _fallback_proposal_report(reason: str) -> ProposalCriticReport:
    """Fail-closed report for when the critic LLM cannot produce a valid response."""
    return ProposalCriticReport(
        status="FAIL",
        approved=False,
        violations=[],
        notes=(
            "Proposal critic could not produce parseable JSON; treating as FAIL "
            "so the refine loop continues. Reason: " + reason
        ),
    )
