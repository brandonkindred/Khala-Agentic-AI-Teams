"""Independent LLM-as-judge pass over a generated :class:`OutreachSequence`.

Runs against an outreach rubric the Pydantic validators can't enforce —
fabricated emails, missing CTAs, bloated subject lines, leftover template
tokens, ICP misalignment. The orchestrator wraps the critic in a one-shot
refinement loop: emit -> critic -> on revise, re-emit with violations.

The critic uses :func:`llm_service.complete_validated` so the same role-keyed
client and self-correction guard the rest of the sales pod relies on apply
here too. Tests inject a ``CannedLLMClient`` via ``llm_client=...`` so this
file stays Strands-free and runs in CI without a network.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import List, Optional

from llm_service import LLMClient, complete_validated

from ..llm import get_sales_llm_client
from ..models import (
    CriticViolation,
    IdealCustomerProfile,
    OutreachCriticReport,
    OutreachSequence,
    ProspectDossier,
)

logger = logging.getLogger(__name__)

_DOSSIER_CHAR_CAP = 12_000


_OUTREACH_CRITIC_SYSTEM_PROMPT = """\
You are an independent Sales Outreach Reviewer. You did NOT write the sequence \
under review; your job is to score it against the rubric below and the \
authoritative dossier + ICP supplied by the user.

REJECT the sequence (status=FAIL) if ANY must_fix violation is present. For \
every violation, emit a CriticViolation with a concrete suggested_fix the \
agent can apply on retry.

RUBRIC (use these rule_id slugs verbatim):

 1. outreach.citation.fabricated — every non-`fallback` variant whose \
    `email_sequence[0]` includes a personalization claim must cite at least \
    one source URL that appears in the supplied dossier `sources` allowlist. \
    Cited URLs that aren't in the allowlist FAIL.

 2. outreach.email.contact_address — `prospect.contact_email` should be \
    `null` unless it appears verbatim in the dossier's `personal_site`, \
    `executive_summary`, `notes`, or `other_social` fields. Pattern \
    fabrications like `firstname.lastname@company.com` with no dossier \
    backing FAIL. The dossier `sources` field is a list of URLs and never \
    contains an email address — do not check there.

 3. outreach.day1.cta — the first email in every variant's email_sequence \
    must contain a clear call-to-action: a specific question, a meeting ask, \
    or a link. "Let me know if interested" alone is not a CTA.

 4. outreach.day1.subject_length — the first email subject line must be \
    60 characters or fewer.

 5. outreach.forbidden_tokens — no leftover template placeholders. FAIL on \
    any of: `{first_name}`, `{firstName}`, `[FIRST_NAME]`, `[COMPANY]`, \
    `[NAME]`, `<NAME>`, `REPLACE_ME`, `TODO`, `XXXX`, `Lorem ipsum`.

 6. outreach.personalization.icp_alignment — at least one variant must tie \
    the product to a concrete ICP `pain_point`. Generic value-prop \
    restatement does not qualify.

OUTPUT contract:
 - Output a SINGLE JSON object matching this schema:
   {
     "status": "PASS" | "FAIL",
     "approved": true | false,
     "violations": [
       {
         "rule_id": "<rubric rule_id>",
         "severity": "must_fix" | "should_fix" | "consider",
         "section": "<path within sequence, e.g. 'variants[0].day1' or 'overall'>",
         "evidence_quote": "<quoted offending text under ~120 chars>",
         "description": "<what is wrong and why>",
         "suggested_fix": "<concrete instruction for the agent>"
       },
       ...
     ],
     "personalization_grade_override": "high" | "medium" | "low" | "fallback" | null,
     "notes": "<optional short note>",
     "rubric_version": "v1"
   }
 - `status` is PASS only when no must_fix violations exist. `approved` must \
   equal (status == "PASS").
 - Return JSON only. No markdown fences. No prose outside the object.
