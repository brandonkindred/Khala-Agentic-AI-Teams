"""
Tech Lead orchestrator: runs the full pipeline with feature branches.

Planning flow:
1. Review initial_spec and document features and functionalities (high level) via Project Planning.
2. Create detailed tasks from spec + features/functionality doc (Tech Lead).
3. Produce architecture from features/functionality doc + spec (Architecture Expert).
4. Loop steps 2 and 3 until tasks and architecture align (alignment review).
5. Review tasks and architecture for conformance to initial_spec; if non-compliant, go to step 2
   with a detailed list of issues; if compliant, proceed to execution.

Execution:
- Prefix tasks (devops, git_setup) run sequentially on work path.
- Backend and frontend tasks run in parallel (one task per agent type at a time),
  each in its own repo (work_path/backend, work_path/frontend) initialized by Git Setup Agent.
"""

from __future__ import annotations

import logging
import os
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx

# Path setup when run as module
import sys
_team_dir = Path(__file__).resolve().parent
if str(_team_dir) not in sys.path:
    sys.path.insert(0, str(_team_dir))

from shared.git_utils import (
    DEVELOPMENT_BRANCH,
    checkout_branch,
    create_feature_branch,
    delete_branch,
    ensure_development_branch,
    merge_branch,
)
from shared.llm import (
    LLMError,
    LLMPermanentError,
    LLMRateLimitError,
    LLMTemporaryError,
    OLLAMA_WEEKLY_LIMIT_MESSAGE,
    get_llm_for_agent,
)
from shared.job_store import (
    JOB_STATUS_AGENT_CRASH,
    JOB_STATUS_COMPLETED,
    JOB_STATUS_FAILED,
    JOB_STATUS_RUNNING,
    update_job,
)
from shared.command_runner import run_command_with_nvm
from shared.execution_tracker import execution_tracker
from planning_team.plan_dir import ensure_plan_dir
from shared.development_plan_writer import (
    write_architecture_plan,
    write_features_and_functionality_plan,
    write_project_overview_plan,
    write_tech_lead_plan,
)
from shared.models import TaskUpdate, model_to_dict
from shared.repo_writer import write_agent_output

logger = logging.getLogger(__name__)

BANNER_WIDTH = 72

# Exceptions that the repair agent can attempt to fix (code errors in agent framework)
REPAIRABLE_EXCEPTIONS = (
    NameError,
    SyntaxError,
    ImportError,
    AttributeError,
    IndentationError,
    ModuleNotFoundError,
)


def _log_task_completion_banner(
    task_id: str,
    task_title: str,
    assignee: str,
    elapsed_seconds: float,
    log_prefix: str = "",
) -> None:
    """Log a big, flashy banner when a task is considered complete."""
    title_display = (task_title[:50] + "...") if len(task_title) > 53 else task_title
    header = "  ★★★  TASK COMPLETE  ★★★" + ("  [RETRY]" if log_prefix else "")
    logger.info("")
    logger.info("*" * BANNER_WIDTH)
    logger.info(header)
    logger.info("  Task:    %s", task_id)
    logger.info("  Title:   %s", title_display)
    logger.info("  Assignee: %s", assignee)
    logger.info("  Elapsed:  %.1fs", elapsed_seconds)
    logger.info("*" * BANNER_WIDTH)
    logger.info("")


def _parse_traceback_for_crash(exception: BaseException) -> tuple[str | None, int | None, str | None]:
    """
    Extract file_path, line_number, and function_name from the exception traceback.
    Returns the last frame (where the exception occurred) as (file_path, line_number, function_name).
    """
    tb = exception.__traceback__
    if tb is None:
        return None, None, None
    frames = traceback.extract_tb(tb)
    if not frames:
        return None, None, None
    last = frames[-1]
    # Use relative path for display (e.g. backend_agent/agent.py)
    filename = last.filename
    if filename:
        # Try to shorten to module-style path
        for part in ("software_engineering_team", "agent_implementations"):
            if part in filename:
                idx = filename.find(part)
                filename = filename[idx:]
                break
    return filename, last.lineno, last.name or None


def _log_agent_crash_banner(
    task_id: str,
    agent_type: str,
    exception: BaseException,
    log_prefix: str = "",
) -> None:
    """Log a prominent banner when an agent process crashes with an unhandled exception."""
    file_path, line_number, func_name = _parse_traceback_for_crash(exception)
    exc_type = type(exception).__name__
    exc_msg = str(exception)
    location = ""
    if file_path and line_number:
        location = f"{file_path}:{line_number}"
        if func_name:
            location += f" in {func_name}"
    sep = "!" * BANNER_WIDTH
    logger.error("")
    logger.error(sep)
    logger.error("  *** AGENT CRASH (%s) ***%s", agent_type.capitalize(), "  [RETRY]" if log_prefix else "")
    logger.error("  Task: %s", task_id)
    logger.error("  Exception: %s: %s", exc_type, exc_msg)
    if location:
        logger.error("  Location: %s", location)
    logger.error(sep)
    logger.error("")


def _apply_repair_fixes(agent_source_path: Path, suggested_fixes: list) -> bool:
    """
    Apply suggested fixes from the repair agent. Validates that all file paths
    are under agent_source_path. Returns True if any fix was applied.
    """
    agent_root = Path(agent_source_path).resolve()
    applied = False
    for fix in suggested_fixes:
        fp = fix.get("file_path")
        if not fp:
            continue
        target = (agent_root / fp).resolve() if not Path(fp).is_absolute() else Path(fp).resolve()
        try:
            if not str(target).startswith(str(agent_root)):
                logger.warning("Repair: rejecting path outside agent tree: %s", fp)
                continue
            if not target.exists():
                logger.warning("Repair: file does not exist: %s", target)
                continue
            line_start = int(fix.get("line_start", 1))
            line_end = int(fix.get("line_end", line_start))
            replacement = fix.get("replacement_content", "")
            lines = target.read_text(encoding="utf-8").splitlines(keepends=True)
            if line_start < 1 or line_end > len(lines):
                logger.warning("Repair: line range %d-%d out of bounds for %s", line_start, line_end, target)
                continue
            # 1-based to 0-based
            new_content = "".join(lines[: line_start - 1]) + replacement + "".join(lines[line_end:])
            target.write_text(new_content, encoding="utf-8")
            logger.info("Repair: applied fix to %s lines %d-%d", target, line_start, line_end)
            applied = True
        except Exception as e:
            logger.warning("Repair: failed to apply fix to %s: %s", fp, e)
    return applied


def _log_task_breakdown(
    completed: set,
    all_tasks: dict,
    total_tasks: int,
    failed_count: int = 0,
    job_id: str | None = None,
) -> None:
    """Log task count breakdown by assignee (backend, frontend, devops, git_setup, etc.)."""
    breakdown: Dict[str, int] = {}
    for tid in completed:
        t = all_tasks.get(tid)
        if t:
            assignee = getattr(t, "assignee", None) or getattr(t, "type", None) or "unknown"
            if isinstance(assignee, object) and hasattr(assignee, "value"):
                assignee = assignee.value
            breakdown[assignee] = breakdown.get(assignee, 0) + 1

    # Normalize assignee labels for display
    labels = {
        "backend": "Backend",
        "frontend": "Frontend",
        "devops": "DevOps",
        "git_setup": "Git Setup",
        "documentation": "Documentation",
        "security": "Security",
        "qa": "QA",
    }
    logger.info("")
    logger.info("=" * BANNER_WIDTH)
    logger.info("  ★★★  TASK BREAKDOWN  ★★★")
    if job_id:
        logger.info("  Job: %s", job_id)
    logger.info("  Total: %d completed | %d failed | %d total", len(completed), failed_count, total_tasks)
    logger.info("-" * BANNER_WIDTH)
    for key in ["backend", "frontend", "devops", "git_setup", "documentation", "security", "qa"]:
        count = breakdown.get(key, 0)
        if count > 0:
            label = labels.get(key, key.replace("_", " ").title())
            logger.info("  %-14s %d", label + ":", count)
    for key, count in sorted(breakdown.items()):
        if key not in labels:
            logger.info("  %-14s %d", key.replace("_", " ").title() + ":", count)
    logger.info("=" * BANNER_WIDTH)
    logger.info("")


def _get_agents():
    """Lazy init agents including the code review, documentation, and DbC comments agents.
    Each agent uses get_llm_for_agent(key) for per-agent model configuration."""
    from frontend_team.accessibility_agent import AccessibilityExpertAgent, AccessibilityInput
    from architecture_agent import ArchitectureExpertAgent, ArchitectureInput
    from backend_agent import BackendExpertAgent, BackendInput
    from planning_team.project_planning_agent import ProjectPlanningAgent, ProjectPlanningInput
    from code_review_agent import CodeReviewAgent, CodeReviewInput
    from dbc_comments_agent import DbcCommentsAgent, DbcCommentsInput
    from devops_agent import DevOpsExpertAgent, DevOpsInput
    from documentation_agent import DocumentationAgent, DocumentationInput
    from frontend_team.feature_agent import FrontendExpertAgent, FrontendInput
    from git_setup_agent import GitSetupAgent
    from integration_agent import IntegrationAgent, IntegrationInput
    from qa_agent import QAExpertAgent, QAInput
    from security_agent import CybersecurityExpertAgent, SecurityInput
    from planning_team.api_contract_planning_agent import ApiContractPlanningAgent
    from planning_team.data_architecture_agent import DataArchitectureAgent
    from planning_team.devops_planning_agent import DevOpsPlanningAgent
    from planning_team.frontend_architecture_agent import FrontendArchitectureAgent
    from planning_team.infrastructure_planning_agent import InfrastructurePlanningAgent
    from planning_team.observability_planning_agent import ObservabilityPlanningAgent
    from planning_team.performance_planning_doc_agent import PerformancePlanningDocAgent
    from planning_team.qa_test_strategy_agent import QaTestStrategyAgent
    from planning_team.security_planning_agent import SecurityPlanningAgent
    from planning_team.spec_intake_agent import SpecIntakeAgent, SpecIntakeInput, validated_spec_to_requirements
    from tech_lead_agent import TechLeadAgent, TechLeadInput
    from planning_team.ui_ux_design_agent import UiUxDesignAgent
    from acceptance_verifier_agent import AcceptanceVerifierAgent
    from repair_agent import RepairExpertAgent, RepairInput

    return {
        "spec_intake": SpecIntakeAgent(get_llm_for_agent("spec_intake")),
        "project_planning": ProjectPlanningAgent(get_llm_for_agent("project_planning")),
        "architecture": ArchitectureExpertAgent(get_llm_for_agent("architecture")),
        "api_contract": ApiContractPlanningAgent(get_llm_for_agent("api_contract")),
        "data_architecture": DataArchitectureAgent(get_llm_for_agent("data_architecture")),
        "ui_ux": UiUxDesignAgent(get_llm_for_agent("ui_ux")),
        "frontend_architecture": FrontendArchitectureAgent(get_llm_for_agent("frontend_architecture")),
        "infrastructure": InfrastructurePlanningAgent(get_llm_for_agent("infrastructure")),
        "devops_planning": DevOpsPlanningAgent(get_llm_for_agent("devops_planning")),
        "qa_test_strategy": QaTestStrategyAgent(get_llm_for_agent("qa_test_strategy")),
        "security_planning": SecurityPlanningAgent(get_llm_for_agent("security_planning")),
        "observability": ObservabilityPlanningAgent(get_llm_for_agent("observability")),
        "integration": IntegrationAgent(get_llm_for_agent("integration")),
        "acceptance_verifier": AcceptanceVerifierAgent(get_llm_for_agent("acceptance_verifier")),
        "tech_lead": TechLeadAgent(get_llm_for_agent("tech_lead")),
        "devops": DevOpsExpertAgent(get_llm_for_agent("devops")),
        "backend": BackendExpertAgent(get_llm_for_agent("backend")),
        "frontend": FrontendExpertAgent(get_llm_for_agent("frontend")),
        "security": CybersecurityExpertAgent(get_llm_for_agent("security")),
        "qa": QAExpertAgent(get_llm_for_agent("qa")),
        "accessibility": AccessibilityExpertAgent(get_llm_for_agent("accessibility")),
        "code_review": CodeReviewAgent(get_llm_for_agent("code_review")),
        "dbc_comments": DbcCommentsAgent(get_llm_for_agent("dbc_comments")),
        "documentation": DocumentationAgent(get_llm_for_agent("documentation")),
        "git_setup": GitSetupAgent(),
        "repair": RepairExpertAgent(get_llm_for_agent("repair")),
    }


