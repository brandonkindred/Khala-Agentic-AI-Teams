from ..models import Scorecard
from ..models.phase_result import ScoringResult
from ..tools import persist_artifact
from .base import ToolContext, tool


@tool(context=True)
def run_scoring_and_prioritization(engagement_id: str, tool_context: ToolContext) -> dict:
    scorecard = Scorecard(
        component_score=88.0, page_score=82.0, site_score=84.5, priority_score=92.0
    )
    artifact = persist_artifact(
        f"{tool_context.invocation_state['artifact_root']}/scorecard.json",
        scorecard.model_dump(),
    )
    return ScoringResult(
        artifact=artifact,
        site_score=scorecard.site_score,
    ).model_dump()
