from .base import StubAgent, ToolContext, tool
from ..models import Finding
from ..tools import build_component_inventory, persist_artifact, run_axe_scan


@tool(context=True)
def run_component_audit(component_id: str, tool_context: ToolContext) -> dict:
    inventory = build_component_inventory([{"component_id": component_id, "complexity": "high"}])
    run_axe_scan(component_id)
    specialist = StubAgent(name="component_auditor")
    finding = specialist.invoke(
        {
            "finding_id": f"cmp-{component_id}",
            "title": "Component requires focus-visible treatment",
            "severity": "high",
            "wcag_reference": "2.4.7",
            "target": component_id,
            "remediation": "Apply visible focus indicators for keyboard users.",
        },
        structured_output_model=Finding,
    )
    artifact = persist_artifact(f"{tool_context.invocation_state['artifact_root']}/component_{component_id}.json", {
        "inventory": inventory,
        "finding": finding.model_dump(),
    })
    return {"phase": "component_audit", "artifact": artifact, "finding_id": finding.finding_id}
