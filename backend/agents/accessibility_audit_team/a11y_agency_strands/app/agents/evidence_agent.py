from ..models import EvidenceBundle
from ..models.phase_result import EvidenceResult
from ..tools import capture_dom_snippet, capture_screenshot, persist_artifact
from .base import ToolContext, tool


@tool(context=True)
def run_evidence_curation(finding_id: str, target: str, tool_context: ToolContext) -> dict:
    bundle = EvidenceBundle(
        finding_id=finding_id,
        screenshot_path=capture_screenshot(target),
        dom_snippet=capture_dom_snippet(target),
        user_impact="Blocks keyboard users from completing checkout.",
        remediation_suggestion="Fix focus order and add visible focus styling.",
        wcag_reference="2.4.3",
    )
    artifact = persist_artifact(
        f"{tool_context.invocation_state['artifact_root']}/evidence_{finding_id}.json",
        bundle.model_dump(),
    )
    return EvidenceResult(artifact=artifact, finding_id=finding_id).model_dump()
