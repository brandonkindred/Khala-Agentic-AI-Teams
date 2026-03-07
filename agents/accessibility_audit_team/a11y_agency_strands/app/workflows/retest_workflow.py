from ..agents import EngagementOrchestrator


def run_retest_workflow(orchestrator: EngagementOrchestrator, engagement_id: str) -> dict:
    return orchestrator.run_retest_cycle(engagement_id)
