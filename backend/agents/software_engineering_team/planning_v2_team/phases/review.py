"""
Review phase: ensure plan assets are cohesive and aligned with spec.

Tool agents: System Design, Architecture, User Story, Task Dependency.

Uses universal truncation handling via complete_with_continuation.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

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
from ..output_templates import parse_review_output
from ..prompts import REVIEW_PROMPT
from ..tool_agents.json_utils import complete_with_continuation

logger = logging.getLogger(__name__)


def _read_planning_artifacts(repo_path: Path) -> Dict[str, str]:
    """Read planning artifacts from plan/planning_team for review."""
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


def run_review(
    llm: LLMClient,
    spec_content: str,
    repo_path: Path,
    spec_review_result: Optional[SpecReviewResult] = None,
    planning_result: Optional[PlanningPhaseResult] = None,
    implementation_result: Optional[ImplementationPhaseResult] = None,
    tool_agents: Optional[Dict[ToolAgentKind, Any]] = None,
    hierarchy: Optional[PlanningHierarchy] = None,
) -> ReviewPhaseResult:
    """
    Run Review phase with participating tool agents.
    
    Tool agents: System Design, Architecture, User Story, Task Dependency.
    """
    all_issues: list[str] = []
    current_files = _read_planning_artifacts(repo_path)
    
    effective_hierarchy = hierarchy
    if planning_result and planning_result.hierarchy:
        effective_hierarchy = planning_result.hierarchy
    
    tool_agent_input = ToolAgentPhaseInput(
        spec_content=spec_content,
        repo_path=str(repo_path),
        spec_review_result=spec_review_result,
        planning_result=planning_result,
        implementation_result=implementation_result,
        current_files=current_files,
        hierarchy=effective_hierarchy,
    )
    
    participating_agents = [
        ToolAgentKind.SYSTEM_DESIGN,
        ToolAgentKind.ARCHITECTURE,
        ToolAgentKind.USER_STORY,
        ToolAgentKind.TASK_DEPENDENCY,
    ]

    def _run_one_review(agent_kind: ToolAgentKind) -> Tuple[ToolAgentKind, Any, Optional[Exception]]:
        agent = tool_agents.get(agent_kind) if tool_agents else None
        if agent and hasattr(agent, "review"):
            try:
                result = agent.review(tool_agent_input)
                return (agent_kind, result, None)
            except Exception as e:
                return (agent_kind, None, e)
        return (agent_kind, None, None)

    if tool_agents:
        with ThreadPoolExecutor(max_workers=len(participating_agents)) as executor:
            futures = {
                executor.submit(_run_one_review, kind): kind
                for kind in participating_agents
            }
            for future in as_completed(futures):
                agent_kind, result, exc = future.result()
                if exc:
                    logger.warning("Review: %s review failed: %s", agent_kind.value, exc)
                    continue
                if result:
                    all_issues.extend(result.issues)
                    logger.info("Review: %s found %d issues", agent_kind.value, len(result.issues))
                    if result.files:
                        for rel_path, content in result.files.items():
                            full_path = repo_path / rel_path
                            full_path.parent.mkdir(parents=True, exist_ok=True)
                            full_path.write_text(content, encoding="utf-8")
                            file_name = Path(rel_path).name
                            logger.info(
                                "Review: %s applied fix — writing to file: %s; full contents:\n%s",
                                agent_kind.value,
                                file_name,
                                content,
                            )
    
    artifacts_text = "\n".join(
        f"--- {path} ---\n{content[:2000]}"
        for path, content in list(current_files.items())[:5]
    )[:6000]
    
    prompt = REVIEW_PROMPT.format(
        spec_content=(spec_content or "")[:6000],
        artifacts=artifacts_text,
    )
    try:
        raw_text = complete_with_continuation(
            llm=llm,
            prompt=prompt,
            agent_name="PlanningV2_Review",
        )
        raw = parse_review_output(raw_text)
        llm_issues = raw.get("issues") or []
        if isinstance(llm_issues, list):
            all_issues.extend(llm_issues)

        llm_passed = bool(raw.get("passed", True))
        final_passed = llm_passed and len(all_issues) == 0

        logger.info("Review: total %d issues, passed=%s", len(all_issues), final_passed)

        return ReviewPhaseResult(
            passed=final_passed,
            issues=list(set(all_issues)),
            summary=str(raw.get("summary", "") or f"Review complete. {len(all_issues)} issue(s) found."),
        )
    except Exception as e:
        logger.warning("Review LLM call failed, using tool agent results: %s", e)
        passed = len(all_issues) == 0
        return ReviewPhaseResult(
            passed=passed,
            issues=all_issues,
            summary="Review completed with tool agents.",
        )