"""


@dataclass
class OutreachCriticAgent:
    """Reviews a generated :class:`OutreachSequence` against the outreach rubric."""

    llm_client: Optional[LLMClient] = None
    role: str = "Sales Outreach Reviewer"
    _llm: LLMClient = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._llm = self.llm_client or get_sales_llm_client("outreach_critic")

    def review(
        self,
        sequence: OutreachSequence,
        dossier: Optional[ProspectDossier],
        icp: IdealCustomerProfile,
    ) -> OutreachCriticReport:
        """Evaluate ``sequence`` and return a :class:`OutreachCriticReport`.

        On any LLM exception (parse failure after corrective retries, network
        error, schema rejection) the critic returns a fail-closed FAIL report
        so the orchestrator's one-shot refinement budget gets used.
        """
        prompt = self._build_prompt(sequence, dossier, icp)
        try:
            report = complete_validated(
                self._llm,
                prompt,
                schema=OutreachCriticReport,
                system_prompt=_OUTREACH_CRITIC_SYSTEM_PROMPT,
                temperature=0.0,
                correction_attempts=2,
            )
        except Exception as exc:
            logger.warning("sales.outreach_critic.failed reason=%s", exc)
            return _fallback_outreach_report(str(exc))

        # Enforce the invariant: approved iff status == PASS with no must_fix items.
        approved = report.status == "PASS" and report.must_fix_count() == 0
        if approved != report.approved:
            report = report.model_copy(update={"approved": approved})
        return report

    @staticmethod
    def _build_prompt(
        sequence: OutreachSequence,
        dossier: Optional[ProspectDossier],
        icp: IdealCustomerProfile,
    ) -> str:
        sequence_json = json.dumps(sequence.model_dump(mode="json"), indent=2)
        if dossier is not None:
            dossier_json = json.dumps(dossier.model_dump(mode="json"), indent=2)
            if len(dossier_json) > _DOSSIER_CHAR_CAP:
                dossier_json = dossier_json[:_DOSSIER_CHAR_CAP] + "\n…(dossier truncated)"
        else:
            dossier_json = "(no dossier supplied)"
        icp_json = json.dumps(icp.model_dump(mode="json"), indent=2)
        return (
            "--- ICP ---\n"
            f"{icp_json}\n\n"
            "--- DOSSIER ---\n"
            f"{dossier_json}\n\n"
            "--- OUTREACH SEQUENCE (JSON) ---\n"
            f"{sequence_json}\n\n"
            "--- TASK ---\n"
            "Evaluate the outreach sequence against the rubric in your system "
            "prompt. Return a single OutreachCriticReport JSON object only."
        )


def _fallback_outreach_report(reason: str) -> OutreachCriticReport:
    """Fail-closed report for when the critic LLM cannot produce a valid response."""
    return OutreachCriticReport(
        status="FAIL",
        approved=False,
        violations=[],
        notes=(
            "Outreach critic could not produce parseable JSON; treating as FAIL "
            "so the refine loop continues. Reason: " + reason
        ),
    )


def format_critic_feedback(violations: List[CriticViolation], notes: Optional[str] = None) -> str:
    """Render the critic's violations as plain text the agent can ingest on retry.

    Sorted by severity (must_fix first) so the refiner can't miss the blockers.
    Shared between :class:`OutreachCriticAgent` and :class:`ProposalCriticAgent`
    because both reports use the same :class:`CriticViolation` model.
    """
    if not violations:
        return notes or "Critic rejected the artifact but did not list violations."

    severity_order = {"must_fix": 0, "should_fix": 1, "consider": 2}
    ordered = sorted(violations, key=lambda v: (severity_order.get(v.severity, 3), v.rule_id))

    lines: List[str] = [
        "An independent reviewer rejected the previous output.",
        "Address every must_fix violation; resolve should_fix items where possible.",
        "",
    ]
    for v in ordered:
        where = f"[{v.section}] " if v.section else ""
        evidence = f'\n   evidence: "{v.evidence_quote}"' if v.evidence_quote else ""
        lines.append(
            f"- {v.severity.upper()} {where}{v.rule_id}: {v.description}{evidence}\n"
            f"   fix: {v.suggested_fix}"
        )
    if notes:
        lines.append("")
        lines.append(f"Critic notes: {notes}")
    return "\n".join(lines)
