"""
Planning consolidation: produce master plan, ADR pack, risk register, ship checklist.

Runs after all planning agents (including Tech Lead) have completed.
Collects artifacts from plan_dir and produces a unified master_plan.md.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def run_planning_consolidation(
    plan_dir: Path,
    assignment: Any,
    architecture: Any,
    project_overview: Optional[Dict[str, Any]] = None,
) -> Path:
    """
    Produce plan/master_plan.md with links to all artifacts, risk register, ship checklist.

    Returns the path to the written master_plan.md.
    """
    plan_path = Path(plan_dir).resolve()
    plan_path.mkdir(parents=True, exist_ok=True)
    out_file = plan_path / "master_plan.md"

    sections: List[str] = [
        "# Master Plan",
        "",
        "Unified execution plan with links to all planning artifacts.",
        "",
        "## Artifacts in plan/",
        "",
    ]

    artifact_files = [
        "planning_team/planning_document.md",
        "spec_lint_report.md",
        "glossary.md",
        "assumptions_and_questions.md",
        "acceptance_criteria_index.md",
        "project_overview.md",
        "features_and_functionality.md",
        "architecture.md",
        "data_schema.md",
        "data_architecture.md",
        "ui_ux.md",
        "frontend_architecture.md",
        "infrastructure.md",
        "devops_pipeline.md",
        "test_strategy.md",
        "security_and_compliance.md",
        "observability.md",
        "performance.md",
        "tech_lead.md",
    ]
    for f in artifact_files:
        if (plan_path / f).exists():
            sections.append(f"- [{f}]({f})")
    # OpenAPI spec is in backend/ root (not plan/)
    backend_openapi = plan_path.parent / "backend" / "openapi.yaml"
    if backend_openapi.exists():
        sections.append("- [backend/openapi.yaml](../backend/openapi.yaml)")
    sections.append("")

    if assignment and hasattr(assignment, "execution_order"):
        sections.extend(["## Execution Order", ""])
        sections.append(", ".join(assignment.execution_order))
        sections.append("")

    if project_overview and project_overview.get("risk_items"):
        sections.extend(["## Risk Register", ""])
        for r in project_overview["risk_items"]:
            desc = r.get("description", "")
            sev = r.get("severity", "medium")
            mit = r.get("mitigation", "")
            sections.append(f"- **[{sev}]** {desc}")
            if mit:
                sections.append(f"  - Mitigation: {mit}")
        sections.append("")

    sections.extend(
        [
            "## Ship Checklist",
            "",
            "- [ ] All REQ-* covered by tests",
            "- [ ] Security review complete",
            "- [ ] SLOs defined and monitored",
            "- [ ] Documentation updated",
            "- [ ] Runbooks available",
            "",
        ]
    )

    content = "\n".join(sections)
    out_file.write_text(content, encoding="utf-8")
    logger.info("Wrote master plan to %s", out_file)
    return out_file