def _task_requirements(task) -> str:
    """Build full requirements string including description, user story, requirements, and acceptance criteria."""
    parts = []
    if task.description:
        parts.append(f"Task Description:\n{task.description}")
    if getattr(task, "user_story", None):
        parts.append(f"User Story: {task.user_story}")
    if task.requirements:
        parts.append(f"Technical Requirements:\n{task.requirements}")
    if getattr(task, "acceptance_criteria", None):
        parts.append("Acceptance Criteria:\n- " + "\n- ".join(task.acceptance_criteria))
    return "\n\n".join(parts) if parts else task.description


MAX_REVIEW_ITERATIONS = 20
MAX_CLARIFICATION_REFINEMENTS = 20  # Max times to refine a task based on specialist clarification
MAX_CODE_REVIEW_ITERATIONS = 20    # Max rounds of code review -> fix -> re-review


def _issues_to_dicts(qa_bugs, sec_vulns) -> tuple:
    """Convert QA/Security outputs to dict lists for coding agent input."""
    qa_list = [b.model_dump() if hasattr(b, "model_dump") else b.dict() for b in (qa_bugs or [])]
    sec_list = [v.model_dump() if hasattr(v, "model_dump") else v.dict() for v in (sec_vulns or [])]
    return qa_list, sec_list


# When reading frontend-like extensions, exclude dirs that bloat payload (request body too large)
_READ_REPO_EXCLUDE_PARTS = frozenset({".git", "node_modules", "dist", ".angular"})


def _read_repo_code(repo_path: Path, extensions: List[str] = None) -> str:
    """Read code files from repo, concatenated. Excludes .git, and for frontend paths node_modules/dist/.angular."""
    if extensions is None:
        extensions = [".py", ".ts", ".tsx", ".java", ".yml", ".yaml"]
    frontend_exts = {".ts", ".tsx", ".html", ".scss"}
    use_frontend_exclusions = bool(set(extensions) & frontend_exts)
    parts = []
    for f in repo_path.rglob("*"):
        if ".git" in f.parts:
            continue
        if use_frontend_exclusions and _READ_REPO_EXCLUDE_PARTS & set(f.parts):
            continue
        if f.is_file() and f.suffix in extensions:
            try:
                parts.append(f"### {f.relative_to(repo_path)} ###\n{f.read_text(encoding='utf-8', errors='replace')}")
            except Exception:
                pass
    return "\n\n".join(parts) if parts else "# No code files found"


def _truncate_for_context(text: str, max_chars: int) -> str:
    """Truncate text for agent context, with truncation notice."""
    if not text or len(text) <= max_chars:
        return text or ""
    return text[:max_chars] + f"\n\n... [truncated, {len(text) - max_chars} more chars]"


def _build_task_update(task_id: str, agent_type: str, result, status: str = "completed") -> TaskUpdate:
    """Construct a TaskUpdate from a specialist agent's output."""
    summary = getattr(result, "summary", "") or ""
    files_changed = list((getattr(result, "files", None) or {}).keys())
    if not files_changed:
        files_changed = list((getattr(result, "artifacts", None) or {}).keys())
    needs_followup = bool(getattr(result, "needs_clarification", False))
    return TaskUpdate(
        task_id=task_id,
        agent_type=agent_type,
        status=status,
        summary=summary,
        files_changed=files_changed,
        needs_followup=needs_followup,
    )


def _run_tier1_agent(
    agent_key: str,
    agents: dict,
    spec_content: str,
    arch_overview: str,
    plan_dir: Path,
    requirements: Any,
    features_and_functionality_doc: str,
    tenancy: str,
) -> Tuple[str, Optional[Any]]:
    """
    Run a single Tier 1 planning agent. Returns (agent_key, output_or_exc).
    Output is a dict with keys like infra_doc, data_lifecycle, ui_ux_doc, or None for agents that don't produce downstream inputs.
    """
    try:
        if agent_key == "api_contract" and agents.get("api_contract"):
            from planning_team.api_contract_planning_agent.models import ApiContractPlanningInput
            agents["api_contract"].run(ApiContractPlanningInput(
                spec_content=spec_content,
                architecture_overview=arch_overview,
                requirements_title=requirements.title,
                acceptance_criteria=requirements.acceptance_criteria or [],
                plan_dir=plan_dir,
            ))
            return (agent_key, None)
        if agent_key == "data_architecture" and agents.get("data_architecture"):
            from planning_team.data_architecture_agent.models import DataArchitectureInput
            data_out = agents["data_architecture"].run(DataArchitectureInput(
                spec_content=spec_content,
                architecture_overview=arch_overview,
                requirements_title=requirements.title,
                plan_dir=plan_dir,
            ))
            return (agent_key, {"data_lifecycle": data_out.data_lifecycle_policy or ""})
        if agent_key == "ui_ux" and agents.get("ui_ux"):
            from ui_ux_design_agent.models import UiUxDesignInput
            ui_out = agents["ui_ux"].run(UiUxDesignInput(
                spec_content=spec_content,
                requirements_title=requirements.title,
                features_doc=features_and_functionality_doc or "",
                plan_dir=plan_dir,
            ))
            ui_ux_doc = (ui_out.user_journeys or "") + "\n" + (ui_out.wireframes or "")
            return (agent_key, {"ui_ux_doc": ui_ux_doc})
        if agent_key == "infrastructure" and agents.get("infrastructure"):
            from planning_team.infrastructure_planning_agent.models import InfrastructurePlanningInput
            infra_out = agents["infrastructure"].run(InfrastructurePlanningInput(
                architecture_overview=arch_overview,
                tenancy_model=tenancy,
                requirements_title=requirements.title,
                plan_dir=plan_dir,
            ))
            infra_doc = (infra_out.cloud_diagram or "") + "\n" + (infra_out.environment_strategy or "")
            return (agent_key, {"infra_doc": infra_doc})
    except Exception as e:
        logger.debug("%s planning skipped: %s", agent_key.replace("_", " ").title(), e)
        return (agent_key, None)
    return (agent_key, None)


def _run_tier2_agent(
    agent_key: str,
    agents: dict,
    spec_content: str,
    arch_overview: str,
    plan_dir: Path,
    requirements: Any,
    req_ids: List[str],
    ui_ux_doc: str,
    infra_doc: str,
    data_lifecycle: str,
) -> Tuple[str, Optional[Any]]:
    """Run a single Tier 2 planning agent. Returns (agent_key, output_dict or None)."""
    try:
        if agent_key == "frontend_architecture" and agents.get("frontend_architecture"):
            from planning_team.frontend_architecture_agent.models import FrontendArchitectureInput
            agents["frontend_architecture"].run(FrontendArchitectureInput(
                spec_content=spec_content,
                architecture_overview=arch_overview,
                ui_ux_doc=ui_ux_doc,
                requirements_title=requirements.title,
                plan_dir=plan_dir,
            ))
            return (agent_key, None)
        if agent_key == "devops_planning" and agents.get("devops_planning"):
            from planning_team.devops_planning_agent.models import DevOpsPlanningInput
            dev_out = agents["devops_planning"].run(DevOpsPlanningInput(
                architecture_overview=arch_overview,
                infrastructure_doc=infra_doc,
                requirements_title=requirements.title,
                plan_dir=plan_dir,
            ))
            devops_doc = (dev_out.ci_pipeline or "") + "\n" + (dev_out.cd_pipeline or "")
            return (agent_key, {"devops_doc": devops_doc})
        if agent_key == "qa_test_strategy" and agents.get("qa_test_strategy"):
            from planning_team.qa_test_strategy_agent.models import QaTestStrategyInput
            agents["qa_test_strategy"].run(QaTestStrategyInput(
                spec_content=spec_content,
                architecture_overview=arch_overview,
                acceptance_criteria=requirements.acceptance_criteria or [],
                requirement_ids=req_ids,
                requirements_title=requirements.title,
                plan_dir=plan_dir,
            ))
            return (agent_key, None)
        if agent_key == "security_planning" and agents.get("security_planning"):
            from planning_team.security_planning_agent import SecurityPlanningInput
            agents["security_planning"].run(SecurityPlanningInput(
                spec_content=spec_content,
                architecture_overview=arch_overview,
                data_lifecycle=data_lifecycle,
                requirements_title=requirements.title,
                plan_dir=plan_dir,
            ))
            return (agent_key, None)
    except Exception as e:
        logger.debug("%s planning skipped: %s", agent_key.replace("_", " ").title(), e)
        return (agent_key, None)
    return (agent_key, None)


def _run_tier3_observability(
    agents: dict,
    arch_overview: str,
    infra_doc: str,
    devops_doc: str,
    requirements: Any,
    plan_dir: Path,
) -> None:
    """Run observability planning agent."""
    from planning_team.observability_planning_agent.models import ObservabilityPlanningInput
    agents["observability"].run(ObservabilityPlanningInput(
        architecture_overview=arch_overview,
        infrastructure_doc=infra_doc,
        devops_doc=devops_doc,
        requirements_title=requirements.title,
        plan_dir=plan_dir,
    ))


def _run_tier3_performance_doc(
    agents: dict,
    spec_content: str,
    arch_overview: str,
    requirements: Any,
    plan_dir: Path,
) -> None:
    """Run performance doc planning agent."""
    from planning_team.performance_planning_doc_agent.models import PerformancePlanningDocInput
    agents["performance_doc"].run(PerformancePlanningDocInput(
        spec_content=spec_content,
        architecture_overview=arch_overview,
        requirements_title=requirements.title,
        plan_dir=plan_dir,
    ))


def _run_dbc_comments_review(
    agents: dict,
    repo_path: Path,
    task_id: str,
    language: str,
    task_description: str,
    architecture,
) -> None:
    """
    Run the Design by Contract Comments agent on the current feature branch.
    Adds DbC-compliant comments to all methods, functions, and classes.
    Commits changes to the branch if any comments were added.

    Preconditions:
        - The current branch is the feature branch with code to review
        - agents dict contains a "dbc_comments" key

    Postconditions:
        - If comments were added, they are committed to the current branch
        - If code was already compliant, a praise message is logged
        - Any failures are logged but do not block the pipeline
    """
    from dbc_comments_agent.models import DbcCommentsInput
    from shared.git_utils import write_files_and_commit

    try:
        dbc_code = _read_repo_code(repo_path)
        if not dbc_code or dbc_code == "# No code files found":
            logger.info("[%s] DbC: no code files to review, skipping", task_id)
            return

        dbc_result = agents["dbc_comments"].run(DbcCommentsInput(
            code=dbc_code,
            language=language,
            task_description=task_description,
            architecture=architecture,
        ))

        if not dbc_result.already_compliant and dbc_result.files:
            ok, msg = write_files_and_commit(
                repo_path,
                dbc_result.files,
                dbc_result.suggested_commit_message,
            )
            if ok:
                logger.info(
                    "[%s] DbC: added %s comments, updated %s -- committed to branch",
                    task_id,
                    dbc_result.comments_added,
                    dbc_result.comments_updated,
                )
            else:
                logger.warning("[%s] DbC: commit failed: %s", task_id, msg)
        else:
            logger.info(
                "[%s] DbC: code complies with Design by Contract -- great job coding!",
                task_id,
            )
    except Exception as e:
        # Non-blocking: DbC failure should never stop the pipeline
        logger.warning("[%s] DbC: review failed (non-blocking): %s", task_id, e)


