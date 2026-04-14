from ..models.phase_result import RetestResult
from ..tools import persist_artifact
from .base import ToolContext, a11y_phase


@a11y_phase(context=True)
def run_retest_cycle(engagement_id: str, tool_context: ToolContext) -> dict:
    output = {
        "engagement_id": engagement_id,
        "closed": [],
        "open": [],
        "monitoring_recommendation": "Run monthly checks",
    }
    artifact = persist_artifact(
        f"{tool_context.invocation_state['artifact_root']}/retest.json", output
    )
    return RetestResult(artifact=artifact).model_dump()
