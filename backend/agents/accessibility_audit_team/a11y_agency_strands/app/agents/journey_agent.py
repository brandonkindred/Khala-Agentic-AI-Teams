from .base import ToolContext, tool
from ..tools import (
    log_keyboard_test,
    log_mobile_accessibility_test,
    log_screen_reader_test,
    persist_artifact,
)


@tool(context=True)
def run_journey_assessment(journey_id: str, tool_context: ToolContext) -> dict:
    results = [
        log_keyboard_test(journey_id, "pass"),
        log_screen_reader_test(journey_id, "needs-improvement"),
        log_mobile_accessibility_test(journey_id, "pass"),
    ]
    artifact = persist_artifact(
        f"{tool_context.invocation_state['artifact_root']}/journey_{journey_id}.json",
        results,
    )
    return {"phase": "journey_assessment", "artifact": artifact, "journey": journey_id}