def _run_tech_lead_review(
    tech_lead,
    task_update: TaskUpdate,
    spec_content: str,
    architecture,
    all_tasks: dict,
    completed: set,
    execution_queue: list,
    repo_path: Path,
    doc_agent=None,
    append_task_id_fn=None,
) -> None:
    """
    Ask the Tech Lead to review progress after a task completes.
    If the Tech Lead identifies gaps, new tasks are added to the execution queue
    (or to the queue provided via append_task_id_fn when running in a worker).
    After review, the Tech Lead triggers the Documentation Agent if available.
    """
    from shared.context_sizing import compute_existing_code_chars

    completed_tasks = [t for tid, t in all_tasks.items() if tid in completed]
    remaining_ids = set(execution_queue)
    remaining_tasks = [t for tid, t in all_tasks.items() if tid in remaining_ids]
    max_code_chars = compute_existing_code_chars(tech_lead.llm)
    codebase_summary = _truncate_for_context(_read_repo_code(repo_path), max_code_chars)

    new_tasks = tech_lead.review_progress(
        task_update=task_update,
        spec_content=spec_content,
        architecture=architecture,
        completed_tasks=completed_tasks,
        remaining_tasks=remaining_tasks,
        codebase_summary=codebase_summary,
    )

    if new_tasks:
        for nt in new_tasks:
            if nt.id not in all_tasks:
                all_tasks[nt.id] = nt
                if append_task_id_fn is not None:
                    append_task_id_fn(nt.id)
                else:
                    execution_queue.append(nt.id)
        logger.info(
            "Tech Lead review: added %s new tasks from progress review: %s",
            len(new_tasks),
            [t.id for t in new_tasks],
        )

    # Tech Lead triggers the Documentation Agent to update project docs
    if doc_agent:
        tech_lead.trigger_documentation_update(
            doc_agent=doc_agent,
            repo_path=repo_path,
            task_update=task_update,
            spec_content=spec_content,
            architecture=architecture,
            codebase_summary=codebase_summary,
        )


def _run_code_review(
    agents: dict,
    code_to_review: str,
    spec_content: str,
    task,
    language: str,
    architecture,
    existing_codebase: str | None = None,
):
    """
    Run the code review agent on the given code.
    Returns the CodeReviewOutput.
    """
    from shared.context_sizing import compute_code_review_total_chars
    from code_review_agent.models import CodeReviewInput

    llm = agents["code_review"].llm
    max_chars = compute_code_review_total_chars(llm)
    code_capped = _truncate_for_context(code_to_review, max_chars)
    review_input = CodeReviewInput(
        code=code_capped,
        spec_content=spec_content,
        task_description=task.description,
        task_requirements=_task_requirements(task),
        acceptance_criteria=getattr(task, "acceptance_criteria", []) or [],
        language=language,
        architecture=architecture,
        existing_codebase=existing_codebase,
    )
    return agents["code_review"].run(review_input)


def _code_review_issues_to_dicts(issues) -> list:
    """Convert CodeReviewIssue objects to dicts for coding agent input."""
    return [
        i.model_dump() if hasattr(i, "model_dump") else i.dict()
        for i in (issues or [])
    ]


def _log_code_review_result(review_result, task_id: str) -> None:
    """Log code review result with full issue details for debugging."""
    if review_result.approved:
        logger.info("[%s] Code review APPROVED", task_id)
        if review_result.summary:
            logger.info("[%s]   Summary: %s", task_id, review_result.summary[:300])
        return
    logger.warning(
        "[%s] Code review REJECTED: %s issues (%s critical/major)",
        task_id,
        len(review_result.issues),
        len([i for i in review_result.issues if i.severity in ("critical", "major")]),
    )
    for i, issue in enumerate(review_result.issues, 1):
        logger.warning(
            "[%s]   Issue %s: [%s] %s: %s (file: %s)",
            task_id, i, issue.severity, issue.category,
            issue.description, issue.file_path or "n/a",
        )
        if issue.suggestion:
            logger.warning(
                "[%s]     Suggestion: %s", task_id, issue.suggestion[:300],
            )
    if review_result.summary:
        logger.info("[%s]   Review summary: %s", task_id, review_result.summary[:300])
    if review_result.spec_compliance_notes:
        logger.info("[%s]   Spec compliance: %s", task_id, review_result.spec_compliance_notes[:300])
    if not review_result.issues:
        logger.warning(
            "[%s]   WARNING: Review rejected but returned 0 issues -- coding agent has nothing to fix!",
            task_id,
        )


def _run_build_verification(
    repo_path: Path,
    agent_type: str,
    task_id: str,
) -> tuple[bool, str]:
    """
    Run build verification for the given agent type.
    Returns (success, error_output).
    For frontend: runs ng build.
    For backend: runs python syntax check (pytest if tests exist).
    """
    from shared.command_runner import run_command, run_ng_build_with_nvm_fallback, run_python_syntax_check, run_pytest

    if agent_type == "frontend":
        # repo_path may be frontend repo root (package.json here) or work path (frontend/ subdir)
        frontend_dir = repo_path if (repo_path / "package.json").exists() else (repo_path / "frontend")
        if not (frontend_dir / "package.json").exists():
            logger.info("Build verification: no Angular project found, skipping ng build")
            return True, ""
        from shared.command_runner import is_ng_build_environment_failure
        result = run_ng_build_with_nvm_fallback(frontend_dir)
        if not result.success:
            if is_ng_build_environment_failure(result):
                # Environment (e.g. Node version) - caller should fail task, not retry
                return False, "ENV:" + result.error_summary
            failures = result.parsed_failures("ng_build")
            if failures:
                from shared.error_parsing import build_agent_feedback, get_failure_class_tag
                feedback = build_agent_feedback(failures)
                logger.warning(
                    "Build verification failed for task %s: %s",
                    task_id,
                    get_failure_class_tag(failures[0].failure_class),
                )
                return False, feedback
            logger.warning("Build verification failed for task %s: %s", task_id, result.error_summary[:200])
            return False, result.error_summary
        logger.info("Build verification passed for frontend task %s", task_id)
        return True, ""

    elif agent_type == "backend":
        # repo_path may be backend repo root (py files here) or work path (backend/ subdir)
        backend_dir = repo_path if any(repo_path.rglob("*.py")) else (repo_path / "backend")
        if not backend_dir.exists() or not any(backend_dir.rglob("*.py")):
            logger.info("Build verification: no Python files found, skipping")
            return True, ""
        result = run_python_syntax_check(backend_dir)
        if not result.success:
            logger.warning("Syntax check failed for task %s: %s", task_id, result.error_summary[:200])
            return False, result.error_summary
        # Also try pytest if tests directory exists
        tests_dir = backend_dir / "tests"
        if tests_dir.exists() and any(tests_dir.rglob("test_*.py")):
            # Install deps before pytest so agent-added packages (e.g. sqlalchemy) are available
            req_txt = backend_dir / "requirements.txt"
            if req_txt.exists():
                try:
                    pip_result = run_command(
                        [sys.executable, "-m", "pip", "install", "-r", "requirements.txt"],
                        cwd=backend_dir,
                        timeout=120,
                    )
                    if not pip_result.success:
                        logger.warning(
                            "pip install -r requirements.txt failed (non-fatal): %s",
                            pip_result.error_summary[:200],
                        )
                except Exception as e:
                    logger.warning("pip install before pytest failed (non-fatal): %s", e)
            test_result = run_pytest(backend_dir, python_exe=sys.executable)
            if not test_result.success:
                failures = test_result.parsed_failures("pytest")
                if failures:
                    from shared.error_parsing import build_agent_feedback, get_failure_class_tag
                    summary = build_agent_feedback(failures)
                    logger.warning(
                        "Tests failed for task %s: %s",
                        task_id,
                        get_failure_class_tag(failures[0].failure_class),
                    )
                else:
                    summary = test_result.pytest_error_summary()
                # When failure matches exception-handler test patterns, append canonical FIX line
                from backend_agent.agent import EXCEPTION_HANDLER_TEST_PATTERNS
                if any(p in summary for p in EXCEPTION_HANDLER_TEST_PATTERNS):
                    summary += (
                        "\n\nFIX: Preserve the /test-generic-error route in app/main.py and "
                        "ensure the exception handler returns JSONResponse; do not re-raise."
                    )
                return False, summary
        logger.info("Build verification passed for backend task %s", task_id)
        return True, ""

    elif agent_type == "devops":
        # Validate YAML files and run docker build if Dockerfile exists
        import yaml
        from shared.command_runner import run_command

        errors: list[str] = []
        # Validate .github/workflows/*.yml
        workflows_dir = repo_path / ".github" / "workflows"
        if workflows_dir.exists():
            for yml_file in workflows_dir.glob("*.yml"):
                try:
                    content = yml_file.read_text(encoding="utf-8", errors="replace")
                    yaml.safe_load(content)
                except yaml.YAMLError as e:
                    errors.append(f"YAML parse error in {yml_file.relative_to(repo_path)}: {e}")
                except Exception as e:
                    errors.append(f"Error reading {yml_file.relative_to(repo_path)}: {e}")
        # Validate top-level *.yml and *.yaml
        for pattern in ("*.yml", "*.yaml"):
            for yml_file in repo_path.glob(pattern):
                if yml_file.name.startswith("."):
                    continue
                try:
                    content = yml_file.read_text(encoding="utf-8", errors="replace")
                    yaml.safe_load(content)
                except yaml.YAMLError as e:
                    errors.append(f"YAML parse error in {yml_file.name}: {e}")
                except Exception as e:
                    errors.append(f"Error reading {yml_file.name}: {e}")
        if errors:
            return False, "\n".join(errors[:10])

        # Docker build if Dockerfile exists
        dockerfile = repo_path / "Dockerfile"
        if dockerfile.exists():
            result = run_command(
                ["docker", "build", "-t", "devops-verify", "."],
                cwd=repo_path,
                timeout=120,
            )
            if not result.success:
                logger.warning("Docker build failed for task %s: %s", task_id, result.error_summary[:200])
                return False, result.error_summary

        logger.info("Build verification passed for devops task %s", task_id)
        return True, ""

    return True, ""


def _pop_runnable_task(
    queue: List[str],
    all_tasks: Dict[str, Any],
    completed: set,
) -> Optional[str]:
    """
    Pop a task from the queue whose dependencies are all in completed.
    If none are runnable, return None (caller should wait and retry).
    Mutates queue by removing the task.
    """
    for i, task_id in enumerate(queue):
        task = all_tasks.get(task_id)
        if not task:
            continue
        deps = getattr(task, "dependencies", None) or []
        if all(dep in completed for dep in deps):
            queue.pop(i)
            return task_id
    return None


