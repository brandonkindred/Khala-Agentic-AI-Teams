from ..agents import EngagementOrchestrator


def _first_non_empty(raw_answers: dict, keys: tuple[str, ...], fallback: str) -> str:
    for key in keys:
        value = raw_answers.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, list):
            for item in value:
                if isinstance(item, str) and item.strip():
                    return item.strip()
    return fallback


def run_engagement_workflow(
    orchestrator: EngagementOrchestrator, engagement_id: str, raw_answers: dict
) -> dict:
    page_id = _first_non_empty(
        raw_answers, ("tier1_pages", "pages", "page_ids"), "home"
    )
    component_id = _first_non_empty(
        raw_answers,
        ("priority_components", "components", "component_ids"),
        "header-nav",
    )
    journey_id = _first_non_empty(
        raw_answers, ("priority_journeys", "journeys", "journey_ids"), page_id
    )
    system_target = _first_non_empty(
        raw_answers,
        ("system_target", "site_id", "site_name", "organization"),
        "primary-site",
    )

    outputs = {}
    outputs["run_discovery"] = orchestrator.run_discovery(raw_answers)
    outputs["run_inventory_setup"] = orchestrator.run_inventory_setup(
        page_id=page_id, component_id=component_id
    )
    outputs["run_component_audit"] = orchestrator.run_component_audit(component_id)
    outputs["run_journey_assessment"] = orchestrator.run_journey_assessment(journey_id)
    outputs["run_page_audit"] = orchestrator.run_page_audit(page_id)
    outputs["run_architecture_audit"] = orchestrator.run_architecture_audit(
        system_target
    )
    outputs["run_infrastructure_audit"] = orchestrator.run_infrastructure_audit(
        system_target
    )
    outputs["run_wcag_coverage"] = orchestrator.run_wcag_coverage(engagement_id)
    outputs["run_508_mapping"] = orchestrator.run_508_mapping(engagement_id)
    outputs["run_scoring_and_prioritization"] = (
        orchestrator.run_scoring_and_prioritization(engagement_id)
    )
    outputs["run_reporting"] = orchestrator.run_reporting(engagement_id)
    outputs["request_human_approval"] = orchestrator.request_human_approval(
        engagement_id
    )
    outputs["run_remediation_planning"] = orchestrator.run_remediation_planning()
    if outputs["request_human_approval"].get("approved"):
        outputs["run_delivery"] = orchestrator.run_delivery()
    else:
        outputs["run_delivery"] = {
            "phase": "delivery",
            "status": "blocked",
            "reason": "approval not granted",
        }
    outputs["run_retest_cycle"] = orchestrator.run_retest_cycle(engagement_id)
    return outputs
