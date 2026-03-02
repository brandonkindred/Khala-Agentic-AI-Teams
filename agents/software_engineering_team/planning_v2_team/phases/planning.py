"""
Planning phase: high-level plan, milestones, user stories, hierarchy.

Tool agents: System Design, Architecture, User Story, DevOps, UI Design.

Collects clarification questions from tool agents (e.g., DevOps needing deployment target info)
and returns them in PlanningPhaseResult for the orchestrator to surface to the user.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from shared.llm import LLMClient
from shared.models import PlanningHierarchy

from ..models import PlanningPhaseResult, SpecReviewResult, ToolAgentKind, ToolAgentPhaseInput
from ..output_templates import parse_planning_output
from ..prompts import PLANNING_PROMPT
from ..tool_agents.json_utils import complete_text_with_continuation

logger = logging.getLogger(__name__)


def _collect_clarification_questions(
    agent_name: str,
    metadata: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Extract clarification questions from tool agent metadata.
    
    Args:
        agent_name: Name of the agent (e.g., "devops", "architecture")
        metadata: The metadata dict from ToolAgentPhaseOutput
        
    Returns:
        List of question dicts with source and question_text
    """
    questions: List[Dict[str, Any]] = []
    if metadata.get("needs_clarification"):
        for q_text in metadata.get("clarification_questions", []):
            if isinstance(q_text, str) and q_text.strip():
                questions.append({
                    "source": agent_name,
                    "question_text": q_text.strip(),
                })
    return questions


def _parse_planning_response(raw: Any) -> PlanningPhaseResult:
    """Parse LLM JSON response into PlanningPhaseResult."""
    if not isinstance(raw, dict):
        return PlanningPhaseResult(summary="Planning complete (no structured output).")

    key_features = raw.get("key_features")
    milestones = raw.get("milestones")
    dependencies = raw.get("dependencies")

    return PlanningPhaseResult(
        goals_vision=str(raw.get("goals_vision", "") or ""),
        constraints_limitations=str(raw.get("constraints_limitations", "") or ""),
        key_features=list(key_features) if isinstance(key_features, list) else [],
        milestones=list(milestones) if isinstance(milestones, list) else [],
        architecture=str(raw.get("architecture", "") or ""),
        maintainability=str(raw.get("maintainability", "") or ""),
        security=str(raw.get("security", "") or ""),
        file_system=str(raw.get("file_system", "") or ""),
        styling=str(raw.get("styling", "") or ""),
        dependencies=list(dependencies) if isinstance(dependencies, list) else [],
        microservices=str(raw.get("microservices", "") or ""),
        others=str(raw.get("others", "") or ""),
        summary=str(raw.get("summary", "") or "Planning complete."),
    )


