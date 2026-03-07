from .agents import EngagementOrchestrator
from .config import AgencyConfig
from .workflows import run_engagement_workflow


def run_engagement(engagement_id: str, raw_answers: dict, config: AgencyConfig | None = None) -> dict:
    active = config or AgencyConfig()
    invocation_state = {
        "engagement_id": engagement_id,
        "artifact_root": f"{active.artifact_root}/{engagement_id}",
        "questionnaire": {},
        "approval_mode": "human",
    }
    orchestrator = EngagementOrchestrator(invocation_state=invocation_state)
    return run_engagement_workflow(orchestrator, engagement_id=engagement_id, raw_answers=raw_answers)
