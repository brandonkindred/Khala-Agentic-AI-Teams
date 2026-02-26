"""
Spec Review and Gap analysis phase: System Design + Architecture tool agents.

Identifies critical gaps, open questions, and requirements from the spec.
Tool agents: System Design, Architecture.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, Optional

from shared.llm import LLMClient

from ..models import (
    OpenQuestion,
    QuestionOption,
    SpecReviewResult,
    ToolAgentKind,
    ToolAgentPhaseInput,
)
from ..prompts import SPEC_REVIEW_PROMPT

logger = logging.getLogger(__name__)


def _parse_question_option(opt_data: Any, index: int) -> QuestionOption:
    """Parse a single question option from LLM output."""
    if isinstance(opt_data, dict):
        return QuestionOption(
            id=str(opt_data.get("id", f"opt{index}")),
            label=str(opt_data.get("label", "")),
            is_default=bool(opt_data.get("is_default", False)),
        )
    return QuestionOption(id=f"opt{index}", label=str(opt_data), is_default=index == 0)


def _parse_open_question(q_data: Any, index: int) -> OpenQuestion:
    """Parse a single open question from LLM output."""
    if isinstance(q_data, dict):
        raw_options = q_data.get("options", [])
        options = [
            _parse_question_option(opt, i)
            for i, opt in enumerate(raw_options)
            if raw_options
        ]
        if not any(opt.is_default for opt in options) and options:
            options[0] = QuestionOption(
                id=options[0].id, label=options[0].label, is_default=True
            )
        return OpenQuestion(
            id=str(q_data.get("id", f"q{index}")),
            question_text=str(q_data.get("question_text", "")),
            context=str(q_data.get("context", "")),
            options=options,
            source="spec_review",
        )
    return _convert_string_to_question(str(q_data), index)


def _convert_string_to_question(question_str: str, index: int) -> OpenQuestion:
    """Convert a plain string question to structured OpenQuestion with default options."""
    return OpenQuestion(
        id=f"q{index}",
        question_text=question_str,
        context="This question was identified during spec review.",
        options=[
            QuestionOption(id="opt1", label="Yes", is_default=True),
            QuestionOption(id="opt2", label="No", is_default=False),
            QuestionOption(id="opt3", label="Needs further discussion", is_default=False),
        ],
        source="spec_review",
    )


def _parse_spec_review_response(raw: Any) -> SpecReviewResult:
    """Parse LLM JSON response into SpecReviewResult."""
    if not isinstance(raw, dict):
        return SpecReviewResult(summary="Spec review completed (no structured output).")

    issues = raw.get("issues")
    product_gaps = raw.get("product_gaps")
    raw_questions = raw.get("open_questions", [])

    open_questions = []
    if isinstance(raw_questions, list):
        for i, q in enumerate(raw_questions):
            open_questions.append(_parse_open_question(q, i))

    return SpecReviewResult(
        issues=list(issues) if isinstance(issues, list) else [],
        product_gaps=list(product_gaps) if isinstance(product_gaps, list) else [],
        open_questions=open_questions,
        plan_summary=str(raw.get("plan_summary", "") or ""),
        summary=str(raw.get("summary", "") or "Spec review complete."),
    )


def run_spec_review_gap(
    llm: LLMClient,
    spec_content: str,
    repo_path: Path,
    inspiration_content: Optional[str] = None,
    tool_agents: Optional[Dict[ToolAgentKind, Any]] = None,
) -> SpecReviewResult:
    """
    Run Product Requirement Analysis phase (Spec Review and Gap analysis).

    Tool agents participating: System Design, Architecture.
    """
    from typing import List

    all_issues: List[str] = []
    all_gaps: List[str] = []

    tool_agent_input = ToolAgentPhaseInput(
        spec_content=spec_content,
        inspiration_content=inspiration_content or "",
        repo_path=str(repo_path),
    )

    if tool_agents:
        system_design_agent = tool_agents.get(ToolAgentKind.SYSTEM_DESIGN)
        if system_design_agent and hasattr(system_design_agent, "spec_review"):
            try:
                sd_result = system_design_agent.spec_review(tool_agent_input)
                all_issues.extend(sd_result.issues)
                logger.info(
                    "Spec review: SystemDesign found %d issues", len(sd_result.issues)
                )
            except Exception as e:
                logger.warning("SystemDesign spec_review failed: %s", e)

        architecture_agent = tool_agents.get(ToolAgentKind.ARCHITECTURE)
        if architecture_agent and hasattr(architecture_agent, "spec_review"):
            try:
                arch_result = architecture_agent.spec_review(tool_agent_input)
                all_issues.extend(arch_result.issues)
                logger.info(
                    "Spec review: Architecture found %d issues", len(arch_result.issues)
                )
            except Exception as e:
                logger.warning("Architecture spec_review failed: %s", e)

    prompt = SPEC_REVIEW_PROMPT.format(
        spec_content=(spec_content or "")[:12000],
    )
    try:
        raw = llm.complete_json(prompt)
        result = _parse_spec_review_response(raw)

        combined_issues = list(set(all_issues + result.issues))
        combined_gaps = list(set(all_gaps + result.product_gaps))

        logger.info(
            "Spec review: %d issues, %d product gaps, %d open questions",
            len(combined_issues),
            len(combined_gaps),
            len(result.open_questions),
        )

        return SpecReviewResult(
            issues=combined_issues,
            product_gaps=combined_gaps,
            open_questions=result.open_questions,
            plan_summary=result.plan_summary,
            summary=result.summary,
        )
    except Exception as e:
        logger.warning("Spec review LLM call failed, using tool agent results: %s", e)
        return SpecReviewResult(
            issues=all_issues,
            product_gaps=all_gaps,
            open_questions=[],
            plan_summary="",
            summary="Spec review completed with tool agents.",
        )
