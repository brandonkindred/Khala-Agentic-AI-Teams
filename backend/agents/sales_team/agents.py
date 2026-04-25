"""AI agent implementations for the Sales Team pod.

Each agent wraps a methodology-rich system prompt grounded in:
- Gong Labs Blog (pipeline velocity, talk/listen ratios, deal risk signals)
- Jeb Blount (Fanatical Prospecting, Sales EQ, objection handling)
- HubSpot Sales Blog (lead scoring, nurture sequences, inbound methodology)
- Anthony Iannarino (Level 1-4 Value Creation, sales-specific advisory selling)
- Jill Konrath (SNAP Selling, SPIN framework application)
- Sales Hacker Blog (modern cadence frameworks, tech-stack prospecting)
- Salesfolk (hyper-personalized cold email copy)
- Zig Ziglar (classic closing techniques: assumptive, summary, urgency, etc.)

Every agent calls the shared ``llm_service`` layer through
``complete_validated`` so responses are Pydantic-typed with self-correction on
JSON / schema failures, and every call is tagged with its own
``sales.<role>`` agent key for model overrides and telemetry.

System prompts and task templates live in :mod:`sales_team.prompts` — one
module per specialist. This file holds only the orchestration glue.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from llm_service import LLMClient, complete_validated

from .llm import get_sales_llm_client
from .models import (
    PERSONALIZATION_CONFIDENCE_THRESHOLD,
    ClosingStrategyBody,
    DecisionMakerList,
    DiscoveryPlanBody,
    NurtureSequenceBody,
    OutreachVariantList,
    PipelineCoachingReport,
    ProspectDossier,
    ProspectList,
    QualificationScoreBody,
    SalesProposalBody,
)
from .prompts import (
    CLOSER_SYSTEM_PROMPT,
    CLOSER_TASK_TEMPLATE,
    COACH_SYSTEM_PROMPT,
    COACH_TASK_TEMPLATE,
    DECISION_MAKER_MAPPER_SYSTEM_PROMPT,
    DECISION_MAKER_MAPPER_TASK_TEMPLATE,
    DISCOVERY_SYSTEM_PROMPT,
    DISCOVERY_TASK_TEMPLATE,
    DOSSIER_BUILDER_SYSTEM_PROMPT,
    DOSSIER_BUILDER_TASK_TEMPLATE,
    NURTURE_SYSTEM_PROMPT,
    NURTURE_TASK_TEMPLATE,
    OUTREACH_SYSTEM_PROMPT,
    OUTREACH_TASK_TEMPLATE,
    PROPOSAL_SYSTEM_PROMPT,
    PROPOSAL_TASK_TEMPLATE,
    PROSPECT_COMPANIES_TASK_TEMPLATE,
    PROSPECT_TASK_TEMPLATE,
    PROSPECTOR_SYSTEM_PROMPT,
    QUALIFIER_SYSTEM_PROMPT,
    QUALIFIER_TASK_TEMPLATE,
)
from .prompts._dossier_render import render_dossier_for_prompt

logger = logging.getLogger(__name__)


def _with_insights(base_prompt: str, insights_context: Optional[str]) -> str:
    """Prepend learned-pattern context to a prompt when available."""
    if not insights_context or not insights_context.strip():
        return base_prompt
    return f"{insights_context}\n\n---\n\n{base_prompt}"


def _invoke(client: LLMClient, prompt: str, schema: type, system_prompt: str, **extra: Any):
    """Shared call into ``complete_validated`` with the sales-team defaults."""
    return complete_validated(
        client,
        prompt,
        schema=schema,
        system_prompt=system_prompt,
        temperature=0.0,
        correction_attempts=2,
        **extra,
    )


@dataclass
class ProspectorAgent:
    """SDR: identifies and researches prospects matching the ICP.

    Grounded in Jeb Blount's Fanatical Prospecting and Sales Hacker ICP frameworks.
    """

    llm_client: Optional[LLMClient] = None
    role: str = "Prospector (SDR)"
    _llm: LLMClient = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._llm = self.llm_client or get_sales_llm_client("prospector")

    def prospect(
        self,
        icp_json: str,
        product_name: str,
        value_proposition: str,
        max_prospects: int,
        company_context: str,
        insights_context: Optional[str] = None,
    ) -> ProspectList:
        prompt = _with_insights(
            PROSPECT_TASK_TEMPLATE.format(
                product_name=product_name,
                value_proposition=value_proposition,
                company_context=company_context,
                icp_json=icp_json,
                max_prospects=max_prospects,
            ),
            insights_context,
        )
        return _invoke(self._llm, prompt, ProspectList, PROSPECTOR_SYSTEM_PROMPT)

    def prospect_companies(
        self,
        icp_json: str,
        product_name: str,
        value_proposition: str,
        max_companies: int,
        company_context: str,
        insights_context: Optional[str] = None,
    ) -> ProspectList:
        """Return a ranked list of *companies* (not individual contacts).

        Used by the deep-research pipeline as the first stage: we first build
        the account list, then map decision-makers into each account, then
        build dossiers per decision-maker.

        Each returned Prospect carries company-level data only: company_name,
        website, industry, company_size_estimate, icp_match_score,
        research_notes, trigger_events. contact_* fields are null.
        """
        prompt = _with_insights(
            PROSPECT_COMPANIES_TASK_TEMPLATE.format(
                product_name=product_name,
                value_proposition=value_proposition,
                company_context=company_context,
                icp_json=icp_json,
                max_companies=max_companies,
            ),
            insights_context,
        )
        return _invoke(self._llm, prompt, ProspectList, PROSPECTOR_SYSTEM_PROMPT)


@dataclass
class DecisionMakerMapperAgent:
    """Given a company + ICP, returns named decision-makers at that company.

    Used by the deep-research pipeline after the company shortlist is built.
    """

    llm_client: Optional[LLMClient] = None
    role: str = "Account Researcher (Decision-Maker Mapper)"
    _llm: LLMClient = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._llm = self.llm_client or get_sales_llm_client("decision_maker_mapper")

    def map_contacts(
        self,
        company_json: str,
        icp_json: str,
        product_name: str,
        value_proposition: str,
        max_contacts: int = 2,
        insights_context: Optional[str] = None,
    ) -> DecisionMakerList:
        prompt = _with_insights(
            DECISION_MAKER_MAPPER_TASK_TEMPLATE.format(
                product_name=product_name,
                value_proposition=value_proposition,
                company_json=company_json,
                icp_json=icp_json,
                max_contacts=max_contacts,
            ),
            insights_context,
        )
        return _invoke(self._llm, prompt, DecisionMakerList, DECISION_MAKER_MAPPER_SYSTEM_PROMPT)


@dataclass
class DossierBuilderAgent:
    """Given one named prospect, builds a full :class:`ProspectDossier`."""

    llm_client: Optional[LLMClient] = None
    role: str = "Sales Research Analyst (Dossier Builder)"
    _llm: LLMClient = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._llm = self.llm_client or get_sales_llm_client("dossier_builder")

    def build(
        self,
        prospect_json: str,
        product_name: str,
        value_proposition: str,
        insights_context: Optional[str] = None,
    ) -> ProspectDossier:
        prompt = _with_insights(
            DOSSIER_BUILDER_TASK_TEMPLATE.format(
                product_name=product_name,
                value_proposition=value_proposition,
                prospect_json=prospect_json,
            ),
            insights_context,
        )
        return _invoke(self._llm, prompt, ProspectDossier, DOSSIER_BUILDER_SYSTEM_PROMPT)


@dataclass
class OutreachAgent:
    """SDR/BDR: crafts hyper-personalized cold outreach sequences.

    Grounded in Salesfolk, Jill Konrath SNAP, and Jeb Blount's 6-touch cadence.
    Requires a ProspectDossier per call — every personalization claim in the
    generated copy must trace back to a cited dossier field.
    """

    llm_client: Optional[LLMClient] = None
    role: str = "Outreach Specialist (SDR/BDR)"
    _llm: LLMClient = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._llm = self.llm_client or get_sales_llm_client("outreach")

    def generate_sequence(
        self,
        prospect_json: str,
        dossier: ProspectDossier,
        product_name: str,
        value_proposition: str,
        case_studies: str,
        company_context: str,
        insights_context: Optional[str] = None,
        variant_count: int = 3,
    ) -> OutreachVariantList:
        """Generate the raw variant list for a prospect.

        Citation verification and grade enforcement happen inside the Pydantic
        validators on :class:`EmailTouch` and :class:`OutreachVariant`, driven
        by ``context={"dossier_source_urls": ...}``. The confidence-gate rule
        (``dossier.confidence < PERSONALIZATION_CONFIDENCE_THRESHOLD`` →
        company_soft_opener only) is enforced by the orchestrator when it
        wraps the result into an ``OutreachSequence``.
        """
        dossier_block = render_dossier_for_prompt(dossier)
        prompt = _with_insights(
            OUTREACH_TASK_TEMPLATE.format(
                personalization_confidence_threshold=PERSONALIZATION_CONFIDENCE_THRESHOLD,
                dossier_block=dossier_block,
                variant_count=variant_count,
                prospect_json=prospect_json,
                product_name=product_name,
                value_proposition=value_proposition,
                company_context=company_context,
                case_studies=case_studies,
            ),
            insights_context,
        )
        context: dict[str, Any] = {
            "dossier_source_urls": set(dossier.sources or []),
            "citations_stripped": False,
        }
        return _invoke(
            self._llm, prompt, OutreachVariantList, OUTREACH_SYSTEM_PROMPT, context=context
        )


@dataclass
class LeadQualifierAgent:
    """BDR: scores leads using BANT, MEDDIC, and Iannarino's value tiers.

    Grounded in Anthony Iannarino and HubSpot lead scoring methodology.
    """

    llm_client: Optional[LLMClient] = None
    role: str = "Lead Qualifier (BDR)"
    _llm: LLMClient = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._llm = self.llm_client or get_sales_llm_client("qualifier")

    def qualify(
        self,
        prospect_json: str,
        product_name: str,
        value_proposition: str,
        call_notes: str,
        insights_context: Optional[str] = None,
    ) -> QualificationScoreBody:
        prompt = _with_insights(
            QUALIFIER_TASK_TEMPLATE.format(
                product_name=product_name,
                prospect_json=prospect_json,
                value_proposition=value_proposition,
                call_notes=call_notes or "None yet",
            ),
            insights_context,
        )
        return _invoke(self._llm, prompt, QualificationScoreBody, QUALIFIER_SYSTEM_PROMPT)


@dataclass
class NurtureAgent:
    """AM: builds long-cycle nurture sequences for leads not ready to buy.

    Grounded in HubSpot inbound methodology and Gong Labs optimal cadence research.
    """

    llm_client: Optional[LLMClient] = None
    role: str = "Nurture Specialist (AM)"
    _llm: LLMClient = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._llm = self.llm_client or get_sales_llm_client("nurture")

    def build_sequence(
        self,
        prospect_json: str,
        product_name: str,
        value_proposition: str,
        duration_days: int,
        insights_context: Optional[str] = None,
    ) -> NurtureSequenceBody:
        prompt = _with_insights(
            NURTURE_TASK_TEMPLATE.format(
                duration_days=duration_days,
                prospect_json=prospect_json,
                product_name=product_name,
                value_proposition=value_proposition,
            ),
            insights_context,
        )
        return _invoke(self._llm, prompt, NurtureSequenceBody, NURTURE_SYSTEM_PROMPT)


@dataclass
class DiscoveryAgent:
    """AE: prepares discovery call guides and demo agendas.

    Grounded in SPIN Selling (Jill Konrath), the Challenger Sale, and Gong Labs discovery research.
    """

    llm_client: Optional[LLMClient] = None
    role: str = "Discovery & Demo Specialist (AE)"
    _llm: LLMClient = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._llm = self.llm_client or get_sales_llm_client("discovery")

    def prepare(
        self,
        prospect_json: str,
        qualification_json: str,
        product_name: str,
        value_proposition: str,
        insights_context: Optional[str] = None,
    ) -> DiscoveryPlanBody:
        prompt = _with_insights(
            DISCOVERY_TASK_TEMPLATE.format(
                prospect_json=prospect_json,
                qualification_json=qualification_json,
                product_name=product_name,
                value_proposition=value_proposition,
            ),
            insights_context,
        )
        return _invoke(self._llm, prompt, DiscoveryPlanBody, DISCOVERY_SYSTEM_PROMPT)


@dataclass
class ProposalAgent:
    """AE: generates structured, ROI-driven sales proposals.

    Grounded in Anthony Iannarino's Level-4 Value Creation proposal methodology.
    """

    llm_client: Optional[LLMClient] = None
    role: str = "Proposal Writer (AE)"
    _llm: LLMClient = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._llm = self.llm_client or get_sales_llm_client("proposal")

    def write(
        self,
        prospect_json: str,
        product_name: str,
        value_proposition: str,
        annual_cost_usd: float,
        discovery_notes: str,
        case_studies: str,
        company_context: str,
        insights_context: Optional[str] = None,
    ) -> SalesProposalBody:
        prompt = _with_insights(
            PROPOSAL_TASK_TEMPLATE.format(
                prospect_json=prospect_json,
                product_name=product_name,
                value_proposition=value_proposition,
                annual_cost_usd=annual_cost_usd,
                discovery_notes=discovery_notes or "See prospect research notes",
                case_studies=case_studies,
                company_context=company_context,
            ),
            insights_context,
        )
        return _invoke(self._llm, prompt, SalesProposalBody, PROPOSAL_SYSTEM_PROMPT)


@dataclass
class CloserAgent:
    """AE: develops closing strategies and objection handlers.

    Grounded in Zig Ziglar's closing techniques and Jeb Blount's Sales EQ.
    """

    llm_client: Optional[LLMClient] = None
    role: str = "Closer (AE)"
    _llm: LLMClient = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._llm = self.llm_client or get_sales_llm_client("closer")

    def develop_strategy(
        self,
        prospect_json: str,
        proposal_json: str,
        product_name: str,
        value_proposition: str,
        insights_context: Optional[str] = None,
    ) -> ClosingStrategyBody:
        prompt = _with_insights(
            CLOSER_TASK_TEMPLATE.format(
                prospect_json=prospect_json,
                proposal_json=proposal_json,
                product_name=product_name,
                value_proposition=value_proposition,
            ),
            insights_context,
        )
        return _invoke(self._llm, prompt, ClosingStrategyBody, CLOSER_SYSTEM_PROMPT)


@dataclass
class SalesCoachAgent:
    """Sales Manager: reviews the pipeline and provides Gong-style coaching.

    Grounded in Gong Labs research, pipeline velocity metrics, and Iannarino's coaching framework.
    """

    llm_client: Optional[LLMClient] = None
    role: str = "Sales Coach (Manager)"
    _llm: LLMClient = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._llm = self.llm_client or get_sales_llm_client("coach")

    def review(
        self,
        prospects_json: str,
        product_name: str,
        pipeline_context: str,
        insights_context: Optional[str] = None,
    ) -> PipelineCoachingReport:
        prompt = _with_insights(
            COACH_TASK_TEMPLATE.format(
                product_name=product_name,
                prospects_json=prospects_json,
                pipeline_context=pipeline_context or "None provided",
            ),
            insights_context,
        )
        return _invoke(self._llm, prompt, PipelineCoachingReport, COACH_SYSTEM_PROMPT)