def _run_backend_frontend_workers(
    *,
    job_id: str,
    path: Path,
    backend_dir: Path,
    frontend_dir: Path,
    backend_queue: List[str],
    frontend_queue: List[str],
    all_tasks: Dict[str, Any],
    completed: set,
    failed: Dict[str, str],
    completed_code_task_ids: List[str],
    spec_content: str,
    architecture: Any,
    agents: Dict[str, Any],
    tech_lead: Any,
    total_tasks: int,
    is_retry: bool = False,
) -> None:
    """
    Run backend and frontend workers in parallel (1 backend task, 1 frontend task at a time).
    Mutates completed, failed, backend_queue, frontend_queue, all_tasks.
    """
    state_lock = threading.Lock()
    llm_limit_exceeded = [False]  # mutable ref for workers
    repaired_tasks = set()  # max 1 repair per task
    agent_source_path = Path(__file__).resolve().parent  # software_engineering_team/

    def _remaining_queue_ids() -> List[str]:
        with state_lock:
            return list(backend_queue) + list(frontend_queue)

    DEP_WAIT_SLEEP = 0.5  # seconds to wait when no runnable task (dependencies pending)

    def _backend_worker() -> None:
        while True:
            with state_lock:
                if llm_limit_exceeded[0]:
                    break
                if not backend_queue:
                    break
                task_id = _pop_runnable_task(backend_queue, all_tasks, completed)
            if task_id is None:
                # No runnable task; wait for dependencies (e.g. from frontend) then retry
                time.sleep(DEP_WAIT_SLEEP)
                continue
            task = all_tasks.get(task_id)
            if not task:
                continue
            update_job(job_id, current_task=task_id)
            execution_tracker.start_task(task_id)
            log_prefix = "[RETRY] " if is_retry else ""
            logger.info("%s[%s] >>> Backend worker starting task %s", log_prefix, task_id, task_id)
            task_start_time = time.monotonic()
            try:
                from shared.command_runner import ensure_backend_project_initialized
                init_result = ensure_backend_project_initialized(backend_dir)
                if not init_result.success:
                    with state_lock:
                        failed[task_id] = f"Backend init failed: {init_result.error_summary}"
                    continue
                if not (backend_dir / ".git").exists():
                    gs_result = agents["git_setup"].run(backend_dir)
                    if not gs_result.success:
                        with state_lock:
                            failed[task_id] = f"Git setup failed: {gs_result.message}"
                        continue
                completed_tasks_list = [t for tid, t in all_tasks.items() if tid in completed]
                remaining_ids = set(_remaining_queue_ids()) - {task_id}
                remaining_tasks_list = [t for tid, t in all_tasks.items() if tid in remaining_ids]

                def _append_backend_task(nt) -> None:
                    with state_lock:
                        all_tasks[nt.id] = nt
                        backend_queue.append(nt.id)

                workflow_result = agents["backend"].run_workflow(
                    repo_path=backend_dir,
                    task=task,
                    spec_content=spec_content,
                    architecture=architecture,
                    qa_agent=agents["qa"],
                    security_agent=agents["security"],
                    dbc_agent=agents["dbc_comments"],
                    code_review_agent=agents["code_review"],
                    acceptance_verifier_agent=agents.get("acceptance_verifier"),
                    tech_lead=tech_lead,
                    build_verifier=_run_build_verification,
                    doc_agent=agents.get("documentation"),
                    completed_tasks=completed_tasks_list,
                    remaining_tasks=remaining_tasks_list,
                    all_tasks=all_tasks,
                    execution_queue=backend_queue,
                    append_task_fn=_append_backend_task,
                )
                elapsed = time.monotonic() - task_start_time
                with state_lock:
                    if workflow_result.success:
                        completed.add(task_id)
                        completed_code_task_ids.append(task_id)
                        execution_tracker.observe_loop(task_id, 1)
                        execution_tracker.finish_task(task_id)
                        _log_task_completion_banner(
                            task_id=task_id,
                            task_title=getattr(task, "title", "") or task_id,
                            assignee="backend",
                            elapsed_seconds=elapsed,
                            log_prefix=log_prefix,
                        )
                    else:
                        failed[task_id] = workflow_result.failure_reason or "Backend workflow failed"
                        execution_tracker.observe_loop(task_id, 1)
                        execution_tracker.finish_task(task_id, blocked=True)
                        logger.warning("%s[%s] Backend FAILED after %.1fs: %s", log_prefix, task_id, elapsed, failed[task_id])
            except (LLMError, httpx.HTTPError) as e:
                with state_lock:
                    if isinstance(e, LLMRateLimitError):
                        llm_limit_exceeded[0] = True
                        failed[task_id] = OLLAMA_WEEKLY_LIMIT_MESSAGE
                    elif isinstance(e, LLMTemporaryError):
                        failed[task_id] = "LLM rate limited or temporarily unavailable – please retry later"
                    elif isinstance(e, LLMPermanentError):
                        failed[task_id] = str(e)
                    else:
                        failed[task_id] = f"LLM error: {e}"
                if isinstance(e, LLMRateLimitError):
                    logger.warning("Ollama LLM usage limit exceeded for week. Job %s paused.", job_id)
                else:
                    logger.warning("%s[%s] Backend task LLM/HTTP error: %s", log_prefix, task_id, e)
            except Exception as e:
                _log_agent_crash_banner(task_id, "backend", e, log_prefix)
                file_path, line_number, func_name = _parse_traceback_for_crash(e)
                agent_crash_details = {
                    "task_id": task_id,
                    "agent_type": "backend",
                    "exception_type": type(e).__name__,
                    "exception_message": str(e),
                    "traceback": traceback.format_exc(),
                    "file_path": file_path,
                    "line_number": line_number,
                    "function_name": func_name,
                }
                update_job(job_id, status=JOB_STATUS_AGENT_CRASH, error=str(e), agent_crash_details=agent_crash_details)
                repair_applied = False
                if type(e) in REPAIRABLE_EXCEPTIONS and task_id not in repaired_tasks:
                    repair_agent = agents.get("repair")
                    if repair_agent:
                        try:
                            from repair_agent.models import RepairInput
                            result = repair_agent.run(RepairInput(
                                traceback=traceback.format_exc(),
                                exception_type=type(e).__name__,
                                exception_message=str(e),
                                task_id=task_id,
                                agent_type="backend",
                                agent_source_path=agent_source_path,
                            ))
                            if result.suggested_fixes and _apply_repair_fixes(agent_source_path, result.suggested_fixes):
                                repair_applied = True
                                with state_lock:
                                    repaired_tasks.add(task_id)
                                    backend_queue.append(task_id)
                                update_job(job_id, status=JOB_STATUS_RUNNING, error=None, agent_crash_details=None)
                                logger.info("%s[%s] Repair applied, re-queued task", log_prefix, task_id)
                        except Exception as repair_err:
                            logger.warning("Repair agent failed for %s: %s", task_id, repair_err)
                if not repair_applied:
                    with state_lock:
                        failed[task_id] = f"Unhandled exception: {e}"
                logger.exception("%s[%s] Backend task exception", log_prefix, task_id)
            logger.info("%s[%s] <<< Backend worker done", log_prefix, task_id)

        # After backend agent is done with all tasks for this repo, containerize it
        devops_agent = agents.get("devops")
        if devops_agent and backend_dir.is_dir() and (backend_dir / ".git").exists():
            existing_pipeline = _read_repo_code(backend_dir, [".yml", ".yaml"])
            tech_lead.trigger_devops_for_backend(
                devops_agent, backend_dir, architecture, spec_content,
                existing_pipeline=existing_pipeline if existing_pipeline != "# No code files found" else None,
                build_verifier=_run_build_verification,
            )

    def _frontend_worker() -> None:
        while True:
            with state_lock:
                if llm_limit_exceeded[0]:
                    break
                if not frontend_queue:
                    break
                task_id = _pop_runnable_task(frontend_queue, all_tasks, completed)
            if task_id is None:
                # No runnable task; wait for dependencies (e.g. from backend) then retry
                time.sleep(DEP_WAIT_SLEEP)
                continue
            task = all_tasks.get(task_id)
            if not task:
                continue
            update_job(job_id, current_task=task_id)
            execution_tracker.start_task(task_id)
            log_prefix = "[RETRY] " if is_retry else ""
            logger.info("%s[%s] >>> Frontend worker starting task %s", log_prefix, task_id, task_id)
            task_start_time = time.monotonic()
            try:
                from shared.command_runner import ensure_frontend_project_initialized
                init_result = ensure_frontend_project_initialized(frontend_dir)
                if not init_result.success:
                    with state_lock:
                        failed[task_id] = f"Frontend init failed: {init_result.error_summary}"
                    continue
                if not (frontend_dir / ".git").exists():
                    gs_result = agents["git_setup"].run(frontend_dir)
                    if not gs_result.success:
                        with state_lock:
                            failed[task_id] = f"Git setup failed: {gs_result.message}"
                        continue

                completed_tasks_list = [t for tid, t in all_tasks.items() if tid in completed]
                remaining_ids = set(_remaining_queue_ids())
                remaining_tasks_list = [t for tid, t in all_tasks.items() if tid in remaining_ids]
                completed_with_current = completed_tasks_list + [task]

                def _append_backend_task(nt) -> None:
                    with state_lock:
                        all_tasks[nt.id] = nt
                        backend_queue.insert(0, nt.id)

                def _append_frontend_task_id(tid: str) -> None:
                    with state_lock:
                        frontend_queue.append(tid)

                workflow_result = agents["frontend"].run_workflow(
                    repo_path=frontend_dir,
                    backend_dir=backend_dir,
                    task=task,
                    spec_content=spec_content,
                    architecture=architecture,
                    qa_agent=agents["qa"],
                    accessibility_agent=agents["accessibility"],
                    security_agent=agents["security"],
                    code_review_agent=agents["code_review"],
                    acceptance_verifier_agent=agents.get("acceptance_verifier"),
                    dbc_agent=agents["dbc_comments"],
                    tech_lead=tech_lead,
                    build_verifier=_run_build_verification,
                    doc_agent=agents.get("documentation"),
                    completed_tasks=completed_with_current,
                    remaining_tasks=remaining_tasks_list,
                    all_tasks=all_tasks,
                    append_backend_task_fn=_append_backend_task,
                    append_frontend_task_fn=_append_frontend_task_id,
                )

                elapsed = time.monotonic() - task_start_time
                with state_lock:
                    if workflow_result.success:
                        completed.add(task_id)
                        completed_code_task_ids.append(task_id)
                        execution_tracker.observe_loop(task_id, 1)
                        execution_tracker.finish_task(task_id)
                        _log_task_completion_banner(
                            task_id=task_id,
                            task_title=getattr(task, "title", "") or task_id,
                            assignee="frontend",
                            elapsed_seconds=elapsed,
                            log_prefix=log_prefix,
                        )
                    else:
                        failed[task_id] = workflow_result.failure_reason or "Frontend workflow failed"
                        execution_tracker.observe_loop(task_id, 1)
                        execution_tracker.finish_task(task_id, blocked=True)
                        logger.warning("%s[%s] Frontend FAILED after %.1fs: %s", log_prefix, task_id, elapsed, failed[task_id])
                logger.info("%s[%s] <<< Frontend worker done (completed=%s)", log_prefix, task_id, workflow_result.success)
            except (LLMError, httpx.HTTPError) as e:
                with state_lock:
                    if isinstance(e, LLMRateLimitError):
                        llm_limit_exceeded[0] = True
                        failed[task_id] = OLLAMA_WEEKLY_LIMIT_MESSAGE
                    elif isinstance(e, LLMTemporaryError):
                        failed[task_id] = "LLM rate limited or temporarily unavailable – please retry later"
                    elif isinstance(e, LLMPermanentError):
                        failed[task_id] = str(e)
                    else:
                        failed[task_id] = f"LLM error: {e}"
                if isinstance(e, LLMRateLimitError):
                    logger.warning("Ollama LLM usage limit exceeded for week. Job %s paused.", job_id)
                else:
                    logger.warning("%s[%s] Frontend task LLM/HTTP error: %s", log_prefix, task_id, e)
                checkout_branch(frontend_dir, DEVELOPMENT_BRANCH)
            except Exception as e:
                _log_agent_crash_banner(task_id, "frontend", e, log_prefix)
                file_path, line_number, func_name = _parse_traceback_for_crash(e)
                agent_crash_details = {
                    "task_id": task_id,
                    "agent_type": "frontend",
                    "exception_type": type(e).__name__,
                    "exception_message": str(e),
                    "traceback": traceback.format_exc(),
                    "file_path": file_path,
                    "line_number": line_number,
                    "function_name": func_name,
                }
                update_job(job_id, status=JOB_STATUS_AGENT_CRASH, error=str(e), agent_crash_details=agent_crash_details)
                repair_applied = False
                if type(e) in REPAIRABLE_EXCEPTIONS and task_id not in repaired_tasks:
                    repair_agent = agents.get("repair")
                    if repair_agent:
                        try:
                            from repair_agent.models import RepairInput
                            result = repair_agent.run(RepairInput(
                                traceback=traceback.format_exc(),
                                exception_type=type(e).__name__,
                                exception_message=str(e),
                                task_id=task_id,
                                agent_type="frontend",
                                agent_source_path=agent_source_path,
                            ))
                            if result.suggested_fixes and _apply_repair_fixes(agent_source_path, result.suggested_fixes):
                                repair_applied = True
                                with state_lock:
                                    repaired_tasks.add(task_id)
                                    frontend_queue.append(task_id)
                                update_job(job_id, status=JOB_STATUS_RUNNING, error=None, agent_crash_details=None)
                                logger.info("%s[%s] Repair applied, re-queued task", log_prefix, task_id)
                        except Exception as repair_err:
                            logger.warning("Repair agent failed for %s: %s", task_id, repair_err)
                if not repair_applied:
                    with state_lock:
                        failed[task_id] = f"Unhandled exception: {e}"
                logger.exception("%s[%s] Frontend task exception", log_prefix, task_id)
                checkout_branch(frontend_dir, DEVELOPMENT_BRANCH)

            # After frontend agent is done with all tasks for this repo, containerize it
            devops_agent = agents.get("devops")
            if devops_agent and frontend_dir.is_dir() and (frontend_dir / ".git").exists():
                existing_pipeline = _read_repo_code(frontend_dir, [".yml", ".yaml"])
                tech_lead.trigger_devops_for_frontend(
                    devops_agent, frontend_dir, architecture, spec_content,
                    existing_pipeline=existing_pipeline if existing_pipeline != "# No code files found" else None,
                    build_verifier=_run_build_verification,
                )

    logger.info("Running with parallel workers: 1 backend task, 1 frontend task at a time")
    t_backend = threading.Thread(target=_backend_worker)
    t_frontend = threading.Thread(target=_frontend_worker)
    t_backend.start()
    t_frontend.start()
    t_backend.join()
    t_frontend.join()


