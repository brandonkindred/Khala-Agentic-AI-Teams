"""
Implementation phase: create or update planning assets.

Tool agents: All 8 (System Design, Architecture, User Story, DevOps, UI, UX, Task Classification, Task Dependency).
Note: Task Dependency only participates in Review per the matrix, but we include others in Implementation.

When review_result contains issues, they are passed to tool agents for fixing.
Agents decide how to handle fixes (batch, one-by-one, or all at once).

Document Ownership: Each tool agent is responsible for its own document lifecycle.
- If a document already exists and there are review issues, agents apply targeted fixes
- Only if no document exists does the agent generate from scratch
- This prevents overwriting fixes made in previous iterations
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from software_engineering_team.shared.llm import LLMClient
from software_engineering_team.shared.models import PlanningHierarchy

from ..models import (
    ImplementationPhaseResult,
    PLAN_PLANNING_TEAM_DIR,
    PlanningPhaseResult,
    ReviewPhaseResult,
    SpecReviewResult,
    ToolAgentKind,
    ToolAgentPhaseInput,
)
from ..tool_agents.user_story.agent import _hierarchy_to_markdown

logger = logging.getLogger(__name__)


def _read_planning_artifacts(repo_path: Path) -> Dict[str, str]:
    """Read existing planning artifacts from plan/planning_team for update semantics.
    
    This allows agents to see their current documents and apply targeted fixes
    instead of regenerating from scratch.
    """
    files: Dict[str, str] = {}
    plan_dir = repo_path / PLAN_PLANNING_TEAM_DIR
    if plan_dir.exists():
        for f in plan_dir.glob("*.md"):
            try:
                content = f.read_text(encoding="utf-8")
                files[str(f.relative_to(repo_path))] = content
            except Exception:
                pass
    return files


def run_implementation(
    llm: LLMClient,
    spec_content: str,
    repo_path: Path,
    spec_review_result: Optional[SpecReviewResult] = None,
    planning_result: Optional[PlanningPhaseResult] = None,
    inspiration_content: Optional[str] = None,
    tool_agents: Optional[Dict[ToolAgentKind, Any]] = None,
    hierarchy: Optional[PlanningHierarchy] = None,
    review_result: Optional[ReviewPhaseResult] = None,
) -> ImplementationPhaseResult:
    """
    Run Implementation phase with all participating tool agents.
    
    Tool agents: System Design, Architecture, User Story, DevOps, UI Design, UX Design, Task Classification.
    
    Args:
        review_result: If provided and contains issues, they are passed to tool agents for fixing.
                      Agents decide how to handle fixes (batch, one-by-one, or all at once).
    """
    assets_created: list[str] = []
    assets_updated: list[str] = []
    current_files = _read_planning_artifacts(repo_path)
    all_files: Dict[str, str] = dict(current_files)
    
    try:
        repo_path.mkdir(parents=True, exist_ok=True)
        plan_dir = repo_path / PLAN_PLANNING_TEAM_DIR
        plan_dir.mkdir(parents=True, exist_ok=True)
        
        effective_hierarchy = hierarchy
        if planning_result and planning_result.hierarchy:
            effective_hierarchy = planning_result.hierarchy
        
        if current_files:
            logger.info(
                "Implementation: read %d existing planning artifacts for update semantics",
                len(current_files),
            )
        
        metadata: Dict[str, Any] = {}
        if planning_result:
            pass
        
        review_issues: List[str] = []
        if review_result and review_result.issues:
            review_issues = review_result.issues
            logger.info(
                "Implementation: received %d review issues to address",
                len(review_issues),
            )
        
        tool_agent_input = ToolAgentPhaseInput(
            spec_content=spec_content,
            inspiration_content=inspiration_content or "",
            repo_path=str(repo_path),
            spec_review_result=spec_review_result,
            planning_result=planning_result,
            review_result=review_result,
            review_issues=review_issues,
            hierarchy=effective_hierarchy,
            metadata=metadata,
            current_files=current_files,
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

        def _run_one_execute(agent_kind: ToolAgentKind) -> Tuple[ToolAgentKind, Any, Optional[Exception]]:
            agent = tool_agents.get(agent_kind) if tool_agents else None
            if agent and hasattr(agent, "execute"):
                try:
                    result = agent.execute(tool_agent_input)
                    return (agent_kind, result, None)
                except Exception as e:
                    return (agent_kind, None, e)
            return (agent_kind, None, None)

        agent_written: set = set()
        if tool_agents:
            # Tool agents run in parallel; each writes only to its own file(s), so no coordination required.
            with ThreadPoolExecutor(max_workers=len(participating_agents)) as executor:
                futures = {
                    executor.submit(_run_one_execute, kind): kind
                    for kind in participating_agents
                }
                for future in as_completed(futures):
                    agent_kind, result, exc = future.result()
                    if exc:
                        logger.warning(
                            "Implementation: %s execute failed: %s. Next step -> Continuing with other agents",
                            agent_kind.value, exc,
                        )
                        continue
                    if result:
                        if result.files_written:
                            agent_written.update(result.files_written)
                        if result.files:
                            all_files.update(result.files)
                            file_names = [Path(p).name for p in result.files]
                            logger.info(
                                "Implementation: %s generated %d file(s) (writing to: %s)",
                                agent_kind.value,
                                len(result.files),
                                ", ".join(file_names),
                            )
        
        for rel_path, content in all_files.items():
            if rel_path in agent_written:
                logger.info(
                    "Implementation: skipped %s (file already written by tool agent during this phase)",
                    Path(rel_path).name,
                )
                continue
            full_path = repo_path / rel_path
            full_path.parent.mkdir(parents=True, exist_ok=True)
            file_name = Path(rel_path).name
            if rel_path in current_files:
                if content == current_files[rel_path]:
                    logger.info(
                        "Implementation: preserved %s (content unchanged, no write performed)",
                        file_name,
                    )
                else:
                    full_path.write_text(content, encoding="utf-8")
                    assets_updated.append(rel_path)
                    logger.info(
                        "Implementation: applied update — writing to file: %s; full contents:\n%s",
                        file_name,
                        content,
                    )
            else:
                full_path.write_text(content, encoding="utf-8")
                assets_created.append(rel_path)
                logger.info(
                    "Implementation: wrote new file: %s; full contents:\n%s",
                    file_name,
                    content,
                )
        
        parts = ["# Planning (v2) Artifacts\n\n"]
        if spec_review_result:
            parts.append("## Product Requirement Analysis\n")
            parts.append(spec_review_result.summary or "")
            parts.append("\n\n")
            if spec_review_result.issues:
                parts.append("### Issues Identified\n")
                for issue in spec_review_result.issues:
                    parts.append(f"- {issue}\n")
                parts.append("\n")
            if spec_review_result.product_gaps:
                parts.append("### Product Gaps\n")
                for gap in spec_review_result.product_gaps:
                    parts.append(f"- {gap}\n")
                parts.append("\n")

        if planning_result:
            parts.append("## Product Planning\n\n")

            if planning_result.goals_vision:
                parts.append("### Goals / Vision\n")
                parts.append(planning_result.goals_vision)
                parts.append("\n\n")

            if planning_result.constraints_limitations:
                parts.append("### Constraints and Limitations\n")
                parts.append(planning_result.constraints_limitations)
                parts.append("\n\n")

            if planning_result.key_features:
                parts.append("### Key Features\n")
                for feature in planning_result.key_features:
                    parts.append(f"- {feature}\n")
                parts.append("\n")

            if planning_result.milestones:
                parts.append("### Milestones\n")
                for m in planning_result.milestones:
                    parts.append(f"- {m}\n")
                parts.append("\n")

            if planning_result.architecture:
                parts.append("### Architecture\n")
                parts.append(planning_result.architecture)
                parts.append("\n\n")

            if planning_result.maintainability:
                parts.append("### Maintainability\n")
                parts.append(planning_result.maintainability)
                parts.append("\n\n")

            if planning_result.security:
                parts.append("### Security\n")
                parts.append(planning_result.security)
                parts.append("\n\n")

            if planning_result.file_system:
                parts.append("### File System\n")
                parts.append(planning_result.file_system)
                parts.append("\n\n")

            if planning_result.styling:
                parts.append("### Styling\n")
                parts.append(planning_result.styling)
                parts.append("\n\n")

            if planning_result.dependencies:
                parts.append("### Dependencies\n")
                for dep in planning_result.dependencies:
                    parts.append(f"- {dep}\n")
                parts.append("\n")

            if planning_result.microservices:
                parts.append("### Microservices\n")
                parts.append(planning_result.microservices)
                parts.append("\n\n")

            if planning_result.others:
                parts.append("### Others\n")
                parts.append(planning_result.others)
                parts.append("\n\n")
        
        if effective_hierarchy:
            parts.append("## Planning Hierarchy\n")
            parts.append(f"- {len(effective_hierarchy.initiatives)} Initiative(s)\n")
            epic_count = sum(len(i.epics) for i in effective_hierarchy.initiatives)
            story_count = sum(len(e.stories) for i in effective_hierarchy.initiatives for e in i.epics)
            task_count = sum(len(s.tasks) for i in effective_hierarchy.initiatives for e in i.epics for s in e.stories)
            parts.append(f"- {epic_count} Epic(s)\n")
            parts.append(f"- {story_count} Story(ies)\n")
            parts.append(f"- {task_count} Task(s)\n")
            parts.append(f"\nSee `planning_hierarchy.md` in `{PLAN_PLANNING_TEAM_DIR}` for detailed hierarchy.\n")
            
            # Write full hierarchy to plan/planning_team/planning_hierarchy.md
            try:
                hierarchy_md = _hierarchy_to_markdown(effective_hierarchy)
                hierarchy_file = plan_dir / "planning_hierarchy.md"
                hierarchy_file.write_text(hierarchy_md, encoding="utf-8")
                hierarchy_rel_path = str(hierarchy_file.relative_to(repo_path))
                if hierarchy_rel_path not in assets_created:
                    assets_created.append(hierarchy_rel_path)
                logger.info(
                    "Implementation: wrote full planning hierarchy to file: %s; full contents:\n%s",
                    hierarchy_file.name,
                    hierarchy_md,
                )
            except Exception as e:
                logger.warning(
                    "Implementation: failed to write hierarchy file: %s. Next step -> Continuing with main artifacts",
                    e,
                )
        
        out_file = plan_dir / "planning_artifacts.md"
        artifacts_content = "".join(parts)
        out_file.write_text(artifacts_content, encoding="utf-8")
        rel_path = str(out_file.relative_to(repo_path))
        if rel_path not in assets_created:
            assets_created.append(rel_path)
        logger.info(
            "Implementation: wrote consolidated planning artifacts to file: %s; full contents:\n%s",
            out_file.name,
            artifacts_content,
        )
        
    except Exception as e:
        logger.warning("Implementation write failed: %s", e)

    return ImplementationPhaseResult(
        assets_created=assets_created,
        assets_updated=assets_updated,
        summary=f"Implementation complete. Created {len(assets_created)} assets." if assets_created else "No assets written.",
    )
