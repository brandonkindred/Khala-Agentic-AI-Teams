"""Spec Intake and Validation agent: validates spec, produces lint report, glossary, assumptions, REQ-IDs."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List

from software_engineering_team.shared.llm import LLMClient
from software_engineering_team.shared.models import ProductRequirements

from .models import AcceptanceCriterionItem, SpecIntakeInput, SpecIntakeOutput, validated_spec_to_requirements
from .prompts import SPEC_INTAKE_PROMPT

logger = logging.getLogger(__name__)


def _write_artifact(plan_dir: Path, filename: str, content: str) -> None:
    """Write a plan artifact to plan_dir."""
    if not plan_dir:
        return
    plan_dir = Path(plan_dir).resolve()
    plan_dir.mkdir(parents=True, exist_ok=True)
    out_file = plan_dir / filename
    out_file.write_text(content, encoding="utf-8")
    logger.info("Wrote plan artifact to %s", out_file)


def _format_lint_report(content: str) -> str:
    """Format spec lint report as markdown."""
    if not content or not content.strip():
        return "# Spec Lint Report\n\nNo significant issues found.\n"
    if not content.strip().startswith("#"):
        return "# Spec Lint Report\n\n" + content.strip() + "\n"
    return content.strip() + "\n"


def _format_glossary(glossary: Dict[str, str]) -> str:
    """Format glossary as markdown."""
    lines = ["# Glossary\n", "Canonical domain terms and definitions.\n", ""]
    for term, definition in sorted(glossary.items()):
        lines.append(f"- **{term}:** {definition}")
    return "\n".join(lines) + "\n"


def _format_assumptions_and_questions(assumptions: List[str], open_questions: List[str]) -> str:
    """Format assumptions and open questions as markdown."""
    lines = ["# Assumptions and Open Questions\n", ""]
    if assumptions:
        lines.extend(["## Assumptions", ""])
        for a in assumptions:
            lines.append(f"- {a}")
        lines.append("")
    if open_questions:
        lines.extend(["## Open Questions", ""])
        for q in open_questions:
            lines.append(f"- {q}")
        lines.append("")
    if not assumptions and not open_questions:
        lines.append("None documented.\n")
    return "\n".join(lines)


def _format_acceptance_criteria_index(items: List[AcceptanceCriterionItem]) -> str:
    """Format acceptance criteria index as markdown."""
    lines = ["# Acceptance Criteria Index\n", "Every requirement mapped to a stable ID and testable statement.\n", ""]
    for item in items:
        lines.append(f"- **{item.id}:** {item.statement}")
    return "\n".join(lines) + "\n"


class SpecIntakeAgent:
    """
    Validates the spec, detects ambiguity/contradictions, normalizes terms,
    and produces a workable spec snapshot with REQ-IDs.
    """

    def __init__(self, llm_client: LLMClient) -> None:
        assert llm_client is not None, "llm_client is required"
        self.llm = llm_client

    def run(self, input_data: SpecIntakeInput) -> SpecIntakeOutput:
        """Validate spec and produce lint report, glossary, assumptions, acceptance criteria index."""
        logger.info("Spec Intake: starting validation (%s chars)", len(input_data.spec_content))
        prompt = SPEC_INTAKE_PROMPT + "\n\n---\n\n**Specification:**\n\n" + (
            input_data.spec_content[:50000] + ("..." if len(input_data.spec_content) > 50000 else "")
        )
        data: Dict[str, Any] = self.llm.complete_json(prompt, temperature=0.1) or {}

        # Parse acceptance criteria index
        ac_index: List[AcceptanceCriterionItem] = []
        for item in data.get("acceptance_criteria_index") or []:
            if isinstance(item, dict) and item.get("id") and item.get("statement"):
                ac_index.append(AcceptanceCriterionItem(id=item["id"], statement=item["statement"]))
            elif isinstance(item, dict) and (item.get("id") or item.get("statement")):
                ac_index.append(AcceptanceCriterionItem(
                    id=item.get("id", f"REQ-{len(ac_index)+1:03d}"),
                    statement=item.get("statement", str(item)),
                ))

        # Build ProductRequirements for output
        constraints = data.get("constraints") or []
        if not isinstance(constraints, list):
            constraints = []
        requirements = ProductRequirements(
            title=data.get("title") or "Software Project",
            description=data.get("description") or input_data.spec_content[:2000],
            acceptance_criteria=[item.statement for item in ac_index] if ac_index else [],
            constraints=constraints,
            priority=data.get("priority") or "medium",
            metadata={"parsed_from": "initial_spec.md", "validated_by": "spec_intake_agent"},
        )

        glossary = data.get("glossary") or {}
        if not isinstance(glossary, dict):
            glossary = {}
        assumptions = data.get("assumptions") or []
        if not isinstance(assumptions, list):
            assumptions = [str(assumptions)] if assumptions else []
        open_questions = data.get("open_questions") or []
        if not isinstance(open_questions, list):
            open_questions = [str(open_questions)] if open_questions else []

        output = SpecIntakeOutput(
            requirements=requirements,
            acceptance_criteria_index=ac_index,
            spec_lint_report=data.get("spec_lint_report") or "",
            glossary=glossary,
            assumptions=assumptions,
            open_questions=open_questions,
            summary=data.get("summary") or "",
            compact_summary=data.get("compact_summary") or "",
        )

        # Write artifacts to plan_dir
        plan_dir = input_data.plan_dir
        if plan_dir is not None:
            plan_path = Path(plan_dir).resolve()
            _write_artifact(plan_path, "spec_lint_report.md", _format_lint_report(output.spec_lint_report))
            _write_artifact(plan_path, "glossary.md", _format_glossary(output.glossary))
            _write_artifact(
                plan_path,
                "assumptions_and_questions.md",
                _format_assumptions_and_questions(output.assumptions, output.open_questions),
            )
            _write_artifact(plan_path, "acceptance_criteria_index.md", _format_acceptance_criteria_index(output.acceptance_criteria_index))

        logger.info(
            "Spec Intake: done, %s REQ-IDs, %s glossary terms, %s assumptions",
            len(ac_index),
            len(glossary),
            len(assumptions),
        )
        return output
