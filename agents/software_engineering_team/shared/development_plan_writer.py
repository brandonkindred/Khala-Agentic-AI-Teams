"""
Write development plans produced by planning agents to markdown files.

When plan_dir is provided (or defaulted), files are written under plan/ at project root
(e.g. plan/project_overview.md). When plan_dir is explicitly None, files use the legacy
DEVELOPMENT_PLAN- prefix at repo root for backward compatibility.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from software_engineering_team.shared.models import Task, TaskAssignment, SystemArchitecture

logger = logging.getLogger(__name__)

DEVELOPMENT_PLAN_PREFIX = "DEVELOPMENT_PLAN-"
PLAN_FOLDER_NAME = "plan"


def _resolve_output_dir(repo_path: str | Path, plan_dir: Optional[Path]) -> Path:
    """Resolve the directory for writing plan artifacts. Defaults to {repo_path}/plan."""
    base = Path(repo_path).resolve()
    if plan_dir is not None:
        return Path(plan_dir).resolve()
    return base / PLAN_FOLDER_NAME


def _resolve_output_file(repo_path: str | Path, plan_dir: Optional[Path], filename: str) -> Path:
    """Resolve the full path for a plan artifact file under plan/."""
    out_dir = _resolve_output_dir(repo_path, plan_dir)
    return out_dir / filename


def _normalize_mermaid(content: str) -> str:
    """Strip existing mermaid code fences if present, return raw Mermaid."""
    s = content.strip()
    # Match ```mermaid or ``` mermaid at start, ``` at end
    match = re.match(r"^```\s*mermaid\s*\n(.*)\n?```\s*$", s, re.DOTALL)
    if match:
        return match.group(1).strip()
    return s


def write_project_overview_plan(
    repo_path: str | Path,
    overview: Any,
    plan_dir: Optional[Path] = None,
) -> Path:
    """
    Write the project overview plan to plan/project_overview.md.
    Returns the path of the written file.
    """
    out_dir = _resolve_output_dir(repo_path, plan_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = _resolve_output_file(repo_path, plan_dir, "project_overview.md")

    sections: List[str] = [
        "# Development Plan: Project Overview",
        "",
        "## Primary Goal",
        "",
        overview.primary_goal.strip() or "(No primary goal provided.)",
        "",
    ]

    if overview.secondary_goals:
        sections.extend(["## Secondary Goals", ""])
        for g in overview.secondary_goals:
            sections.append(f"- {g}")
        sections.append("")

    if overview.delivery_strategy:
        sections.extend(["## Delivery Strategy", "", overview.delivery_strategy.strip(), ""])

    if overview.milestones:
        sections.extend(["## Milestones", ""])
        for m in sorted(overview.milestones, key=lambda x: x.target_order):
            sections.append(f"### {m.name}")
            sections.append(f"- **Order:** {m.target_order}")
            if m.description:
                sections.append(f"- **Description:** {m.description}")
            if m.scope_summary:
                sections.append(f"- **Scope:** {m.scope_summary}")
            if getattr(m, "definition_of_done", "") and str(m.definition_of_done).strip():
                sections.append(f"- **Definition of Done:** {m.definition_of_done}")
            sections.append("")

    if getattr(overview, "scope_cut", "") and str(overview.scope_cut).strip():
        sections.extend(["## Scope Cut (MVP vs V1 vs Later)", "", overview.scope_cut.strip(), ""])

    if getattr(overview, "epic_story_breakdown", None):
        breakdown = overview.epic_story_breakdown
        if breakdown:
            sections.extend(["## Epic/Story Breakdown", ""])
            for e in breakdown:
                deps = ", ".join(e.dependencies) if getattr(e, "dependencies", None) else ""
                scope = getattr(e, "scope", "MVP") or "MVP"
                sections.append(f"- **{e.name}** (id: {e.id}, scope: {scope})")
                if e.description:
                    sections.append(f"  - {e.description}")
                if deps:
                    sections.append(f"  - Dependencies: {deps}")
            sections.append("")

    if getattr(overview, "non_functional_requirements", None):
        nfrs = overview.non_functional_requirements
        if nfrs:
            sections.extend(["## Non-Functional Requirements", ""])
            for nfr in nfrs:
                sections.append(f"- {nfr}")
            sections.append("")

    if overview.risk_items:
        sections.extend(["## Risks", ""])
        for r in overview.risk_items:
            sections.append(f"- **[{r.severity}]** {r.description}")
            if r.mitigation:
                sections.append(f"  - Mitigation: {r.mitigation}")
        sections.append("")

    content = "\n".join(sections)
    out_file.write_text(content, encoding="utf-8")
    logger.info("Wrote development plan to %s", out_file)
    return out_file


def write_features_and_functionality_plan(
    repo_path: str | Path,
    features_doc: str,
    plan_dir: Optional[Path] = None,
) -> Path:
    """
    Write the features and functionality document to plan/features_and_functionality.md.
    Returns the path of the written file.
    """
    out_dir = _resolve_output_dir(repo_path, plan_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = _resolve_output_file(repo_path, plan_dir, "features_and_functionality.md")
    content = (
        "# Development Plan: Features and Functionality\n\n"
        "High-level features and functionalities required (from initial spec).\n\n"
        "---\n\n"
    ) + (features_doc.strip() or "(No features document generated.)")
    out_file.write_text(content, encoding="utf-8")
    logger.info("Wrote development plan to %s", out_file)
    return out_file


def write_architecture_plan(
    repo_path: str | Path,
    architecture: SystemArchitecture,
    plan_dir: Optional[Path] = None,
) -> Path:
    """
    Write the architecture plan to plan/architecture.md.
    Returns the path of the written file.
    """
    out_dir = _resolve_output_dir(repo_path, plan_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = _resolve_output_file(repo_path, plan_dir, "architecture.md")

    sections: List[str] = [
        "# Development Plan: Architecture",
        "",
        "## Overview",
        "",
        architecture.overview.strip() or "(No overview provided.)",
        "",
    ]

    if architecture.architecture_document and architecture.architecture_document.strip():
        sections.extend(["## Architecture Document", "", architecture.architecture_document.strip(), ""])

    if architecture.components:
        sections.extend(["## Components", ""])
        for c in architecture.components:
            sections.append(f"### {c.name}")
            sections.append(f"- **Type:** {c.type}")
            if c.technology:
                sections.append(f"- **Technology:** {c.technology}")
            if c.description:
                sections.append(f"- **Description:** {c.description}")
            if c.dependencies:
                sections.append(f"- **Dependencies:** {', '.join(c.dependencies)}")
            if c.interfaces:
                sections.append(f"- **Interfaces:** {', '.join(c.interfaces)}")
            sections.append("")

    if architecture.diagrams:
        sections.extend(["## Diagrams", ""])
        for name, content in architecture.diagrams.items():
            mermaid = _normalize_mermaid(content)
            sections.extend([f"### {name}", "", "```mermaid", mermaid, "```", ""])

    if getattr(architecture, "tenancy_model", "") and str(architecture.tenancy_model).strip():
        sections.extend(["## Tenancy Model", "", architecture.tenancy_model.strip(), ""])

    if getattr(architecture, "reliability_model", "") and str(architecture.reliability_model).strip():
        sections.extend(["## Reliability Model", "", architecture.reliability_model.strip(), ""])

    if architecture.decisions:
        sections.extend(["## Architecture Decision Records (ADRs)", ""])
        for d in architecture.decisions:
            if isinstance(d, dict):
                adr_id = d.get("id", "")
                title = d.get("title") or d.get("name") or "Decision"
                header = f"### {adr_id} {title}" if adr_id else f"### {title}"
                sections.append(header)
                for k, v in d.items():
                    if k not in ("title", "name", "id") and v is not None:
                        sections.append(f"- **{k}:** {v}")
            else:
                sections.append(f"- {d}")
            sections.append("")

    content = "\n".join(sections)
    out_file.write_text(content, encoding="utf-8")
    logger.info("Wrote development plan to %s", out_file)
    return out_file


def write_tech_lead_plan(
    repo_path: str | Path,
    assignment: TaskAssignment,
    summary: str = "",
    requirement_task_mapping: List[Dict[str, Any]] | None = None,
    validation_report: str | None = None,
    plan_dir: Optional[Path] = None,
) -> Path:
    """
    Write the Tech Lead task plan to plan/tech_lead.md.
    Includes all tasks with full details, execution order, and optional validation report.
    Returns the path of the written file.
    """
    out_dir = _resolve_output_dir(repo_path, plan_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = _resolve_output_file(repo_path, plan_dir, "tech_lead.md")

    sections: List[str] = [
        "# Development Plan: Tech Lead",
        "",
        "## Summary",
        "",
        summary.strip() or "(No summary provided.)",
        "",
    ]

    if validation_report:
        sections.extend([validation_report.strip(), ""])

    if assignment.rationale:
        sections.extend(["## Rationale", "", assignment.rationale.strip(), ""])

    if requirement_task_mapping:
        sections.extend(["## Requirement → Task Mapping", ""])
        for m in requirement_task_mapping:
            spec_item = m.get("spec_item", "")
            task_ids = m.get("task_ids", [])
            sections.append(f"- **{spec_item}** → {', '.join(task_ids)}")
        sections.append("")

    sections.extend(["## Execution Order", ""])
    sections.append(", ".join(assignment.execution_order))
    sections.append("")
    sections.extend(["## Tasks", ""])

    for task in assignment.tasks:
        t = task if isinstance(task, Task) else task
        sections.extend([
            f"### {t.id}",
            "",
            f"- **Title:** {t.title}",
            f"- **Type:** {t.type.value if hasattr(t.type, 'value') else t.type}",
            f"- **Assignee:** {t.assignee}",
            "",
            "#### Description",
            "",
            t.description.strip() or "(No description.)",
            "",
        ])
        # If the task is linked to a specific architecture component, surface that for readers
        component_name = None
        try:
            component_name = (getattr(t, "metadata", {}) or {}).get("component_name")
        except Exception:
            component_name = None
        if component_name:
            sections.extend(
                [
                    f"- **Architecture component:** {component_name}",
                    "",
                ]
            )
        if t.user_story:
            sections.extend(["#### User Story", "", t.user_story.strip(), ""])
        if t.requirements:
            sections.extend(["#### Requirements", "", t.requirements.strip(), ""])
        if t.acceptance_criteria:
            sections.extend(["#### Acceptance Criteria", ""])
            for ac in t.acceptance_criteria:
                sections.append(f"- {ac}")
            sections.append("")
        if t.dependencies:
            sections.append(f"**Dependencies:** {', '.join(t.dependencies)}")
            sections.append("")
        sections.append("---")
        sections.append("")

    content = "\n".join(sections)
    out_file.write_text(content, encoding="utf-8")
    logger.info("Wrote development plan to %s", out_file)
    return out_file
