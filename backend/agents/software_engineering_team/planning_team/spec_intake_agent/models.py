"""Models for the Spec Intake and Validation agent."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from software_engineering_team.shared.models import ProductRequirements


class AcceptanceCriterionItem(BaseModel):
    """A single acceptance criterion with a stable requirement ID."""

    id: str = Field(..., description="Stable requirement ID, e.g. REQ-001")
    statement: str = Field(..., description="Testable requirement statement")


class SpecIntakeInput(BaseModel):
    """Input for the Spec Intake and Validation agent."""

    spec_content: str = Field(..., description="Raw content of initial_spec.md")
    plan_dir: Optional[Any] = Field(None, description="Path to plan folder for writing artifacts")


class SpecIntakeOutput(BaseModel):
    """Output from the Spec Intake and Validation agent."""

    requirements: ProductRequirements = Field(
        ...,
        description="Validated product requirements with acceptance_criteria from index",
    )
    acceptance_criteria_index: List[AcceptanceCriterionItem] = Field(
        default_factory=list,
        description="Every requirement mapped to REQ-ID and testable statement",
    )
    spec_lint_report: str = Field(
        default="",
        description="Report of missing sections, unclear requirements, inconsistent terms",
    )
    glossary: Dict[str, str] = Field(
        default_factory=dict,
        description="Canonical domain terms: term -> definition",
    )
    assumptions: List[str] = Field(
        default_factory=list,
        description="Documented assumptions made when interpreting the spec",
    )
    open_questions: List[str] = Field(
        default_factory=list,
        description="Open questions that need stakeholder clarification",
    )
    summary: str = Field(default="", description="Brief summary of validation outcome")
    compact_summary: str = Field(
        default="",
        description="Single paragraph ~200 chars for downstream planning context",
    )


def build_compact_spec_for_planning(output: SpecIntakeOutput, max_chars: int = 4000) -> str:
    """
    Build a compact, structured spec string from SpecIntakeOutput for downstream planning agents.
    Keeps only essential fields to reduce context size and speed up planning.
    """
    reqs = output.requirements
    lines = [
        f"# {reqs.title}",
        "",
        "## Goal",
        (reqs.description or "")[:500] + ("..." if len(reqs.description or "") > 500 else ""),
        "",
    ]
    if output.compact_summary:
        lines.extend(["## Summary", output.compact_summary, ""])
    if output.acceptance_criteria_index:
        lines.extend(["## Requirements", ""])
        for item in output.acceptance_criteria_index:
            lines.append(f"- **{item.id}:** {item.statement}")
        lines.append("")
    if reqs.constraints:
        lines.extend(["## Constraints", ""])
        for c in reqs.constraints:
            lines.append(f"- {c}")
        lines.append("")
    if output.open_questions:
        lines.extend(["## Open Questions", ""])
        for q in output.open_questions[:5]:  # cap at 5
            lines.append(f"- {q}")
        lines.append("")
    result = "\n".join(lines)
    if len(result) > max_chars:
        result = result[:max_chars] + "\n\n... [truncated for planning context]"
    return result


def validated_spec_to_requirements(output: SpecIntakeOutput) -> ProductRequirements:
    """
    Convert SpecIntakeOutput to ProductRequirements for downstream agents.

    Uses acceptance_criteria_index for acceptance_criteria and stores REQ-IDs in metadata.
    """
    reqs = output.requirements
    ac_statements = [item.statement for item in output.acceptance_criteria_index]
    req_ids = [item.id for item in output.acceptance_criteria_index]
    metadata = dict(reqs.metadata or {})
    metadata["requirement_ids"] = req_ids
    metadata["acceptance_criteria_index"] = [
        {"id": item.id, "statement": item.statement} for item in output.acceptance_criteria_index
    ]
    return ProductRequirements(
        title=reqs.title,
        description=reqs.description,
        acceptance_criteria=ac_statements if ac_statements else reqs.acceptance_criteria,
        constraints=reqs.constraints,
        priority=reqs.priority,
        metadata=metadata,
    )