def run_orchestrator(job_id: str, repo_path: str | Path) -> None:
    """
    Main orchestration loop. Runs in background thread.

    Work path (repo_path) is the folder where work is saved; it does not need to be a git repo.
    Backend and frontend each have their own repo at work_path/backend and work_path/frontend,
    initialized by the Git Setup Agent before first use. Backend and frontend tasks run in parallel.
    """
    path = Path(repo_path).resolve()
    backend_dir = path / "backend"
    frontend_dir = path / "frontend"
    try:
        update_job(job_id, status=JOB_STATUS_RUNNING)

        agents = _get_agents()

        # 1. Read spec from work path (no git required at root)
        from spec_parser import load_spec_from_repo, parse_spec_heuristic, parse_spec_with_llm
        spec_content = load_spec_from_repo(path)
        try:
            requirements = parse_spec_with_llm(spec_content, get_llm_for_agent("spec_intake"))
        except LLMRateLimitError:
            logger.warning("Ollama LLM usage limit exceeded for week. Job %s paused.", job_id)
            update_job(job_id, status="paused_llm_limit", error=OLLAMA_WEEKLY_LIMIT_MESSAGE)
            return
        except Exception:
            requirements = parse_spec_heuristic(spec_content)
        update_job(job_id, requirements_title=requirements.title)

        # Create plan folder after spec is ingested successfully (all planning artifacts go here)
        plan_dir = ensure_plan_dir(path)
        logger.info("Plan folder ensured at %s", plan_dir)

        # 1b. Spec Intake and Validation: normalize spec into structured form for faster planning
        # When SW_ENFORCE_STRUCTURED_SPEC=1, fail if spec is too long or intake fails
        MAX_SPEC_CHARS_STRUCTURED = 60000
        enforce_structured_spec = (os.environ.get("SW_ENFORCE_STRUCTURED_SPEC") or "").strip().lower() in ("1", "true", "yes")
        spec_content_for_planning = spec_content  # fallback to full spec if no intake
        spec_intake_open_questions: List[str] = []
        spec_intake_assumptions: List[str] = []
        spec_intake_agent = agents.get("spec_intake")
        if spec_intake_agent:
            if enforce_structured_spec and len(spec_content) > MAX_SPEC_CHARS_STRUCTURED:
                msg = (
                    f"SW_ENFORCE_STRUCTURED_SPEC: spec too long ({len(spec_content)} chars, max {MAX_SPEC_CHARS_STRUCTURED}). "
                    "Shorten the spec or increase MAX_SPEC_CHARS_STRUCTURED."
                )
                logger.error(msg)
                update_job(job_id, status=JOB_STATUS_FAILED, error=msg)
                return
            try:
                from planning_team.spec_intake_agent import (
                    SpecIntakeInput,
                    build_compact_spec_for_planning,
                    validated_spec_to_requirements,
                )
                spec_intake_output = spec_intake_agent.run(SpecIntakeInput(
                    spec_content=spec_content,
                    plan_dir=plan_dir,
                ))
                requirements = validated_spec_to_requirements(spec_intake_output)
                spec_content_for_planning = build_compact_spec_for_planning(spec_intake_output)
                spec_intake_open_questions = spec_intake_output.open_questions or []
                spec_intake_assumptions = spec_intake_output.assumptions or []
                logger.info(
                    "Spec Intake: success, %s REQ-IDs, %s open questions, compact spec %s chars (was %s)",
                    len(spec_intake_output.acceptance_criteria_index),
                    len(spec_intake_open_questions),
                    len(spec_content_for_planning),
                    len(spec_content),
                )
            except LLMRateLimitError:
                if enforce_structured_spec:
                    update_job(job_id, status="paused_llm_limit", error=OLLAMA_WEEKLY_LIMIT_MESSAGE)
                    return
                logger.warning("Spec Intake skipped (LLM rate limit); using parsed requirements")
            except Exception as e:
                if enforce_structured_spec:
                    update_job(job_id, status=JOB_STATUS_FAILED, error=f"Spec Intake required but failed: {e}")
                    return
                logger.warning("Spec Intake failed (using parsed requirements): %s", e)
        update_job(job_id, requirements_title=requirements.title)

        # 2. Project Overview (before architecture) - never skip; use fallback if LLM fails
        project_overview: Optional[Dict[str, Any]] = None
        project_planning_agent = agents.get("project_planning")
        if project_planning_agent:
            try:
                from shared.context_sizing import compute_repo_summary_chars
                from planning_team.project_planning_agent.models import ProjectPlanningInput, build_fallback_overview_from_requirements
                max_repo_chars = compute_repo_summary_chars(project_planning_agent.llm)
                repo_summary = _truncate_for_context(_read_repo_code(path), max_repo_chars)
                pp_input = ProjectPlanningInput(
                    requirements=requirements,
                    spec_content=spec_content_for_planning,
                    repo_state_summary=repo_summary if repo_summary != "# No code files found" else None,
                    plan_dir=plan_dir,
                )
                pp_output = project_planning_agent.run(pp_input)
                project_overview = model_to_dict(pp_output.overview)
                logger.info("Project Planning: success (LLM-based)")
                try:
                    write_project_overview_plan(path, pp_output.overview, plan_dir=plan_dir)
                except Exception as e:
                    logger.warning("Failed to write plan/project_overview.md: %s", e)
                try:
                    features_doc = getattr(pp_output, "features_and_functionality_doc", None) or (project_overview.get("features_and_functionality_doc") or "")
                    if features_doc:
                        write_features_and_functionality_plan(path, features_doc, plan_dir=plan_dir)
                except Exception as e:
                    logger.warning("Failed to write plan/features_and_functionality.md: %s", e)
            except LLMRateLimitError:
                logger.warning("Ollama LLM usage limit exceeded for week. Job %s paused.", job_id)
                update_job(job_id, status="paused_llm_limit", error=OLLAMA_WEEKLY_LIMIT_MESSAGE)
                return
            except Exception as e:
                logger.warning("Project planning failed (attempting fallback overview): %s", e)
                try:
                    fallback = build_fallback_overview_from_requirements(requirements)
                    project_overview = model_to_dict(fallback)
                    logger.info("Project Planning: success via fallback overview (LLM failed: %s)", e)
                    try:
                        write_project_overview_plan(path, fallback, plan_dir=plan_dir)
                    except Exception as we:
                        logger.warning("Failed to write plan/project_overview.md: %s", we)
                    try:
                        if getattr(fallback, "features_and_functionality_doc", ""):
                            write_features_and_functionality_plan(path, fallback.features_and_functionality_doc, plan_dir=plan_dir)
                    except Exception as we2:
                        logger.warning("Failed to write plan/features_and_functionality.md: %s", we2)
                except Exception as fallback_err:
                    logger.error(
                        "Project planning hard failure (no overview available): LLM=%s, fallback=%s",
                        e, fallback_err,
                    )
                    update_job(
                        job_id,
                        status=JOB_STATUS_FAILED,
                        error=f"Project planning failed and fallback unavailable: {fallback_err}",
                    )
                    return
        else:
            # No project planning agent; build fallback so downstream agents always get an overview
            try:
                from planning_team.project_planning_agent.models import build_fallback_overview_from_requirements
                fallback = build_fallback_overview_from_requirements(requirements)
                project_overview = model_to_dict(fallback)
                logger.info("Project Planning: no agent configured, using fallback overview")
                try:
                    if getattr(fallback, "features_and_functionality_doc", ""):
                        write_features_and_functionality_plan(path, fallback.features_and_functionality_doc, plan_dir=plan_dir)
                except Exception as e2:
                    logger.warning("Failed to write plan/features_and_functionality.md: %s", e2)
            except Exception as e:
                logger.error("Project planning fallback failed (no agent): %s", e)
                update_job(job_id, status=JOB_STATUS_FAILED, error=f"Project planning fallback failed: {e}")
                return

        if project_overview is None:
            logger.error("Project planning produced no overview; failing job")
            update_job(job_id, status=JOB_STATUS_FAILED, error="Project planning produced no overview")
            return

        # Planning process: (1) features doc done above; (2) tasks from spec + features; (3) architecture from spec + features;
        # (4) loop until tasks and architecture align; (5) conformance to spec; if non-compliant, re-run from (2) with feedback.
        features_and_functionality_doc = (project_overview.get("features_and_functionality_doc") or "").strip()
        from architecture_agent.models import ArchitectureInput
        from tech_lead_agent.models import TechLeadInput
        from planning_team.planning_review import check_tasks_architecture_alignment, check_spec_conformance

        from shared.context_sizing import compute_existing_code_chars

        arch_agent = agents["architecture"]
        tech_lead = agents["tech_lead"]
        max_code_chars = compute_existing_code_chars(tech_lead.llm)
        existing_code = _truncate_for_context(_read_repo_code(path), max_code_chars)

        minimal_planning = (os.environ.get("SW_MINIMAL_PLANNING") or "").strip().lower() in ("1", "true", "yes")
        fast_start_planning = (os.environ.get("SW_FAST_START_PLANNING") or "").strip().lower() in ("1", "true", "yes")
        if fast_start_planning:
            minimal_planning = True
            logger.info("Fast-start planning mode: minimal planning, 1 alignment/conformance iteration")
        tech_lead_minimal_planning = minimal_planning

        MAX_ALIGNMENT_ITERATIONS = int(os.environ.get("SW_MAX_ALIGNMENT_ITERATIONS") or ("1" if fast_start_planning else "5"))
        MAX_CONFORMANCE_RETRIES = int(os.environ.get("SW_MAX_CONFORMANCE_RETRIES") or ("1" if fast_start_planning else "3"))

        assignment = None
        architecture = None
        conformance_retries = 0
        conformance_issues_from_last: List[str] = []
        tech_lead_output = None
        enable_planning_cache = (os.environ.get("SW_ENABLE_PLANNING_CACHE", "1") or "1").strip().lower() in ("1", "true", "yes")

        while True:
            # Step 2: Detailed tasks from spec + features doc (and architecture if we have it from a previous iteration)
            alignment_feedback = []
            conformance_issues = conformance_issues_from_last if conformance_retries > 0 else []
            if conformance_retries > 0 and conformance_issues:
                logger.info("Re-running task generation with %d spec conformance issues", len(conformance_issues))

            try:
                tech_lead_output = tech_lead.run(TechLeadInput(
                    requirements=requirements,
                    architecture=architecture,
                    repo_path=str(path),
                    spec_content=spec_content_for_planning,
                    existing_codebase=existing_code if existing_code != "# No code files found" else None,
                    project_overview=project_overview,
                    alignment_feedback=alignment_feedback if alignment_feedback else None,
                    conformance_issues=conformance_issues if conformance_issues else None,
                    minimal_planning=tech_lead_minimal_planning,
                    open_questions=spec_intake_open_questions if spec_intake_open_questions else None,
                    assumptions=spec_intake_assumptions if spec_intake_assumptions else None,
                ))
            except LLMRateLimitError:
                logger.warning("Ollama LLM usage limit exceeded for week. Job %s paused.", job_id)
                update_job(job_id, status="paused_llm_limit", error=OLLAMA_WEEKLY_LIMIT_MESSAGE)
                return
            if tech_lead_output.spec_clarification_needed:
                questions = tech_lead_output.clarification_questions or []
                error_msg = f"Spec is unclear. Tech Lead requests clarification: {'; '.join(questions[:5])}"
                if len(questions) > 5:
                    error_msg += f" (+{len(questions) - 5} more)"
                logger.warning(error_msg)
                update_job(job_id, status=JOB_STATUS_FAILED, error=error_msg)
                return
            assignment = tech_lead_output.assignment
            if not assignment or not assignment.tasks:
                logger.error("Tech Lead produced no tasks; failing job")
                update_job(job_id, status=JOB_STATUS_FAILED, error="Tech Lead produced no tasks")
                return

            # Step 3: Architecture from features doc + spec (and optional planning feedback)
            planning_feedback_for_arch = (alignment_feedback + conformance_issues) if (alignment_feedback or conformance_issues) else None
            arch_input = ArchitectureInput(
                requirements=requirements,
                technology_preferences=["Python", "FastAPI", "Angular", "PostgreSQL", "Docker"],
                project_overview=project_overview,
                features_and_functionality_doc=features_and_functionality_doc or None,
                planning_feedback=planning_feedback_for_arch,
            )
            try:
                arch_output = arch_agent.run(arch_input)
            except LLMRateLimitError:
                logger.warning("Ollama LLM usage limit exceeded for week. Job %s paused.", job_id)
                update_job(job_id, status="paused_llm_limit", error=OLLAMA_WEEKLY_LIMIT_MESSAGE)
                return
            architecture = arch_output.architecture
            update_job(job_id, architecture_overview=architecture.overview)
            try:
                write_architecture_plan(path, architecture, plan_dir=plan_dir)
            except Exception as e:
                logger.warning("Failed to write plan/architecture.md: %s", e)

            # Planning cache: reuse assignment when spec+arch+project unchanged (first iteration only)
            if enable_planning_cache and conformance_retries == 0 and not alignment_feedback:
                from shared.planning_cache import (
                    compute_planning_cache_key,
                    get_cached_plan,
                    set_cached_plan,
                )
                from shared.models import TaskAssignment
                cache_key = compute_planning_cache_key(
                    spec_content_for_planning,
                    architecture.overview if architecture else "",
                    project_overview,
                )
                cached = get_cached_plan(plan_dir, cache_key)
                if cached and cached.get("assignment"):
                    try:
                        assignment = TaskAssignment(**cached["assignment"])
                        tech_lead_output = type("TechLeadOutput", (), {
                            "assignment": assignment,
                            "requirement_task_mapping": cached.get("requirement_task_mapping") or [],
                            "summary": cached.get("summary") or "cached",
                        })()
                        logger.info("Using cached planning result (skipping alignment/conformance)")
                        break
                    except Exception as cache_err:
                        logger.warning("Planning cache load failed, continuing: %s", cache_err)

            # Step 4: Loop until tasks and architecture align
            alignment_iterations = 0
            alignment_early_exit = int(os.environ.get("SW_ALIGNMENT_EARLY_EXIT_THRESHOLD") or "2")
            while alignment_iterations < MAX_ALIGNMENT_ITERATIONS:
                aligned, alignment_feedback = check_tasks_architecture_alignment(assignment, architecture, tech_lead.llm)
                if aligned:
                    logger.info("Tasks and architecture aligned (iteration %s)", alignment_iterations + 1)
                    break
                # Early exit: few minor issues and we've done at least one iteration
                if alignment_iterations >= 1 and len(alignment_feedback) <= alignment_early_exit:
                    has_critical = any(
                        kw in " ".join(alignment_feedback).lower()
                        for kw in ("missing", "no task", "no corresponding", "critical", "required")
                    )
                    if not has_critical:
                        logger.info(
                            "Alignment: early exit (iteration %s, %s minor issues below threshold)",
                            alignment_iterations + 1,
                            len(alignment_feedback),
                        )
                        break
                logger.info("Tasks and architecture not aligned (iteration %s/%s): %s", alignment_iterations + 1, MAX_ALIGNMENT_ITERATIONS, alignment_feedback[:3])
                alignment_iterations += 1
                if alignment_iterations >= MAX_ALIGNMENT_ITERATIONS:
                    logger.warning("Max alignment iterations reached; proceeding with current plan")
                    break
                # Re-run Tech Lead with architecture and alignment feedback
                try:
                    tech_lead_output = tech_lead.run(TechLeadInput(
                        requirements=requirements,
                        architecture=architecture,
                        repo_path=str(path),
                        spec_content=spec_content_for_planning,
                        existing_codebase=existing_code if existing_code != "# No code files found" else None,
                        project_overview=project_overview,
                        alignment_feedback=alignment_feedback,
                        minimal_planning=tech_lead_minimal_planning,
                        open_questions=spec_intake_open_questions if spec_intake_open_questions else None,
                        assumptions=spec_intake_assumptions if spec_intake_assumptions else None,
                    ))
                except LLMRateLimitError:
                    update_job(job_id, status="paused_llm_limit", error=OLLAMA_WEEKLY_LIMIT_MESSAGE)
                    return
                if tech_lead_output.spec_clarification_needed:
                    update_job(job_id, status=JOB_STATUS_FAILED, error="Spec clarification needed during alignment")
                    return
                assignment = tech_lead_output.assignment
                if not assignment or not assignment.tasks:
                    break
                # Re-run Architecture with alignment feedback
                arch_input = ArchitectureInput(
                    requirements=requirements,
                    technology_preferences=["Python", "FastAPI", "Angular", "PostgreSQL", "Docker"],
                    project_overview=project_overview,
                    features_and_functionality_doc=features_and_functionality_doc or None,
                    planning_feedback=alignment_feedback,
                )
                try:
                    arch_output = arch_agent.run(arch_input)
                except LLMRateLimitError:
                    update_job(job_id, status="paused_llm_limit", error=OLLAMA_WEEKLY_LIMIT_MESSAGE)
                    return
                architecture = arch_output.architecture
                try:
                    write_architecture_plan(path, architecture, plan_dir=plan_dir)
                except Exception:
                    pass

            # Step 5: Conformance review (tasks + architecture vs initial_spec)
            conformant, conformance_issues_from_last = check_spec_conformance(spec_content_for_planning, assignment, architecture, llm)
            if conformant:
                logger.info("Tasks and architecture conform to initial spec; proceeding to execution")
                if enable_planning_cache and architecture:
                    try:
                        from shared.planning_cache import compute_planning_cache_key, set_cached_plan
                        cache_key = compute_planning_cache_key(
                            spec_content_for_planning,
                            architecture.overview,
                            project_overview,
                        )
                        set_cached_plan(
                            plan_dir,
                            cache_key,
                            assignment,
                            getattr(tech_lead_output, "requirement_task_mapping", None) or [],
                            getattr(tech_lead_output, "summary", "") or "",
                        )
                    except Exception as cache_err:
                        logger.warning("Planning cache store failed: %s", cache_err)
                break
            conformance_early_exit = int(os.environ.get("SW_CONFORMANCE_EARLY_EXIT_THRESHOLD") or "2")
            if len(conformance_issues_from_last) <= conformance_early_exit:
                has_critical_conformance = any(
                    kw in " ".join(conformance_issues_from_last).lower()
                    for kw in ("missing", "violates", "wrong scope", "critical", "required")
                )
                if not has_critical_conformance:
                    logger.info(
                        "Conformance: early exit (%s minor issues below threshold)",
                        len(conformance_issues_from_last),
                    )
                    break
            logger.info("Spec conformance failed (%d issues); re-running planning with feedback", len(conformance_issues_from_last))
            conformance_retries += 1
            if conformance_retries > MAX_CONFORMANCE_RETRIES:
                logger.warning("Max conformance retries reached; proceeding with current plan")
                break

        # Run additional planning agents in dependency tiers (parallel within each tier)
        arch_overview = architecture.overview if architecture else ""
        tenancy = getattr(architecture, "tenancy_model", "") or "" if architecture else ""
        req_ids = (requirements.metadata or {}).get("requirement_ids", []) if requirements else []
        infra_doc = ""
        data_lifecycle = ""
        ui_ux_doc = ""
        devops_doc = ""

        # Optional: skip domain planning agents (env SW_SKIP_PLANNING_AGENTS=agent1,agent2 or SW_MINIMAL_PLANNING=1)
        skip_planning_agents: set = set()
        skip_env = (os.environ.get("SW_SKIP_PLANNING_AGENTS") or "").strip()
        if skip_env:
            skip_planning_agents = {k.strip() for k in skip_env.split(",") if k.strip()}
        if minimal_planning:
            skip_planning_agents = {
                "api_contract", "data_architecture", "ui_ux", "infrastructure",
                "frontend_architecture", "devops_planning", "qa_test_strategy",
                "security_planning", "observability", "performance_doc",
            }

        if not minimal_planning:
            # Tier 1: api_contract, data_architecture, ui_ux, infrastructure (parallel)
            tier1_keys = [k for k in ("api_contract", "data_architecture", "ui_ux", "infrastructure") if k not in skip_planning_agents]
            if tier1_keys:
                with ThreadPoolExecutor(max_workers=4) as executor:
                    futures = {
                        executor.submit(
                            _run_tier1_agent,
                            key,
                            agents,
                            spec_content,
                            arch_overview,
                            plan_dir,
                            requirements,
                            features_and_functionality_doc or "",
                            tenancy,
                        ): key
                        for key in tier1_keys
                    }
                    for future in as_completed(futures):
                        agent_key, result = future.result()
                        if result:
                            if "infra_doc" in result:
                                infra_doc = result["infra_doc"]
                            if "data_lifecycle" in result:
                                data_lifecycle = result["data_lifecycle"]
                            if "ui_ux_doc" in result:
                                ui_ux_doc = result["ui_ux_doc"]

            # Tier 2: frontend_architecture, devops_planning, qa_test_strategy, security_planning (parallel)
            tier2_keys = [k for k in ("frontend_architecture", "devops_planning", "qa_test_strategy", "security_planning") if k not in skip_planning_agents]
            if tier2_keys:
                with ThreadPoolExecutor(max_workers=4) as executor:
                    futures = {
                        executor.submit(
                            _run_tier2_agent,
                            key,
                            agents,
                            spec_content_for_planning,
                            arch_overview,
                            plan_dir,
                            requirements,
                            req_ids,
                            ui_ux_doc,
                            infra_doc,
                            data_lifecycle,
                        ): key
                        for key in tier2_keys
                    }
                    for future in as_completed(futures):
                        agent_key, result = future.result()
                        if result and "devops_doc" in result:
                            devops_doc = result["devops_doc"]

            # Tier 3: observability, performance_doc (parallel)
            tier3_keys = [k for k in ("observability", "performance_doc") if k not in skip_planning_agents]
            if tier3_keys:
                with ThreadPoolExecutor(max_workers=2) as executor:
                    futures = []
                    if "observability" in tier3_keys and agents.get("observability"):
                        futures.append(executor.submit(
                            lambda: _run_tier3_observability(agents, arch_overview, infra_doc, devops_doc, requirements, plan_dir)
                        ))
                    if "performance_doc" in tier3_keys and agents.get("performance_doc"):
                        futures.append(executor.submit(
                            lambda: _run_tier3_performance_doc(agents, spec_content_for_planning, arch_overview, requirements, plan_dir)
                        ))
                    for f in as_completed(futures):
                        try:
                            f.result()
                        except Exception as e:
                            logger.debug("Tier 3 planning skipped: %s", e)

        # Planning consolidation: master plan, risk register, ship checklist
        try:
            from planning_team.planning_consolidation import run_planning_consolidation
            run_planning_consolidation(plan_dir, assignment, architecture, project_overview)
        except Exception as e:
            logger.warning("Planning consolidation skipped: %s", e)

        # Write Tech Lead development plan
        try:
            write_tech_lead_plan(
                path,
                assignment,
                summary=getattr(tech_lead_output, "summary", "") or "",
                requirement_task_mapping=getattr(tech_lead_output, "requirement_task_mapping", None) or [],
                validation_report=getattr(tech_lead_output, "validation_report", None),
                plan_dir=plan_dir,
            )
        except Exception as e:
            logger.warning("Failed to write plan/tech_lead.md: %s", e)

        # Store execution order in job state for API polling
        update_job(job_id, execution_order=assignment.execution_order)
        for t in assignment.tasks:
            execution_tracker.upsert_task(
                task_id=t.id,
                title=getattr(t, "title", "") or t.id,
                assigned_agent=getattr(t, "assignee", "unknown"),
                dependencies=getattr(t, "dependencies", []) or [],
            )

        # 6. Execute tasks: partition into prefix (devops/git_setup), backend, frontend
        completed = set()
        failed: Dict[str, str] = {}
        completed_code_task_ids: List[str] = []
        all_tasks = {t.id: t for t in assignment.tasks}
        full_order = list(assignment.execution_order)
        prefix_queue = [tid for tid in full_order if all_tasks.get(tid) and (
            all_tasks[tid].type.value == "git_setup" or all_tasks[tid].assignee == "devops"
        )]
        backend_queue: List[str] = [tid for tid in full_order if all_tasks.get(tid) and all_tasks[tid].assignee == "backend"]
        frontend_queue: List[str] = [tid for tid in full_order if all_tasks.get(tid) and all_tasks[tid].assignee == "frontend"]
        total_tasks = len(prefix_queue) + len(backend_queue) + len(frontend_queue)

        logger.info(
            "=== Starting task execution: prefix=%s, backend=%s, frontend=%s ===",
            len(prefix_queue), len(backend_queue), len(frontend_queue),
        )

        # Run prefix tasks sequentially (work path; devops writes to path/devops, no git)
        for task_id in prefix_queue:
            task = all_tasks.get(task_id)
            if not task:
                continue
            update_job(job_id, current_task=task_id)
            execution_tracker.start_task(task_id)
            if task.type.value == "git_setup":
                completed.add(task_id)
                execution_tracker.finish_task(task_id)
                _log_task_completion_banner(
                    task_id=task_id,
                    task_title=getattr(task, "title", "") or task_id,
                    assignee="git_setup",
                    elapsed_seconds=0.0,
                )
                continue
            if task.assignee == "devops":
                # Defer containerization to after backend and frontend complete; skip early devops run.
                completed.add(task_id)
                execution_tracker.finish_task(task_id)
                _log_task_completion_banner(
                    task_id=task_id,
                    task_title=getattr(task, "title", "") or task_id,
                    assignee="devops",
                    elapsed_seconds=0.0,
                )
                continue

        # Backend and frontend workers run in parallel (one task per agent type at a time)
        _run_backend_frontend_workers(
            job_id=job_id,
            path=path,
            backend_dir=backend_dir,
            frontend_dir=frontend_dir,
            backend_queue=backend_queue,
            frontend_queue=frontend_queue,
            all_tasks=all_tasks,
            completed=completed,
            failed=failed,
            completed_code_task_ids=completed_code_task_ids,
            spec_content=spec_content,
            architecture=architecture,
            agents=agents,
            tech_lead=tech_lead,
            total_tasks=total_tasks,
            is_retry=False,
        )

        llm_limit_exceeded = any(v == OLLAMA_WEEKLY_LIMIT_MESSAGE for v in failed.values())
        remaining_in_queues = len(backend_queue) + len(frontend_queue)
        # Log final execution summary with task breakdown
        logger.info(
            "=== Task execution finished: %s completed, %s failed, %s remaining (of %s total) ===",
            len(completed), len(failed), remaining_in_queues, total_tasks,
        )
        _log_task_breakdown(
            completed=completed,
            all_tasks=all_tasks,
            total_tasks=total_tasks,
            failed_count=len(failed),
            job_id=job_id,
        )
        if failed:
            logger.warning("=== Failed task report ===")
            for tid, reason in sorted(failed.items()):
                task_obj = all_tasks.get(tid)
                title = task_obj.title if task_obj else tid
                logger.warning("  [%s] %s — Reason: %s", tid, title, reason)
        if remaining_in_queues:
            logger.warning(
                "Unprocessed tasks still in queues: backend=%s, frontend=%s",
                len(backend_queue), len(frontend_queue),
            )

        # Integration phase: validate backend-frontend API contract alignment
        integration_agent = agents.get("integration")
        has_backend = backend_dir.is_dir() and any(backend_dir.rglob("*.py"))
        has_frontend = frontend_dir.is_dir() and any(frontend_dir.rglob("*.ts"))
        if integration_agent and has_backend and has_frontend and completed_code_task_ids:
            try:
                from integration_agent.models import IntegrationInput
                code_backend = _read_repo_code(backend_dir, [".py"])
                code_frontend = _read_repo_code(frontend_dir, [".ts", ".tsx", ".html", ".scss"])
                if code_backend != "# No code files found" and code_frontend != "# No code files found":
                    int_result = integration_agent.run(IntegrationInput(
                        backend_code=code_backend,
                        frontend_code=code_frontend,
                        spec_content=spec_content,
                        architecture=architecture,
                    ))
                    if not int_result.passed:
                        logger.warning(
                            "Integration agent found %s issues (%s critical/high)",
                            len(int_result.issues),
                            len([i for i in int_result.issues if i.severity in ("critical", "high")]),
                        )
                        for i, issue in enumerate(int_result.issues[:10], 1):
                            logger.warning(
                                "  [%s] %s: %s (backend: %s, frontend: %s)",
                                i, issue.severity, issue.description[:100],
                                issue.backend_location or "n/a", issue.frontend_location or "n/a",
                            )
                    else:
                        logger.info("Integration agent: passed (no critical/high contract mismatches)")
            except Exception as int_err:
                logger.warning("Integration phase failed (non-blocking): %s", int_err)

        # DevOps: containerize every git repo created by the pipeline (backend and frontend)
        devops_agent = agents.get("devops")
        if devops_agent and backend_dir.is_dir() and (backend_dir / ".git").exists():
            existing_pipeline = _read_repo_code(backend_dir, [".yml", ".yaml"])
            tech_lead.trigger_devops_for_backend(
                devops_agent, backend_dir, architecture, spec_content,
                existing_pipeline=existing_pipeline if existing_pipeline != "# No code files found" else None,
                build_verifier=_run_build_verification,
            )
        if devops_agent and frontend_dir.is_dir() and (frontend_dir / ".git").exists():
            existing_pipeline = _read_repo_code(frontend_dir, [".yml", ".yaml"])
            tech_lead.trigger_devops_for_frontend(
                devops_agent, frontend_dir, architecture, spec_content,
                existing_pipeline=existing_pipeline if existing_pipeline != "# No code files found" else None,
                build_verifier=_run_build_verification,
            )

        # Persist failed task details and retryable state in job store
        failed_details = [
            {"task_id": tid, "reason": reason, "title": (all_tasks.get(tid).title if all_tasks.get(tid) else tid)}
            for tid, reason in failed.items()
        ]
        # Store serializable task data for retry capability
        all_tasks_serialized = {
            tid: t.model_dump() if hasattr(t, "model_dump") else t.dict()
            for tid, t in all_tasks.items()
        }
        update_job(
            job_id,
            failed_tasks=failed_details,
            _all_tasks=all_tasks_serialized,
            _spec_content=spec_content,
            _architecture_overview=architecture.overview if architecture else None,
        )

        # Security: run on backend repo and frontend repo separately
        if completed_code_task_ids and tech_lead.should_run_security(
            completed_code_task_ids, spec_content, tech_lead_output.requirement_task_mapping
        ):
            from security_agent.models import SecurityInput
            has_backend = any(all_tasks.get(tid) and all_tasks[tid].assignee == "backend" for tid in completed_code_task_ids)
            has_frontend = any(all_tasks.get(tid) and all_tasks[tid].assignee == "frontend" for tid in completed_code_task_ids)
            if has_backend:
                logger.info("Tech Lead requested security review - running Security agent on backend repo")
                code_backend = _read_repo_code(backend_dir)
                if code_backend and code_backend != "# No code files found":
                    sec_result = agents["security"].run(SecurityInput(
                        code=code_backend,
                        language="python",
                        task_description="Full codebase security review",
                        architecture=architecture,
                    ))
                    if sec_result.vulnerabilities:
                        logger.warning("Security (backend) found %s vulnerabilities", len(sec_result.vulnerabilities))
            if has_frontend:
                logger.info("Tech Lead requested security review - running Security agent on frontend repo")
                code_frontend = _read_repo_code(frontend_dir, [".ts", ".tsx", ".html", ".scss"])
                if code_frontend and code_frontend != "# No code files found":
                    sec_result = agents["security"].run(SecurityInput(
                        code=code_frontend,
                        language="typescript",
                        task_description="Full codebase security review",
                        architecture=architecture,
                    ))
                    if sec_result.vulnerabilities:
                        logger.warning("Security (frontend) found %s vulnerabilities", len(sec_result.vulnerabilities))

        # Final documentation pass: ensure README exists in backend and frontend repos if missing/empty
        doc_agent = agents.get("documentation")
        if doc_agent and completed_code_task_ids:
            for repo_name, repo_dir in [("backend", backend_dir), ("frontend", frontend_dir)]:
                if not repo_dir.is_dir() or not (repo_dir / ".git").exists():
                    continue
                readme_path = repo_dir / "README.md"
                if readme_path.exists() and readme_path.read_text(encoding="utf-8", errors="replace").strip():
                    continue
                logger.info(
                    "Final documentation pass: %s repo README missing or empty, running Documentation Agent",
                    repo_name,
                )
                try:
                    from shared.context_sizing import compute_existing_code_chars
                    max_code_chars = compute_existing_code_chars(doc_agent.llm)
                    codebase_content = _truncate_for_context(
                        _read_repo_code(
                            repo_dir,
                            [".py"] if repo_name == "backend" else [".ts", ".tsx", ".html", ".scss"],
                        ),
                        max_code_chars,
                    )
                    doc_agent.run_full_workflow(
                        repo_path=repo_dir,
                        task_id=f"final-docs-{repo_name}",
                        task_summary="Update all project documentation; ensure README and key sections exist.",
                        agent_type=repo_name,
                        spec_content=spec_content,
                        architecture=architecture,
                        codebase_content=codebase_content,
                    )
                except Exception as doc_err:
                    logger.warning(
                        "Final documentation pass failed for %s repo (non-blocking): %s",
                        repo_name,
                        doc_err,
                    )

        if llm_limit_exceeded:
            update_job(
                job_id,
                status="paused_llm_limit",
                error=OLLAMA_WEEKLY_LIMIT_MESSAGE,
                progress=100,
                current_task=None,
            )
        else:
            logger.info("")
            logger.info("=" * BANNER_WIDTH)
            logger.info("  ★★★  SOFTWARE ENGINEERING TEAM: DELIVERY COMPLETE  ★★★")
            logger.info("  Job %s finished. All tasks executed. Artifacts in work path.", job_id)
            logger.info("=" * BANNER_WIDTH)
            _log_task_breakdown(
                completed=completed,
                all_tasks=all_tasks,
                total_tasks=total_tasks,
                failed_count=len(failed),
                job_id=job_id,
            )
            update_job(job_id, status=JOB_STATUS_COMPLETED, progress=100, current_task=None)

    except Exception as e:
        logger.exception("Orchestrator failed")
        update_job(job_id, status=JOB_STATUS_FAILED, error=str(e))


