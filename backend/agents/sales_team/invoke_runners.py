"""Invoke-shim entrypoints for sales team agents.

Each function accepts a raw ``body`` dict from the Agent Console sandbox
dispatcher and delegates to the corresponding agent's domain method.
The dispatcher calls plain functions directly (no ``make_*`` factory or
class instantiation needed).
"""

from __future__ import annotations

from typing import Any

from .agents import (
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
from .models import (
    CloserRequest,
    CoachingRequest,
    DecisionMakerMapperRequest,
    DiscoveryRequest,
    DossierRequest,
    NurtureRequest,
    OutreachRequest,
    ProposalRequest,
    ProspectDossier,
    ProspectingRequest,
    QualificationRequest,
)


def invoke_prospector(body: dict[str, Any]) -> dict[str, Any]:
    req = ProspectingRequest(**body)
    agent = ProspectorAgent()
    result = agent.prospect(
        icp_json=req.icp.model_dump_json(),
        product_name=req.product_name,
        value_proposition=req.value_proposition,
        max_prospects=req.max_prospects,
        company_context=req.company_context,
    )
    return result.model_dump(mode="json")


def invoke_decision_maker_mapper(body: dict[str, Any]) -> dict[str, Any]:
    req = DecisionMakerMapperRequest(**body)
    agent = DecisionMakerMapperAgent()
    result = agent.map_contacts(
        company_json=req.company.model_dump_json(),
        icp_json=req.icp.model_dump_json(),
        product_name=req.product_name,
        value_proposition=req.value_proposition,
        max_contacts=req.max_contacts,
    )
    return result.model_dump(mode="json")


def invoke_dossier_builder(body: dict[str, Any]) -> dict[str, Any]:
    req = DossierRequest(**body)
    agent = DossierBuilderAgent()
    result = agent.build(
        prospect_json=req.prospect.model_dump_json(),
        product_name=req.product_name,
        value_proposition=req.value_proposition,
    )
    return result.model_dump(mode="json")


def invoke_outreach(body: dict[str, Any]) -> dict[str, Any]:
    req = OutreachRequest(**body)
    if not req.prospects:
        raise ValueError("OutreachRequest.prospects must not be empty")
    agent = OutreachAgent()
    case_studies = "\n".join(req.case_study_snippets)
    per_prospect_results: list[dict[str, Any]] = []
    for prospect in req.prospects:
        dossier = ProspectDossier(
            prospect_id=prospect.id or "unknown",
            full_name=prospect.contact_name or prospect.company_name,
            current_title=prospect.contact_title or "Unknown",
            current_company=prospect.company_name,
            executive_summary="Stub dossier for sandbox invoke — no deep research performed.",
        )
        result = agent.generate_sequence(
            prospect_json=prospect.model_dump_json(),
            dossier=dossier,
            product_name=req.product_name,
            value_proposition=req.value_proposition,
            case_studies=case_studies,
            company_context=req.company_context,
        )
        per_prospect_results.append(
            {
                "prospect_id": prospect.id,
                "prospect_company": prospect.company_name,
                "variants": result.model_dump(mode="json")["variants"],
            }
        )
    return {"results": per_prospect_results}


def invoke_qualifier(body: dict[str, Any]) -> dict[str, Any]:
    req = QualificationRequest(**body)
    agent = LeadQualifierAgent()
    result = agent.qualify(
        prospect_json=req.prospect.model_dump_json(),
        product_name=req.product_name,
        value_proposition=req.value_proposition,
        call_notes=req.call_notes,
    )
    return result.model_dump(mode="json")


def invoke_nurture(body: dict[str, Any]) -> dict[str, Any]:
    req = NurtureRequest(**body)
    if not req.prospects:
        raise ValueError("NurtureRequest.prospects must not be empty")
    agent = NurtureAgent()
    per_prospect_results: list[dict[str, Any]] = []
    for prospect in req.prospects:
        result = agent.build_sequence(
            prospect_json=prospect.model_dump_json(),
            product_name=req.product_name,
            value_proposition=req.value_proposition,
            duration_days=req.duration_days,
        )
        dumped = result.model_dump(mode="json")
        dumped["prospect_id"] = prospect.id
        dumped["prospect_company"] = prospect.company_name
        per_prospect_results.append(dumped)
    return {"duration_days": req.duration_days, "results": per_prospect_results}


def invoke_discovery(body: dict[str, Any]) -> dict[str, Any]:
    req = DiscoveryRequest(**body)
    agent = DiscoveryAgent()
    result = agent.prepare(
        prospect_json=req.prospect.model_dump_json(),
        qualification_json=req.qualification.model_dump_json(),
        product_name=req.product_name,
        value_proposition=req.value_proposition,
    )
    return result.model_dump(mode="json")


def invoke_proposal(body: dict[str, Any]) -> dict[str, Any]:
    req = ProposalRequest(**body)
    agent = ProposalAgent()
    result = agent.write(
        prospect_json=req.prospect.model_dump_json(),
        product_name=req.product_name,
        value_proposition=req.value_proposition,
        annual_cost_usd=req.annual_cost_usd,
        discovery_notes=req.discovery_notes,
        case_studies="\n".join(req.case_study_snippets),
        company_context=req.company_context,
    )
    return result.model_dump(mode="json")


def invoke_closer(body: dict[str, Any]) -> dict[str, Any]:
    req = CloserRequest(**body)
    agent = CloserAgent()
    result = agent.develop_strategy(
        prospect_json=req.prospect.model_dump_json(),
        proposal_json=req.proposal.model_dump_json(),
        product_name=req.product_name,
        value_proposition=req.value_proposition,
        dossier=req.dossier,
    )
    return result.model_dump(mode="json")


def invoke_coach(body: dict[str, Any]) -> dict[str, Any]:
    req = CoachingRequest(**body)
    agent = SalesCoachAgent()
    prospects_json = "[" + ", ".join(p.model_dump_json() for p in req.prospects) + "]"
    result = agent.review(
        prospects_json=prospects_json,
        product_name=req.product_name,
        pipeline_context=req.pipeline_context,
    )
    return result.model_dump(mode="json")
