"""
Implementation phase: create or update planning assets.

Tool agents: All 8 (System Design, Architecture, User Story, DevOps, UI, UX, Task Classification, Task Dependency).
Note: Task Dependency only participates in Review per the matrix, but we include others in Implementation.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, Optional

from shared.llm import LLMClient
from shared.models import PlanningHierarchy

from ..models import (
    ImplementationPhaseResult,
    PlanningPhaseResult,
    SpecReviewResult,
    ToolAgentKind,
    ToolAgentPhaseInput,
)
from ..tool_agents.user_story.agent import _hierarchy_to_markdown

logger = logging.getLogger(__name__)


def run_implementation(
    llm: LLMClient,
    spec_content: str,
    repo_path: Path,
    spec_review_result: Optional[SpecReviewResult] = None,
    planning_result: Optional[PlanningPhaseResult] = None,
    inspiration_content: Optional[str] = None,
    tool_agents: Optional[Dict[ToolAgentKind, Any]] = None,
    hierarchy: Optional[PlanningHierarchy] = None,
) -> ImplementationPhaseResult:
    """
    Run Implementation phase with all participating tool agents.
    
    Tool agents: System Design, Architecture, User Story, DevOps, UI Design, UX Design, Task Classification.
    """
    assets_created: list[str] = []
    assets_updated: list[str] = []
    all_files: Dict[str, str] = {}
    
    try:
        repo_path.mkdir(parents=True, exist_ok=True)
        plan_dir = repo_path / "planning_v2"
        plan_dir.mkdir(parents=True, exist_ok=True)
        
        effective_hierarchy = hierarchy
        if planning_result and planning_result.hierarchy:
            effective_hierarchy = planning_result.hierarchy
        
        metadata: Dict[str, Any] = {}
        if planning_result:
            pass
        
        tool_agent_input = ToolAgentPhaseInput(
            spec_content=spec_content,
            inspiration_content=inspiration_content or "",
            repo_path=str(repo_path),
            spec_review_result=spec_review_result,
            planning_result=planning_result,
            hierarchy=effective_hierarchy,
            metadata=metadata,
        )
        
        participating_agents = [
            ToolAgentKind.SYSTEM_DESIGN,
            ToolAgentKind.ARCHITECTURE,
            ToolAgentKind.USER_STORY,
            ToolAgentKind.DEVOPS,
            ToolAgentKind.UI_DESIGN,
            ToolAgentKind.UX_DESIGN,
            ToolAgentKind.TASK_CLASSIFICATION,
        ]
        
        if tool_agents:
            for agent_kind in participating_agents:
                agent = tool_agents.get(agent_kind)
                if agent and hasattr(agent, "execute"):
                    try:
                        result = agent.execute(tool_agent_input)
                        if result.files:
                            all_files.update(result.files)
                            logger.info("Implementation: %s generated %d files", agent_kind.value, len(result.files))
                    except Exception as e:
                        logger.warning("Implementation: %s execute failed: %s", agent_kind.value, e)
        
        for rel_path, content in all_files.items():
            full_path = repo_path / rel_path
            full_path.parent.mkdir(parents=True, exist_ok=True)
            full_path.write_text(content, encoding="utf-8")
            assets_created.append(rel_path)
            logger.info("Implementation: wrote %s", rel_path)
        
        parts = ["# Planning (v2) Artifacts\n\n"]
        if spec_review_result:
            parts.append("## Spec Review Summary\n")
            parts.append(spec_review_result.summary or "")
            parts.append("\n\n")
            if spec_review_result.gaps:
                parts.append("### Gaps Identified\n")
                for gap in spec_review_result.gaps:
                    parts.append(f"- {gap}\n")
                parts.append("\n")
        
        if planning_result:
            parts.append("## High-Level Plan\n")
            parts.append(planning_result.high_level_plan or planning_result.summary or "")
            parts.append("\n\n")
            
            if planning_result.milestones:
                parts.append("### Milestones\n")
                for m in planning_result.milestones:
                    parts.append(f"- {m}\n")
                parts.append("\n")
            
            if planning_result.user_stories:
                parts.append("### User Stories (Summary)\n")
                for u in planning_result.user_stories:
                    parts.append(f"- {u}\n")
                parts.append("\n")
        
        if effective_hierarchy:
            parts.append("## Planning Hierarchy\n")
            parts.append(f"- {len(effective_hierarchy.initiatives)} Initiative(s)\n")
            epic_count = sum(len(i.epics) for i in effective_hierarchy.initiatives)
            story_count = sum(len(e.stories) for i in effective_hierarchy.initiatives for e in i.epics)
            task_count = sum(len(s.tasks) for i in effective_hierarchy.initiatives for e in i.epics for s in e.stories)
            parts.append(f"- {epic_count} Epic(s)\n")
            parts.append(f"- {story_count} Story(ies)\n")
            parts.append(f"- {task_count} Task(s)\n")
            parts.append("\nSee `planning_v2_hierarchy.md` in `/plan` for detailed hierarchy.\n")
            
            # Write full hierarchy to /plan/planning_v2_hierarchy.md
            try:
                main_plan_dir = repo_path / "plan"
                main_plan_dir.mkdir(parents=True, exist_ok=True)
                hierarchy_md = _hierarchy_to_markdown(effective_hierarchy)
                hierarchy_file = main_plan_dir / "planning_v2_hierarchy.md"
                hierarchy_file.write_text(hierarchy_md, encoding="utf-8")
                hierarchy_rel_path = str(hierarchy_file.relative_to(repo_path))
                if hierarchy_rel_path not in assets_created:
                    assets_created.append(hierarchy_rel_path)
                logger.info("Implementation: wrote full hierarchy to %s", hierarchy_file)
            except Exception as e:
                logger.warning("Implementation: failed to write hierarchy file: %s", e)
        
        out_file = plan_dir / "planning_artifacts.md"
        out_file.write_text("".join(parts), encoding="utf-8")
        rel_path = str(out_file.relative_to(repo_path))
        if rel_path not in assets_created:
            assets_created.append(rel_path)
        logger.info("Implementation: wrote %s", out_file)
        
    except Exception as e:
        logger.warning("Implementation write failed: %s", e)

    return ImplementationPhaseResult(
        assets_created=assets_created,
        assets_updated=assets_updated,
        summary=f"Implementation complete. Created {len(assets_created)} assets." if assets_created else "No assets written.",
    )
