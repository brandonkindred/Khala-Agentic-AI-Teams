from agents.accessibility_audit_team.a11y_agency_strands.app.agents.base import ToolContext
from agents.accessibility_audit_team.a11y_agency_strands.app.agents.component_auditor import run_component_audit


def test_component_specialist_tool_contract_returns_machine_summary(tmp_path):
    context = ToolContext({"artifact_root": str(tmp_path), "questionnaire": {}})
    result = run_component_audit("checkout-form", context)
    assert result["phase"] == "component_audit"
    assert result["finding_id"].startswith("cmp-")
    assert result["artifact"].endswith("component_checkout-form.json")
