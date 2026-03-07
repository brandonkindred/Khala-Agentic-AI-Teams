from ..agents import EngagementOrchestrator


def run_engagement_workflow(orchestrator: EngagementOrchestrator, engagement_id: str, raw_answers: dict) -> dict:
    outputs = {}
    outputs["run_discovery"] = orchestrator.run_discovery(raw_answers)
    outputs["run_inventory_setup"] = orchestrator.run_inventory_setup(page_id="home", component_id="header-nav")
    outputs["run_component_audit"] = orchestrator.run_component_audit("header-nav")
    outputs["run_journey_assessment"] = orchestrator.run_journey_assessment("checkout")
    outputs["run_page_audit"] = orchestrator.run_page_audit("checkout")
    outputs["run_architecture_audit"] = orchestrator.run_architecture_audit("primary-site")
    outputs["run_infrastructure_audit"] = orchestrator.run_infrastructure_audit("primary-site")
    outputs["run_wcag_coverage"] = orchestrator.run_wcag_coverage(engagement_id)
    outputs["run_508_mapping"] = orchestrator.run_508_mapping(engagement_id)
    outputs["run_scoring_and_prioritization"] = orchestrator.run_scoring_and_prioritization(engagement_id)
    outputs["run_reporting"] = orchestrator.run_reporting(engagement_id)
    outputs["request_human_approval"] = orchestrator.request_human_approval(engagement_id)
    outputs["run_remediation_planning"] = orchestrator.run_remediation_planning()
    outputs["run_delivery"] = orchestrator.run_delivery()
    outputs["run_retest_cycle"] = orchestrator.run_retest_cycle(engagement_id)
    return outputs
