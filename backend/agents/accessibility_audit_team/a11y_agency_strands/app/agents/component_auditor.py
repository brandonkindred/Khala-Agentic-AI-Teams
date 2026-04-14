from ..models import Finding
from ..models.phase_result import ComponentAuditResult
from ..tools import build_component_inventory, persist_artifact, run_axe_scan
from .base import ToolContext, a11y_phase


@a11y_phase(context=True)
def run_component_audit(component_id: str, tool_context: ToolContext) -> dict:
    inventory = build_component_inventory([{"component_id": component_id, "complexity": "high"}])
    run_axe_scan(component_id)
    # TODO: Phase 3 — replace with strands.Agent
    finding = Finding.model_validate(
        {
            "finding_id": f"cmp-{component_id}",
            "title": "Component requires focus-visible treatment",
            "severity": "high",
            "wcag_reference": "2.4.7",
            "target": component_id,
            "remediation": "Apply visible focus indicators for keyboard users.",
        }
    )
    artifact = persist_artifact(
        f"{tool_context.invocation_state['artifact_root']}/component_{component_id}.json",
        {
            "inventory": inventory,
            "finding": finding.model_dump(),
        },
    )
    return ComponentAuditResult(
        artifact=artifact,
        finding_id=finding.finding_id,
    ).model_dump()