def run_failed_tasks(job_id: str) -> None:
    """
    Re-run only the failed tasks from a previous job.

    Reads the persisted failed task list and task objects from the job store,
    re-queues them, and executes them through the same pipeline.
    Runs in a background thread (same pattern as run_orchestrator).
    """
    from shared.job_store import get_job
    from shared.models import Task

    job_data = get_job(job_id)
    if not job_data:
        raise ValueError(f"Job {job_id} not found")
    repo_path = job_data.get("repo_path")
    if not repo_path:
        raise ValueError(f"Job {job_id} has no repo_path")
    failed_tasks = job_data.get("failed_tasks") or []
    if not failed_tasks:
        raise ValueError(f"Job {job_id} has no failed tasks to retry")
    all_tasks_data = job_data.get("_all_tasks") or {}
    if not all_tasks_data:
        raise ValueError(f"Job {job_id} has no stored task data for retry")

    failed_ids = [ft["task_id"] for ft in failed_tasks]
    logger.info("=== Retrying %s failed tasks for job %s: %s ===", len(failed_ids), job_id, failed_ids)

    path = Path(repo_path).resolve()
    backend_dir = path / "backend"
    frontend_dir = path / "frontend"
    try:
        update_job(job_id, status=JOB_STATUS_RUNNING, failed_tasks=[], error=None)

        agents = _get_agents()

        # Reconstruct task objects from stored data
        all_tasks: Dict[str, Task] = {}
        for tid, tdata in all_tasks_data.items():
            try:
                all_tasks[tid] = Task(**tdata)
            except Exception:
                logger.warning("Could not reconstruct task %s from stored data - skipping", tid)

        # Re-read spec for agents that need it
        from spec_parser import load_spec_from_repo
        spec_content = load_spec_from_repo(path)

        # Reconstruct minimal architecture from stored overview
        from shared.models import SystemArchitecture
        arch_overview = job_data.get("_architecture_overview") or job_data.get("architecture_overview") or ""
        architecture = SystemArchitecture(overview=arch_overview)

        tech_lead = agents["tech_lead"]

        # Partition failed tasks into backend/frontend queues; handle devops/git_setup in prefix
        completed = set()
        failed_retry: Dict[str, str] = {}
        completed_code_task_ids: List[str] = []

        retry_prefix = [
            tid for tid in failed_ids
            if all_tasks.get(tid) and (
                all_tasks[tid].type.value == "git_setup" or all_tasks[tid].assignee == "devops"
            )
        ]
        retry_backend_queue = [
            tid for tid in failed_ids
            if all_tasks.get(tid) and all_tasks[tid].assignee == "backend"
        ]
        retry_frontend_queue = [
            tid for tid in failed_ids
            if all_tasks.get(tid) and all_tasks[tid].assignee == "frontend"
        ]
        total_tasks = len(retry_prefix) + len(retry_backend_queue) + len(retry_frontend_queue)

        # Run prefix (devops, git_setup) sequentially
        for task_id in retry_prefix:
            task = all_tasks.get(task_id)
            if not task:
                continue
            update_job(job_id, current_task=task_id)
            if task.type.value == "git_setup":
                completed.add(task_id)
                _log_task_completion_banner(
                    task_id=task_id,
                    task_title=getattr(task, "title", "") or task_id,
                    assignee="git_setup",
                    elapsed_seconds=0.0,
                )
                continue
            if task.assignee == "devops":
                try:
                    devops_start = time.monotonic()
                    existing_pipeline = _read_repo_code(path, [".yml", ".yaml"])
                    workflow_result = agents["devops"].run_workflow(
                        repo_path=path,
                        task_description=task.description,
                        requirements=_task_requirements(task),
                        architecture=architecture,
                        existing_pipeline=existing_pipeline if existing_pipeline != "# No code files found" else None,
                        tech_stack=["Python", "FastAPI", "Angular", "PostgreSQL", "Docker"],
                        build_verifier=_run_build_verification,
                        task_id=task_id,
                        subdir="devops",
                    )
                    devops_elapsed = time.monotonic() - devops_start
                    if workflow_result.success:
                        completed.add(task_id)
                        _log_task_completion_banner(
                            task_id=task_id,
                            task_title=getattr(task, "title", "") or task_id,
                            assignee="devops",
                            elapsed_seconds=devops_elapsed,
                        )
                    else:
                        failed_retry[task_id] = workflow_result.failure_reason or "DevOps workflow failed"
                except Exception as e:
                    failed_retry[task_id] = f"DevOps failed: {e}"

        # Run backend and frontend in parallel (1 backend task, 1 frontend task at a time)
        if retry_backend_queue or retry_frontend_queue:
            logger.info(
                "=== Retry: running with parallel workers (backend=%s, frontend=%s) ===",
                len(retry_backend_queue), len(retry_frontend_queue),
            )
            _run_backend_frontend_workers(
                job_id=job_id,
                path=path,
                backend_dir=backend_dir,
                frontend_dir=frontend_dir,
                backend_queue=retry_backend_queue,
                frontend_queue=retry_frontend_queue,
                all_tasks=all_tasks,
                completed=completed,
                failed=failed_retry,
                completed_code_task_ids=completed_code_task_ids,
                spec_content=spec_content,
                architecture=architecture,
                agents=agents,
                tech_lead=tech_lead,
                total_tasks=total_tasks,
                is_retry=True,
            )

        llm_limit_exceeded = any(v == OLLAMA_WEEKLY_LIMIT_MESSAGE for v in failed_retry.values())

        # Final summary with task breakdown
        logger.info(
            "=== Retry finished: %s completed, %s still failed (of %s retried) ===",
            len(completed), len(failed_retry), total_tasks,
        )
        _log_task_breakdown(
            completed=completed,
            all_tasks=all_tasks,
            total_tasks=total_tasks,
            failed_count=len(failed_retry),
            job_id=job_id,
        )
        if failed_retry:
            logger.warning("=== Still-failed task report ===")
            for tid, reason in sorted(failed_retry.items()):
                task_obj = all_tasks.get(tid)
                title = task_obj.title if task_obj else tid
                logger.warning("  [%s] %s — Reason: %s", tid, title, reason)

        # DevOps: containerize every git repo that exists (same as main run)
        devops_agent = agents.get("devops")
        if devops_agent and backend_dir.is_dir() and (backend_dir / ".git").exists():
            existing_pipeline = _read_repo_code(backend_dir, [".yml", ".yaml"])
            tech_lead.trigger_devops_for_backend(
                devops_agent, backend_dir, architecture, spec_content,
                existing_pipeline=existing_pipeline if existing_pipeline != "# No code files found" else None,
                build_verifier=_run_build_verification,
            )
        if devops_agent and frontend_dir.is_dir() and (frontend_dir / ".git").exists():
            existing_pipeline = _read_repo_code(frontend_dir, [".yml", ".yaml"])
            tech_lead.trigger_devops_for_frontend(
                devops_agent, frontend_dir, architecture, spec_content,
                existing_pipeline=existing_pipeline if existing_pipeline != "# No code files found" else None,
                build_verifier=_run_build_verification,
            )

        failed_details = [
            {"task_id": tid, "reason": reason, "title": (all_tasks.get(tid).title if all_tasks.get(tid) else tid)}
            for tid, reason in failed_retry.items()
        ]
        if llm_limit_exceeded:
            update_job(
                job_id,
                failed_tasks=failed_details,
                status="paused_llm_limit",
                error=OLLAMA_WEEKLY_LIMIT_MESSAGE,
                current_task=None,
            )
        else:
            logger.info("")
            logger.info("=" * BANNER_WIDTH)
            logger.info("  ★★★  SOFTWARE ENGINEERING TEAM: DELIVERY COMPLETE (retry)  ★★★")
            logger.info("  Job %s finished. All retried tasks executed.", job_id)
            logger.info("=" * BANNER_WIDTH)
            _log_task_breakdown(
                completed=completed,
                all_tasks=all_tasks,
                total_tasks=total_tasks,
                failed_count=len(failed_retry),
                job_id=job_id,
            )
            update_job(job_id, failed_tasks=failed_details, status=JOB_STATUS_COMPLETED, current_task=None)

    except Exception as e:
        logger.exception("Retry orchestrator failed")
        update_job(job_id, status=JOB_STATUS_FAILED, error=str(e))
