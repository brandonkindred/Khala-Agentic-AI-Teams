"""
Deliver phase: commit and finalize planning artifacts.

Tool agents: System Design, Architecture, User Story.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Any, Dict, Optional

from llm_service import LLMClient
from software_engineering_team.shared.models import PlanningHierarchy

from ..models import (
    PLAN_PLANNING_TEAM_DIR,
    DeliverPhaseResult,
    ImplementationPhaseResult,
    ToolAgentKind,
    ToolAgentPhaseInput,
)

logger = logging.getLogger(__name__)


def run_deliver(
    llm: LLMClient,
    spec_content: str,
    repo_path: Path,
    implementation_result: Optional[ImplementationPhaseResult] = None,
    tool_agents: Optional[Dict[ToolAgentKind, Any]] = None,
    hierarchy: Optional[PlanningHierarchy] = None,
) -> DeliverPhaseResult:
    """
    Run Deliver phase with participating tool agents.
    
    Tool agents: System Design, Architecture, User Story.
    Finalizes planning artifacts and optionally commits to git.
    
    Also finalizes the spec by:
    - Renaming updated_spec.md to product_spec.md
    - Deleting intermediate updated_spec_v*.md files
    """
    committed = False
    summaries: list[str] = []
    final_spec_content: Optional[str] = None
    
    tool_agent_input = ToolAgentPhaseInput(
        spec_content=spec_content,
        repo_path=str(repo_path),
        implementation_result=implementation_result,
        hierarchy=hierarchy,
    )
    
    participating_agents = [
        ToolAgentKind.SYSTEM_DESIGN,
        ToolAgentKind.ARCHITECTURE,
        ToolAgentKind.USER_STORY,
    ]
    
    if tool_agents:
        for agent_kind in participating_agents:
            agent = tool_agents.get(agent_kind)
            if agent and hasattr(agent, "deliver"):
                try:
                    result = agent.deliver(tool_agent_input)
                    if result.summary:
                        summaries.append(f"{agent_kind.value}: {result.summary}")
                    logger.info("Deliver: %s completed", agent_kind.value)
                except Exception as e:
                    logger.warning(
                        "Deliver: %s failed: %s. Next step -> Continuing with other agents",
                        agent_kind.value, e,
                    )
    
    # Finalize the product spec: rename updated_spec.md to product_spec.md
    # and clean up intermediate versioned files
    final_spec_content = _finalize_product_spec(repo_path, spec_content)
    
    try:
        if (repo_path / ".git").exists():
            subprocess.run(
                ["git", "add", "plan/"],
                cwd=repo_path,
                check=False,
                capture_output=True,
            )
            
            if (repo_path / "plan").exists():
                subprocess.run(
                    ["git", "add", "plan/*"],
                    cwd=repo_path,
                    check=False,
                    capture_output=True,
                )
            
            r = subprocess.run(
                ["git", "commit", "-m", "chore: planning artifacts"],
                cwd=repo_path,
                capture_output=True,
                text=True,
            )
            committed = r.returncode == 0
            if committed:
                logger.info("Deliver: committed planning artifacts")
            elif "nothing to commit" in (r.stdout + r.stderr):
                logger.info("Deliver: no changes to commit")
                committed = True
    except Exception as e:
        logger.warning(
            "Deliver git commit failed (non-fatal): %s. Next step -> Continuing to build summary",
            e,
        )
    
    hierarchy_summary = ""
    if hierarchy:
        init_count = len(hierarchy.initiatives)
        epic_count = sum(len(i.epics) for i in hierarchy.initiatives)
        story_count = sum(len(e.stories) for i in hierarchy.initiatives for e in i.epics)
        task_count = sum(len(s.tasks) for i in hierarchy.initiatives for e in i.epics for s in e.stories)
        hierarchy_summary = f" Hierarchy: {init_count} initiatives, {epic_count} epics, {story_count} stories, {task_count} tasks."
    
    summary = "Planning-v2 artifacts delivered."
    if implementation_result and implementation_result.assets_created:
        summary = f"Deliver complete. Assets: {', '.join(implementation_result.assets_created[:5])}."
        if len(implementation_result.assets_created) > 5:
            summary += f" (+{len(implementation_result.assets_created) - 5} more)"
    
    summary += hierarchy_summary
    
    if committed:
        summary += " Changes committed."
    
    return DeliverPhaseResult(
        committed=committed,
        summary=summary,
        final_spec_content=final_spec_content,
    )


def _finalize_product_spec(repo_path: Path, fallback_spec: str) -> str:
    """
    Finalize the product spec by:
    1. Reading the final updated_spec.md from plan/planning_team
    2. Writing it to product_spec.md in plan/planning_team
    3. Deleting updated_spec_v*.md only under plan/planning_team
    
    Returns the final spec content.
    """
    plan_dir = repo_path / PLAN_PLANNING_TEAM_DIR
    if not plan_dir.exists():
        logger.info("Deliver: No plan/planning_team directory, using original spec content")
        return fallback_spec

    updated_spec_file = plan_dir / "updated_spec.md"
    product_spec_file = plan_dir / "product_spec.md"

    # Read the final spec content
    if updated_spec_file.exists():
        try:
            final_content = updated_spec_file.read_text(encoding="utf-8")
            logger.info("Deliver: Read final spec from %s", updated_spec_file)
        except Exception as e:
            logger.warning("Deliver: Failed to read updated_spec.md: %s", e)
            final_content = fallback_spec
    else:
        logger.info("Deliver: No updated_spec.md in plan/planning_team, using original spec content")
        final_content = fallback_spec

    # Write to product_spec.md under plan/planning_team
    try:
        product_spec_file.write_text(final_content, encoding="utf-8")
        logger.info("Deliver: Wrote product_spec.md in plan/planning_team")
    except Exception as e:
        logger.warning("Deliver: Failed to write product_spec.md: %s", e)

    # Delete updated_spec.md if it exists (now that we have product_spec.md)
    if updated_spec_file.exists():
        try:
            updated_spec_file.unlink()
            logger.info("Deliver: Deleted updated_spec.md")
        except Exception as e:
            logger.warning("Deliver: Failed to delete updated_spec.md: %s", e)

    # Delete intermediate updated_spec_v*.md only under plan/planning_team
    for versioned_file in plan_dir.glob("updated_spec_v*.md"):
        try:
            versioned_file.unlink()
            logger.info("Deliver: Deleted %s", versioned_file.name)
        except Exception as e:
            logger.warning("Deliver: Failed to delete %s: %s", versioned_file.name, e)

    return final_content
