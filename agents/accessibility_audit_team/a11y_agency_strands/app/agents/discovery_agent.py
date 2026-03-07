from .base import StubAgent, ToolContext, tool
from ..models import ClientProfile, SamplingPlan, ScopeDefinition
from ..tools import collect_client_discovery, persist_artifact


@tool(context=True)
def run_discovery(raw_answers: dict, tool_context: ToolContext) -> dict:
    source = collect_client_discovery(raw_answers, tool_context.invocation_state.get("questionnaire", {}))
    specialist = StubAgent(name="discovery")
    client = specialist.invoke(
        {
            "organization": source.get("organization", "Unknown"),
            "goals": source.get("goals", []),
            "business_metrics": source.get("business_metrics", []),
        },
        structured_output_model=ClientProfile,
    )
    scope = ScopeDefinition(
        conformance_target=source.get("conformance_target", "WCAG 2.2 AA"),
        legal_requirements=source.get("legal_requirements", ["Section 508"]),
        site_characteristics=source.get("site_characteristics", []),
        timeline_constraints=source.get("timeline_constraints", "TBD"),
    )
    sampling = SamplingPlan(
        tier1_pages=source.get("tier1_pages", []),
        tier2_pages=source.get("tier2_pages", []),
        tier3_pages=source.get("tier3_pages", []),
        priority_journeys=source.get("priority_journeys", []),
    )
    artifact = persist_artifact(f"{tool_context.invocation_state['artifact_root']}/discovery.json", {
        "client_profile": client.model_dump(),
        "scope_definition": scope.model_dump(),
        "sampling_plan": sampling.model_dump(),
    })
    return {"phase": "discovery", "artifact": artifact, "tier1_count": len(sampling.tier1_pages)}