def run_planning(
    llm: LLMClient,
    spec_content: str,
    repo_path: Path,
    spec_review_result: Optional[SpecReviewResult] = None,
    inspiration_content: Optional[str] = None,
    tool_agents: Optional[Dict[ToolAgentKind, Any]] = None,
) -> PlanningPhaseResult:
    """
    Run Planning phase.
    
    Tool agents participating: System Design, Architecture, User Story, DevOps, UI Design.
    
    Collects clarification questions from tool agents and returns them in the result
    for the orchestrator to surface to the user via the Open Questions UI.
    """
    all_recommendations: list[str] = []
    hierarchy: Optional[PlanningHierarchy] = None
    metadata: Dict[str, Any] = {}
    clarification_questions: List[Dict[str, Any]] = []
    
    tool_agent_input = ToolAgentPhaseInput(
        spec_content=spec_content,
        inspiration_content=inspiration_content or "",
        repo_path=str(repo_path),
        spec_review_result=spec_review_result,
    )
    
    if tool_agents:
        system_design_agent = tool_agents.get(ToolAgentKind.SYSTEM_DESIGN)
        if system_design_agent and hasattr(system_design_agent, "plan"):
            try:
                sd_result = system_design_agent.plan(tool_agent_input)
                all_recommendations.extend(sd_result.recommendations)
                metadata["system_design"] = sd_result.metadata
                logger.info("Planning: SystemDesign provided %d recommendations", len(sd_result.recommendations))
            except Exception as e:
                logger.warning(
                    "SystemDesign plan failed: %s. Next step -> Continuing with other planning agents",
                    e,
                )
        
        architecture_agent = tool_agents.get(ToolAgentKind.ARCHITECTURE)
        if architecture_agent and hasattr(architecture_agent, "plan"):
            try:
                arch_result = architecture_agent.plan(tool_agent_input)
                all_recommendations.extend(arch_result.recommendations)
                metadata["architecture"] = arch_result.metadata
                logger.info("Planning: Architecture provided %d recommendations", len(arch_result.recommendations))
            except Exception as e:
                logger.warning(
                    "Architecture plan failed: %s. Next step -> Continuing with other planning agents",
                    e,
                )
        
        user_story_agent = tool_agents.get(ToolAgentKind.USER_STORY)
        if user_story_agent and hasattr(user_story_agent, "plan"):
            try:
                us_result = user_story_agent.plan(tool_agent_input)
                all_recommendations.extend(us_result.recommendations)
                if us_result.hierarchy:
                    hierarchy = us_result.hierarchy
                    logger.info("Planning: UserStory created hierarchy with %d initiatives", len(hierarchy.initiatives))
            except Exception as e:
                logger.warning(
                    "UserStory plan failed: %s. Next step -> Continuing with other planning agents",
                    e,
                )
        
        devops_agent = tool_agents.get(ToolAgentKind.DEVOPS)
        if devops_agent and hasattr(devops_agent, "plan"):
            try:
                devops_result = devops_agent.plan(tool_agent_input)
                all_recommendations.extend(devops_result.recommendations)
                metadata["devops"] = devops_result.metadata
                logger.info("Planning: DevOps provided %d recommendations", len(devops_result.recommendations))
                devops_questions = _collect_clarification_questions("devops", devops_result.metadata)
                if devops_questions:
                    clarification_questions.extend(devops_questions)
                    logger.info("Planning: DevOps raised %d clarification questions", len(devops_questions))
            except Exception as e:
                logger.warning(
                    "DevOps plan failed: %s. Next step -> Continuing with other planning agents",
                    e,
                )
        
        ui_design_agent = tool_agents.get(ToolAgentKind.UI_DESIGN)
        if ui_design_agent and hasattr(ui_design_agent, "plan"):
            try:
                ui_result = ui_design_agent.plan(tool_agent_input)
                all_recommendations.extend(ui_result.recommendations)
                metadata["ui_design"] = ui_result.metadata
                logger.info("Planning: UIDesign provided %d recommendations", len(ui_result.recommendations))
            except Exception as e:
                logger.warning(
                    "UIDesign plan failed: %s. Next step -> Continuing to LLM synthesis",
                    e,
                )
    
    review_summary = (spec_review_result.summary if spec_review_result else "") or "None"
    prompt = PLANNING_PROMPT.format(
        spec_content=(spec_content or "")[:8000],
        review_summary=review_summary[:1000],
    )
    try:
        raw_text = complete_text_with_continuation(
            llm=llm,
            prompt=prompt,
            agent_name="PlanningV2_Planning",
        )
        raw = parse_planning_output(raw_text)
        result = _parse_planning_response(raw)

        init_count = len(hierarchy.initiatives) if hierarchy else 0
        epic_count = (
            sum(len(i.epics) for i in hierarchy.initiatives) if hierarchy else 0
        )
        story_count = (
            sum(len(e.stories) for i in hierarchy.initiatives for e in i.epics)
            if hierarchy
            else 0
        )

        logger.info(
            "Planning: %d milestones, %d key_features, %d initiatives, %d epics, %d stories",
            len(result.milestones),
            len(result.key_features),
            init_count,
            epic_count,
            story_count,
        )

        return PlanningPhaseResult(
            goals_vision=result.goals_vision,
            constraints_limitations=result.constraints_limitations,
            key_features=result.key_features,
            milestones=result.milestones,
            architecture=result.architecture,
            maintainability=result.maintainability,
            security=result.security,
            file_system=result.file_system,
            styling=result.styling,
            dependencies=result.dependencies,
            microservices=result.microservices,
            others=result.others,
            summary=result.summary,
            hierarchy=hierarchy,
            clarification_questions=clarification_questions,
        )
    except Exception as e:
        logger.warning("Planning LLM call failed, using tool agent results: %s", e)
        return PlanningPhaseResult(
            summary="Planning completed with tool agents.",
            hierarchy=hierarchy,
            clarification_questions=clarification_questions,
        )
