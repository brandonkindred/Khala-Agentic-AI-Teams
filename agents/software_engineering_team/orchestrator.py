"""
Tech Lead orchestrator: runs the full pipeline with feature branches.

Planning flow:
1. Review initial_spec and document features and functionalities (high level) via Project Planning.
2. Tech Lead produces Initiative/Epic/Story hierarchy from spec + features.
3. Architecture Expert produces architecture from spec + features.

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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx

# Path setup when run as module
import sys
_team_dir = Path(__file__).resolve().parent
if str(_team_dir) not in sys.path:
    sys.path.insert(0, str(_team_dir))
_arch_dir = _team_dir / "architect-agents"
if _arch_dir.exists() and str(_arch_dir) not in sys.path:
    sys.path.insert(0, str(_arch_dir))

from software_engineering_team.shared.git_utils import (
    DEVELOPMENT_BRANCH,
    checkout_branch,
    create_feature_branch,
    delete_branch,
    ensure_development_branch,
    merge_branch,
)
from software_engineering_team.shared.llm import (
    LLMError,
    LLMJsonParseError,
    LLMPermanentError,
    LLMRateLimitError,
    LLMTemporaryError,
    LLMTruncatedError,
    OLLAMA_WEEKLY_LIMIT_MESSAGE,
    get_llm_for_agent,
)
from software_engineering_team.shared.job_store import (
    JOB_STATUS_AGENT_CRASH,
    JOB_STATUS_CANCELLED,
    JOB_STATUS_COMPLETED,
    JOB_STATUS_FAILED,
    JOB_STATUS_RUNNING,
    JOB_STATUS_PAUSED_LLM_CONNECTIVITY,
    LLM_UNREACHABLE_AFTER_RETRIES,
    update_job,
    update_task_state,
    update_job_team_progress,
    add_pending_questions,
    is_waiting_for_answers,
    is_cancel_requested,
    get_job,
)
from software_engineering_team.shared.command_runner import run_command_with_nvm
from software_engineering_team.shared.execution_tracker import execution_tracker
# Plan dir: kept in planning_team (also used by planning_consolidation; shared/plan_dir would require moving consolidation)
from planning_team.plan_dir import ensure_plan_dir
from software_engineering_team.shared.development_plan_writer import (
    write_architecture_plan,
    write_features_and_functionality_plan,
    write_project_overview_plan,
    write_tech_lead_plan,
)
from software_engineering_team.shared.models import TaskUpdate, model_to_dict
from software_engineering_team.shared.repo_writer import write_agent_output
from software_engineering_team.shared.repo_utils import read_repo_code, truncate_for_context
from software_engineering_team.shared.task_utils import task_requirements

logger = logging.getLogger(__name__)


def _iso_now() -> str:
    """Return current UTC time as ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


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

# Default options for clarification questions
DEFAULT_CLARIFICATION_OPTIONS = [
    {"id": "yes", "label": "Yes"},
    {"id": "no", "label": "No"},
    {"id": "not_sure", "label": "Not sure / Need more info"},
]

# Poll interval for waiting for user answers (in seconds)
ANSWER_WAIT_POLL_INTERVAL = 5.0


class CancellationError(Exception):
    """Raised when a job cancellation is detected."""
    pass


def _check_cancellation(job_id: str) -> None:
    """Check if cancellation has been requested and raise CancellationError if so."""
    if is_cancel_requested(job_id):
        logger.info("Cancellation detected for job %s", job_id)
        raise CancellationError(f"Job {job_id} was cancelled")


def _convert_to_structured_questions(
    questions: List[str],
    source: str = "planning",
) -> List[Dict[str, Any]]:
    """
    Convert free-text clarification questions to structured questions with options.

    Each question gets:
    - A unique ID based on index
    - Default yes/no/not_sure options
    - An 'other' option is implicitly available in the UI
    """
    import uuid
    structured = []
    for idx, question_text in enumerate(questions):
        question_id = f"{source}_{idx}_{uuid.uuid4().hex[:8]}"
        structured.append({
            "id": question_id,
            "question_text": question_text,
            "context": None,
            "options": DEFAULT_CLARIFICATION_OPTIONS.copy(),
            "required": True,
            "source": source,
        })
    return structured


def _wait_for_user_answers(
    job_id: str,
    timeout_seconds: float = 3600.0,
) -> bool:
    """
    Poll job store until waiting_for_answers becomes False.

    Returns True if answers were received, False if timed out or job failed.
    """
    start = time.time()
    while time.time() - start < timeout_seconds:
        if not is_waiting_for_answers(job_id):
            return True
        job_data = get_job(job_id)
        if job_data and job_data.get("status") in (JOB_STATUS_FAILED, JOB_STATUS_COMPLETED):
            return False
        time.sleep(ANSWER_WAIT_POLL_INTERVAL)
    logger.warning("Timed out waiting for user answers on job %s", job_id)
    update_job(job_id, status=JOB_STATUS_FAILED, error="Timed out waiting for user answers")
    return False


def _get_task_stats() -> Dict[str, Any]:
    """Get task counts from execution tracker: completed, in_progress, queued."""
    snap = execution_tracker.snapshot()
    tasks = snap.get("tasks", [])
    total = len(tasks)
    completed = sum(1 for t in tasks if t.get("status") == "done")
    in_progress = sum(1 for t in tasks if t.get("status") == "in_progress")
    queued = sum(1 for t in tasks if t.get("status") == "pending")
    percent = round((completed / total) * 100.0, 1) if total > 0 else 0.0
    return {
        "completed": completed,
        "in_progress": in_progress,
        "queued": queued,
        "total": total,
        "percent": percent,
    }


def _log_task_completion_banner(
    task_id: str,
    task_title: str,
    assignee: str,
    elapsed_seconds: float,
    log_prefix: str = "",
    description: str = "",
) -> None:
    """Log a big, flashy banner when a task is considered complete."""
    stats = _get_task_stats()
    title_display = (task_title[:50] + "...") if len(task_title) > 53 else task_title
    desc_display = (description[:56] + "...") if len(description) > 59 else (description or "-")
    assignee_display = assignee.replace("_", " ").title()

    # Progress bar (40 chars wide)
    bar_width = 40
    filled = int((stats["percent"] / 100.0) * bar_width) if stats["total"] > 0 else 0
    bar = "█" * filled + "░" * (bar_width - filled)

    header = "  ★★★★★  TASK COMPLETE  ★★★★★" + ("  [RETRY]" if log_prefix else "")
    logger.info("")
    logger.info("╔" + "═" * (BANNER_WIDTH - 2) + "╗")
    logger.info("║%s║", header.ljust(BANNER_WIDTH - 2))
    logger.info("╠" + "═" * (BANNER_WIDTH - 2) + "╣")
    logger.info("║  Task:        %-54s║", (task_id[:54] + "..") if len(task_id) > 56 else task_id)
    logger.info("║  Title:       %-54s║", title_display)
    logger.info("║  Description: %-54s║", desc_display)
    logger.info("║  Assignee:    %-54s║", assignee_display)
    logger.info("║  Elapsed:     %-54s║", f"{elapsed_seconds:.1f}s")
    logger.info("╠" + "═" * (BANNER_WIDTH - 2) + "╣")
    progress_line = f"  [{bar}] {stats['percent']:5.1f}%"
    logger.info("║%-70s║", progress_line)
    stats_line = f"  ✓ Completed: {stats['completed']}  |  ⟳ In Progress: {stats['in_progress']}  |  ◷ Queued: {stats['queued']}"
    logger.info("║%-70s║", stats_line)
    logger.info("╚" + "═" * (BANNER_WIDTH - 2) + "╝")
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
        except (OSError, ValueError, UnicodeDecodeError) as e:
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


def _get_agents() -> Dict[str, Any]:
    """Lazy init agents including the code review, documentation, and DbC comments agents.
    Each agent uses get_llm_for_agent(key) for per-agent model configuration.
    Main pipeline uses planning_v2_team for planning; spec_intake/project_planning/domain planning agents
    are not used in the main flow (clarification_store may still use Spec Intake elsewhere)."""
    from accessibility_agent import AccessibilityExpertAgent, AccessibilityInput
    from architecture_expert import ArchitectureExpertAgent, ArchitectureInput
    from code_review_agent import CodeReviewAgent, CodeReviewInput
    from technical_writers.dbc_comments_agent import DbcCommentsAgent, DbcCommentsInput
    from devops_team import DevOpsTeamLeadAgent
    from technical_writers.documentation_agent import DocumentationAgent, DocumentationInput
    from frontend_team_deprecated.feature_agent import FrontendExpertAgent, FrontendInput
    from git_setup_agent import GitSetupAgent
    from integration_team import IntegrationAgent, IntegrationInput
    from qa_agent import QAExpertAgent, QAInput
    from security_agent import CybersecurityExpertAgent, SecurityInput
    from tech_lead_agent import TechLeadAgent, TechLeadInput
    from acceptance_verifier_agent import AcceptanceVerifierAgent
    from agent_repair_team import RepairExpertAgent, RepairInput
    from linting_tool_agent import LintingToolAgent
    from build_fix_specialist import BuildFixSpecialistAgent

    return {
        "architecture": ArchitectureExpertAgent(get_llm_for_agent("architecture")),
        "integration": IntegrationAgent(get_llm_for_agent("integration")),
        "acceptance_verifier": AcceptanceVerifierAgent(get_llm_for_agent("acceptance_verifier")),
        "tech_lead": TechLeadAgent(get_llm_for_agent("tech_lead")),
        "devops": DevOpsTeamLeadAgent(get_llm_for_agent("devops")),
        "backend": _lazy_init_backend_code_v2_team(),
        "frontend": FrontendExpertAgent(get_llm_for_agent("frontend")),
        "frontend_code_v2": _lazy_init_frontend_code_v2_team(),
        "security": CybersecurityExpertAgent(get_llm_for_agent("security")),
        "qa": QAExpertAgent(get_llm_for_agent("qa")),
        "accessibility": AccessibilityExpertAgent(get_llm_for_agent("accessibility")),
        "code_review": CodeReviewAgent(get_llm_for_agent("code_review")),
        "dbc_comments": DbcCommentsAgent(get_llm_for_agent("dbc_comments")),
        "documentation": DocumentationAgent(get_llm_for_agent("documentation")),
        "git_setup": GitSetupAgent(),
        "repair": RepairExpertAgent(get_llm_for_agent("repair")),
        "linting_tool_agent": LintingToolAgent(get_llm_for_agent("linting_tool_agent")),
        "build_fix_specialist": BuildFixSpecialistAgent(get_llm_for_agent("build_fix_specialist")),
    }


def _lazy_init_backend_code_v2_team():
    """Instantiate the backend team lead (backend_code_v2_team; lazy import)."""
    from backend_code_v2_team import BackendCodeV2TeamLead
    return BackendCodeV2TeamLead(get_llm_for_agent("backend"))


def _lazy_init_frontend_code_v2_team():
    """Instantiate the frontend team lead (frontend_code_v2_team; lazy import)."""
    from frontend_code_v2_team import FrontendCodeV2TeamLead
    return FrontendCodeV2TeamLead(get_llm_for_agent("frontend"))


_task_requirements = task_requirements

MAX_REVIEW_ITERATIONS = 100
MAX_CLARIFICATION_REFINEMENTS = 100  # Max times to refine a task based on specialist clarification
MAX_CODE_REVIEW_ITERATIONS = 100    # Max rounds of code review -> fix -> re-review


def _issues_to_dicts(qa_bugs: Any, sec_vulns: Any) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Convert QA/Security outputs to dict lists for coding agent input."""
    qa_list = [b.model_dump() if hasattr(b, "model_dump") else b.dict() for b in (qa_bugs or [])]
    sec_list = [v.model_dump() if hasattr(v, "model_dump") else v.dict() for v in (sec_vulns or [])]
    return qa_list, sec_list


# _read_repo_code and _truncate_for_context are now in shared.repo_utils
_read_repo_code = read_repo_code
_truncate_for_context = truncate_for_context


def _build_task_update(task_id: str, agent_type: str, result: Any, status: str = "completed") -> TaskUpdate:
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
    from technical_writers.dbc_comments_agent.models import DbcCommentsInput
    from software_engineering_team.shared.git_utils import write_files_and_commit

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
    from software_engineering_team.shared.context_sizing import compute_existing_code_chars

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
    from software_engineering_team.shared.context_sizing import compute_code_review_total_chars
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


def _code_review_issues_to_dicts(issues: Any) -> List[Dict[str, Any]]:
    """Convert CodeReviewIssue objects to dicts for coding agent input."""
    return [
        i.model_dump() if hasattr(i, "model_dump") else i.dict()
        for i in (issues or [])
    ]


def _log_code_review_result(review_result: Any, task_id: str) -> None:
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
    from software_engineering_team.shared.command_runner import run_command, run_ng_build_with_nvm_fallback, run_python_syntax_check, run_pytest

    if agent_type == "frontend":
        # repo_path may be frontend repo root (package.json here) or work path (frontend/ subdir)
        frontend_dir = repo_path if (repo_path / "package.json").exists() else (repo_path / "frontend")
        if not (frontend_dir / "package.json").exists():
            logger.info("Build verification: no frontend project found, skipping frontend build")
            return True, ""
        from software_engineering_team.shared.command_runner import is_ng_build_environment_failure
        result = run_ng_build_with_nvm_fallback(frontend_dir)
        if not result.success:
            if is_ng_build_environment_failure(result):
                # Environment (e.g. Node version) - caller should fail task, not retry
                return False, "ENV:" + result.error_summary
            # Try tool-agent build fix (review all issues, fix one at a time)
            fixed, fix_error = _try_build_fix_one_at_a_time(repo_path, agent_type, task_id)
            if fixed:
                logger.info("Build verification passed for frontend task %s after tool-agent fix", task_id)
                return True, ""
            failures = result.parsed_failures("ng_build")
            if failures:
                from software_engineering_team.shared.error_parsing import build_agent_feedback, get_failure_class_tag
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
            fixed, fix_error = _try_build_fix_one_at_a_time(repo_path, agent_type, task_id)
            if fixed:
                logger.info("Build verification passed for backend task %s after tool-agent fix", task_id)
                return True, ""
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
                    from software_engineering_team.shared.error_parsing import build_agent_feedback, get_failure_class_tag
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
                fixed, fix_error = _try_build_fix_one_at_a_time(repo_path, agent_type, task_id)
                if fixed:
                    logger.info("Build verification passed for backend task %s after tool-agent fix", task_id)
                    return True, ""
                return False, summary
        logger.info("Build verification passed for backend task %s", task_id)
        return True, ""

    elif agent_type == "devops":
        # Validate YAML files and run docker build if Dockerfile exists
        import yaml
        from software_engineering_team.shared.command_runner import run_command

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

        # Docker build if Dockerfile exists and Docker is installed
        dockerfile = repo_path / "Dockerfile"
        if dockerfile.exists():
            # Check if Docker is available before attempting build
            docker_check = run_command(["docker", "--version"], cwd=repo_path, timeout=10)
            if not docker_check.success or "Command not found" in docker_check.stderr:
                logger.info(
                    "Docker not installed; skipping docker build verification for task %s. "
                    "Dockerfile was created but cannot be verified.",
                    task_id,
                )
            else:
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


def _try_build_fix_one_at_a_time(
    repo_path: Path,
    agent_type: str,
    task_id: str,
) -> tuple[bool, str]:
    """
    Use a tool-agent style flow to identify all build issues, then fix them one at a time.
    Returns (True, "") if build passes after fixes; otherwise (False, error_summary).
    """
    from software_engineering_team.shared.command_runner import (
        run_command,
        run_ng_build_with_nvm_fallback,
        run_python_syntax_check,
        run_pytest,
    )

    if agent_type == "frontend":
        project_dir = repo_path if (repo_path / "package.json").exists() else repo_path / "frontend"
        if not (project_dir / "package.json").exists():
            return False, "No frontend project found"
        try:
            from software_engineering_team.shared.command_runner import is_ng_build_environment_failure
            result = run_ng_build_with_nvm_fallback(project_dir)
        except Exception as e:
            logger.warning("Build fix: ng build failed to run: %s", e)
            return False, str(e)
        if result.success:
            return True, ""
        if is_ng_build_environment_failure(result):
            return False, result.error_summary
        failures = result.parsed_failures("ng_build")
        issues = []
        for f in failures:
            issues.append({
                "description": (f.message or f.raw_excerpt or "")[:500],
                "file_path": (f.file_path or "")[:300],
                "recommendation": (f.suggestion or f.playbook_hint or "Fix the build error.")[:500],
            })
        if not issues:
            issues.append({
                "description": result.error_summary[:500],
                "file_path": "",
                "recommendation": "Fix the build error.",
            })
        language = "typescript"
        prompt_module = "frontend_code_v2_team.prompts"
    elif agent_type == "backend":
        project_dir = repo_path if any(repo_path.rglob("*.py")) else repo_path / "backend"
        if not project_dir.exists() or not any(project_dir.rglob("*.py")):
            return False, "No Python project found"
        result = run_python_syntax_check(project_dir)
        test_result = None
        issues = []
        if not result.success:
            stderr = (result.stderr or "").strip()
            if stderr.startswith("Syntax errors found:"):
                for line in stderr.split("\n")[1:]:
                    line = line.strip()
                    if not line or ":" not in line:
                        continue
                    path, _, msg = line.partition(":")
                    path, msg = path.strip(), msg.strip()
                    if path and msg:
                        issues.append({
                            "description": msg[:500],
                            "file_path": path[:300],
                            "recommendation": "Fix the syntax error in this file.",
                        })
            if not issues:
                issues.append({
                    "description": result.error_summary[:500],
                    "file_path": "",
                    "recommendation": "Fix the syntax errors.",
                })
        else:
            tests_dir = project_dir / "tests"
            if tests_dir.exists() and any(tests_dir.rglob("test_*.py")):
                req_txt = project_dir / "requirements.txt"
                if req_txt.exists():
                    try:
                        run_command(
                            [sys.executable, "-m", "pip", "install", "-r", "requirements.txt"],
                            cwd=project_dir,
                            timeout=120,
                        )
                    except Exception:
                        pass
                test_result = run_pytest(project_dir, python_exe=sys.executable)
                if not test_result.success:
                    for f in test_result.parsed_failures("pytest"):
                        issues.append({
                            "description": (f.message or f.raw_excerpt or "")[:500],
                            "file_path": (f.file_path or "")[:300],
                            "recommendation": (f.suggestion or f.playbook_hint or "Fix the test or implementation.")[:500],
                        })
                    if not issues:
                        issues.append({
                            "description": test_result.pytest_error_summary()[:500],
                            "file_path": "",
                            "recommendation": "Fix the failing tests.",
                        })
        if not issues:
            return True, ""
        if test_result is not None:
            result = test_result
        language = "python"
        prompt_module = "backend_code_v2_team.prompts"
    else:
        return False, "Unsupported agent_type for build fix"

    # Read current files from project_dir (relative paths)
    current_files: Dict[str, str] = {}
    ext_map = {"frontend": (".ts", ".tsx", ".html", ".scss", ".css", ".js", ".jsx"), "backend": (".py",)}
    exts = ext_map.get(agent_type, (".py",))
    max_chars = 30_000
    total = 0
    for ext in exts:
        for f in project_dir.rglob(f"*{ext}"):
            if not f.is_file() or any(p in f.parts for p in ("node_modules", ".git", "dist", "build", "__pycache__", ".angular")):
                continue
            try:
                rel = str(f.relative_to(project_dir))
                content = f.read_text(encoding="utf-8", errors="replace")
                current_files[rel] = content
                total += len(content) + len(rel)
                if total > max_chars:
                    break
            except Exception:
                continue
        if total > max_chars:
            break

    try:
        llm = get_llm_for_agent("build_fix_specialist")
    except Exception as e:
        logger.warning("Build fix: could not get LLM: %s", e)
        return False, result.error_summary if agent_type == "frontend" else (result.error_summary if "result" in dir() else "Build failed")

    from backend_code_v2_team.output_templates import parse_problem_solving_single_issue_template
    if prompt_module == "frontend_code_v2_team.prompts":
        from frontend_code_v2_team.prompts import PROBLEM_SOLVING_SINGLE_ISSUE_PROMPT as FIX_PROMPT
        language_conventions = ""
    else:
        from backend_code_v2_team.prompts import (
            JAVA_CONVENTIONS,
            PROBLEM_SOLVING_SINGLE_ISSUE_PROMPT as FIX_PROMPT,
            PYTHON_CONVENTIONS,
        )
        language_conventions = JAVA_CONVENTIONS if language == "java" else PYTHON_CONVENTIONS

    max_fix_attempts = 15
    for attempt in range(max_fix_attempts):
        if not issues:
            break
        issue = issues.pop(0)
        desc = issue["description"]
        logger.info(
            "[%s] Build fix attempt %d/%d: Next step -> Fixing issue: %s",
            task_id, attempt + 1, max_fix_attempts, desc[:80],
        )
        file_path = issue.get("file_path") or ""
        rec = issue.get("recommendation") or "Fix the issue."
        # Build relevant code snippet
        if file_path and file_path in current_files:
            relevant_code = f"--- {file_path} ---\n{current_files[file_path][:8000]}"
        else:
            parts = []
            for p, c in list(current_files.items())[:10]:
                parts.append(f"--- {p} ---\n{c[:2000]}")
            relevant_code = "\n".join(parts) if parts else "(no code)"
        if prompt_module == "frontend_code_v2_team.prompts":
            prompt = FIX_PROMPT.format(
                source="build",
                severity="critical",
                description=desc,
                file_path=file_path or "N/A",
                recommendation=rec,
                current_code=relevant_code,
            )
        else:
            prompt = FIX_PROMPT.format(
                language_conventions=language_conventions,
                source="build",
                severity="critical",
                description=desc,
                file_path=file_path or "N/A",
                recommendation=rec,
                current_code=relevant_code,
            )
        try:
            raw = llm.complete_text(prompt)
        except Exception as e:
            logger.warning(
                "[%s] Build fix attempt %d/%d failed: LLM call error: %s. Next step -> Skipping to next issue",
                task_id, attempt + 1, max_fix_attempts, e,
            )
            continue
        parsed = parse_problem_solving_single_issue_template(raw)
        fixed_files = parsed.get("files") or {}
        if not fixed_files:
            continue
        for rel_path, content in fixed_files.items():
            out_path = project_dir / rel_path
            try:
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_text(content, encoding="utf-8")
                current_files[rel_path] = content
            except Exception as e:
                logger.warning("Build fix: could not write %s: %s", rel_path, e)
        # Re-run build
        if agent_type == "frontend":
            result = run_ng_build_with_nvm_fallback(project_dir)
        else:
            result = run_python_syntax_check(project_dir)
            if result.success:
                tests_dir = project_dir / "tests"
                if tests_dir.exists() and any(tests_dir.rglob("test_*.py")):
                    result = run_pytest(project_dir, python_exe=sys.executable)
        if result.success:
            logger.info(
                "Build fix (tool agent): task %s build passed after fixing one issue at a time",
                task_id,
            )
            return True, ""
        # Collect remaining issues for next iteration
        if agent_type == "frontend":
            failures = result.parsed_failures("ng_build")
            issues = [{"description": (f.message or f.raw_excerpt or "")[:500], "file_path": (f.file_path or "")[:300], "recommendation": (f.suggestion or f.playbook_hint or "Fix.")[:500]} for f in failures]
            if not issues:
                issues.append({"description": result.error_summary[:500], "file_path": "", "recommendation": "Fix."})
        else:
            if not result.success:
                stderr = (result.stderr or "").strip()
                issues = []
                if stderr.startswith("Syntax errors found:"):
                    for line in stderr.split("\n")[1:]:
                        line = line.strip()
                        if ":" in line:
                            path, _, msg = line.partition(":")
                            path, msg = path.strip(), msg.strip()
                            if path and msg:
                                issues.append({"description": msg[:500], "file_path": path[:300], "recommendation": "Fix syntax."})
                if not issues:
                    issues.append({"description": result.error_summary[:500], "file_path": "", "recommendation": "Fix."})
            else:
                test_result = run_pytest(project_dir, python_exe=sys.executable)
                result = test_result
                if not result.success:
                    issues = [{"description": (f.message or f.raw_excerpt or "")[:500], "file_path": (f.file_path or "")[:300], "recommendation": (f.suggestion or f.playbook_hint or "Fix.")[:500]} for f in result.parsed_failures("pytest")]
                    if not issues:
                        issues.append({"description": result.pytest_error_summary()[:500], "file_path": "", "recommendation": "Fix."})

    error_summary = result.error_summary if hasattr(result, "error_summary") else "Build still failing after fix attempts"
    logger.error(
        "[%s] Build fix exhausted. Recovery summary: attempted %d fix iterations, "
        "each applying LLM-generated patches then re-running build. Final error: %s",
        task_id, max_fix_attempts, error_summary[:200],
    )
    return False, error_summary


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


def _backend_code_v2_worker(
    *,
    job_id: str,
    backend_code_v2_queue: List[str],
    all_tasks: Dict[str, Any],
    completed: set,
    failed: Dict[str, str],
    completed_code_task_ids: List[str],
    architecture: Any,
    agents: Dict[str, Any],
    repo_path: Path,
) -> None:
    """
    Worker that drains ``backend_code_v2_queue`` by calling the
    backend team's (backend_code_v2_team) ``run_workflow``.
    Designed to run in its own thread, parallel with frontend worker.
    """
    from software_engineering_team.shared.models import SystemArchitecture
    team_lead = agents.get("backend")
    if team_lead is None:
        for tid in backend_code_v2_queue:
            failed[tid] = "backend team not registered"
        return

    while backend_code_v2_queue:
        # Check for cancellation before starting each task
        if is_cancel_requested(job_id):
            logger.info("Backend worker: cancellation detected, stopping")
            return
            
        task_id = backend_code_v2_queue.pop(0)
        task = all_tasks.get(task_id)
        if not task:
            continue

        update_job(job_id, current_task=task_id)
        update_task_state(job_id, task_id, status="in_progress", started_at=_iso_now())
        update_job_team_progress(job_id, "backend-code-v2", current_task_id=task_id)
        logger.info("[%s] >>> backend worker starting task", task_id)
        task_start = time.monotonic()

        def _job_updater(**kwargs: Any) -> None:
            update_job_team_progress(job_id, "backend-code-v2", **kwargs)

        try:
            arch = architecture if isinstance(architecture, SystemArchitecture) else (
                SystemArchitecture(overview=str(architecture)) if architecture else None
            )
            result = team_lead.run_workflow(
                repo_path=repo_path,
                task=task,
                architecture=arch,
                qa_agent=agents.get("qa"),
                security_agent=agents.get("security"),
                code_review_agent=agents.get("code_review"),
                build_verifier=_run_build_verification,
                doc_agent=agents.get("documentation"),
                linting_tool_agent=agents.get("linting_tool_agent"),
                tech_lead=agents.get("tech_lead"),
                build_fix_specialist=agents.get("build_fix_specialist"),
                job_updater=_job_updater,
            )
            elapsed = time.monotonic() - task_start
            if result.success:
                completed.add(task_id)
                completed_code_task_ids.append(task_id)
                update_task_state(job_id, task_id, status="done", finished_at=_iso_now())
                _log_task_completion_banner(
                    task_id=task_id,
                    task_title=getattr(task, "title", "") or task_id,
                    assignee=getattr(task, "assignee", "backend"),
                    elapsed_seconds=elapsed,
                    description=getattr(task, "description", "") or "",
                )
            else:
                reason = result.failure_reason or "backend workflow did not succeed"
                failed[task_id] = reason
                update_task_state(job_id, task_id, status="failed", finished_at=_iso_now(), error=reason)
                logger.warning("[%s] backend task failed: %s", task_id, reason)
        except Exception as exc:
            failed[task_id] = f"backend exception: {exc}"
            update_task_state(job_id, task_id, status="failed", finished_at=_iso_now(), error=str(exc))
            logger.exception("[%s] backend worker exception", task_id)


def _frontend_code_v2_worker(
    *,
    job_id: str,
    frontend_code_v2_queue: List[str],
    all_tasks: Dict[str, Any],
    completed: set,
    failed: Dict[str, str],
    completed_code_task_ids: List[str],
    architecture: Any,
    agents: Dict[str, Any],
    repo_path: Path,
) -> None:
    """Worker that drains frontend_code_v2_queue by calling frontend_code_v2_team run_workflow."""
    from software_engineering_team.shared.models import SystemArchitecture
    team_lead = agents.get("frontend_code_v2")
    if team_lead is None:
        for tid in frontend_code_v2_queue:
            failed[tid] = "frontend_code_v2 team not registered"
        return

    while frontend_code_v2_queue:
        # Check for cancellation before starting each task
        if is_cancel_requested(job_id):
            logger.info("Frontend worker: cancellation detected, stopping")
            return
            
        task_id = frontend_code_v2_queue.pop(0)
        task = all_tasks.get(task_id)
        if not task:
            continue

        update_job(job_id, current_task=task_id)
        update_task_state(job_id, task_id, status="in_progress", started_at=_iso_now())
        update_job_team_progress(job_id, "frontend-code-v2", current_task_id=task_id)
        logger.info("[%s] >>> frontend_code_v2 worker starting task", task_id)
        task_start = time.monotonic()

        def _job_updater(**kwargs: Any) -> None:
            update_job_team_progress(job_id, "frontend-code-v2", **kwargs)

        try:
            arch = architecture if isinstance(architecture, SystemArchitecture) else (
                SystemArchitecture(overview=str(architecture)) if architecture else None
            )
            result = team_lead.run_workflow(
                repo_path=repo_path,
                task=task,
                architecture=arch,
                qa_agent=agents.get("qa"),
                security_agent=agents.get("security"),
                code_review_agent=agents.get("code_review"),
                build_verifier=_run_build_verification,
                doc_agent=agents.get("documentation"),
                linting_tool_agent=agents.get("linting_tool_agent"),
                job_updater=_job_updater,
            )
            elapsed = time.monotonic() - task_start
            if result.success:
                completed.add(task_id)
                completed_code_task_ids.append(task_id)
                update_task_state(job_id, task_id, status="done", finished_at=_iso_now())
                _log_task_completion_banner(
                    task_id=task_id,
                    task_title=getattr(task, "title", "") or task_id,
                    assignee=getattr(task, "assignee", "frontend-code-v2"),
                    elapsed_seconds=elapsed,
                    description=getattr(task, "description", "") or "",
                )
            else:
                reason = result.failure_reason or "frontend_code_v2 workflow did not succeed"
                failed[task_id] = reason
                update_task_state(job_id, task_id, status="failed", finished_at=_iso_now(), error=reason)
                logger.warning("[%s] frontend_code_v2 task failed: %s", task_id, reason)
        except Exception as exc:
            failed[task_id] = f"frontend_code_v2 exception: {exc}"
            update_task_state(job_id, task_id, status="failed", finished_at=_iso_now(), error=str(exc))
            logger.exception("[%s] frontend_code_v2 worker exception", task_id)


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
    llm_connectivity_failed = [False]  # frontend could not reach LLM after retries; pause job
    repaired_tasks = set()  # max 1 repair per task
    agent_source_path = Path(__file__).resolve().parent  # software_engineering_team/

    def _remaining_queue_ids() -> List[str]:
        with state_lock:
            return list(backend_queue) + list(frontend_queue)

    DEP_WAIT_SLEEP = 0.5  # seconds to wait when no runnable task (dependencies pending)

    def _backend_worker() -> None:
        while True:
            # Check for cancellation
            if is_cancel_requested(job_id):
                logger.info("Legacy backend worker: cancellation detected, stopping")
                return
            with state_lock:
                if llm_limit_exceeded[0] or llm_connectivity_failed[0]:
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
            update_task_state(job_id, task_id, status="in_progress", started_at=_iso_now())
            update_job_team_progress(job_id, "backend", current_task_id=task_id)
            execution_tracker.start_task(task_id)
            log_prefix = "[RETRY] " if is_retry else ""
            logger.info("%s[%s] >>> Backend worker starting task %s", log_prefix, task_id, task_id)
            task_start_time = time.monotonic()
            try:
                from software_engineering_team.shared.command_runner import ensure_backend_project_initialized
                init_result = ensure_backend_project_initialized(backend_dir)
                if not init_result.success:
                    update_task_state(job_id, task_id, status="failed", finished_at=_iso_now(), error=init_result.error_summary)
                    with state_lock:
                        failed[task_id] = f"Backend init failed: {init_result.error_summary}"
                    continue
                if not (backend_dir / ".git").exists():
                    gs_result = agents["git_setup"].run(backend_dir)
                    if not gs_result.success:
                        update_task_state(job_id, task_id, status="failed", finished_at=_iso_now(), error=gs_result.message)
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
                    architecture=None,
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
                    linting_tool_agent=agents.get("linting_tool_agent"),
                    build_fix_specialist=agents.get("build_fix_specialist"),
                )
                elapsed = time.monotonic() - task_start_time
                failure_reason = workflow_result.failure_reason or "Backend workflow failed"
                with state_lock:
                    if workflow_result.success:
                        completed.add(task_id)
                        completed_code_task_ids.append(task_id)
                        update_task_state(job_id, task_id, status="done", finished_at=_iso_now())
                        execution_tracker.observe_loop(task_id, 1)
                        execution_tracker.finish_task(task_id)
                        _log_task_completion_banner(
                            task_id=task_id,
                            task_title=getattr(task, "title", "") or task_id,
                            assignee="backend",
                            elapsed_seconds=elapsed,
                            log_prefix=log_prefix,
                            description=getattr(task, "description", "") or "",
                        )
                    else:
                        failed[task_id] = failure_reason
                        update_task_state(job_id, task_id, status="failed", finished_at=_iso_now(), error=failure_reason)
                        execution_tracker.observe_loop(task_id, 1)
                        execution_tracker.finish_task(task_id, blocked=True)
                        logger.warning("%s[%s] Backend FAILED after %.1fs: %s", log_prefix, task_id, elapsed, failed[task_id])
            except (LLMError, httpx.HTTPError) as e:
                err_msg = (
                    OLLAMA_WEEKLY_LIMIT_MESSAGE if isinstance(e, LLMRateLimitError)
                    else "LLM rate limited or temporarily unavailable – please retry later" if isinstance(e, LLMTemporaryError)
                    else str(e) if isinstance(e, LLMPermanentError)
                    else f"LLM error: {e}"
                )
                update_task_state(job_id, task_id, status="failed", finished_at=_iso_now(), error=err_msg)
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
                    logger.info(
                        "%s[%s] Exception is repairable (%s). Next step -> Attempting automatic repair",
                        log_prefix, task_id, type(e).__name__,
                    )
                    repair_agent = agents.get("repair")
                    if repair_agent:
                        try:
                            from agent_repair_team.models import RepairInput
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
                                logger.info("%s[%s] Repair applied. Next step -> Re-queuing task for retry", log_prefix, task_id)
                        except Exception as repair_err:
                            logger.warning("%s[%s] Repair agent failed: %s", log_prefix, task_id, repair_err)
                if not repair_applied:
                    update_task_state(job_id, task_id, status="failed", finished_at=_iso_now(), error=str(e))
                    with state_lock:
                        failed[task_id] = f"Unhandled exception: {e}"
                    logger.error(
                        "%s[%s] Backend task failed. Recovery summary: 1) Workflow execution failed, "
                        "2) Repair %s. Final error: %s",
                        log_prefix, task_id,
                        "attempted but unsuccessful" if type(e) in REPAIRABLE_EXCEPTIONS else "not applicable (non-repairable exception)",
                        e,
                    )
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
            # Check for cancellation
            if is_cancel_requested(job_id):
                logger.info("Legacy frontend worker: cancellation detected, stopping")
                return
            with state_lock:
                if llm_limit_exceeded[0] or llm_connectivity_failed[0]:
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
            update_task_state(job_id, task_id, status="in_progress", started_at=_iso_now())
            update_job_team_progress(job_id, "frontend", current_task_id=task_id)
            execution_tracker.start_task(task_id)
            log_prefix = "[RETRY] " if is_retry else ""
            logger.info("%s[%s] >>> Frontend worker starting task %s", log_prefix, task_id, task_id)
            task_start_time = time.monotonic()
            try:
                from software_engineering_team.shared.command_runner import ensure_frontend_project_initialized
                from software_engineering_team.shared.frontend_framework import resolve_frontend_framework
                # Detect framework from task metadata, project files, or spec
                task_meta = getattr(task, "metadata", None) or {}
                detected_framework = resolve_frontend_framework(task_meta, spec_content, frontend_dir)
                init_result = ensure_frontend_project_initialized(frontend_dir, framework=detected_framework)
                if not init_result.success:
                    update_task_state(job_id, task_id, status="failed", finished_at=_iso_now(), error=init_result.error_summary)
                    with state_lock:
                        failed[task_id] = f"Frontend init failed: {init_result.error_summary}"
                    continue
                if not (frontend_dir / ".git").exists():
                    gs_result = agents["git_setup"].run(frontend_dir)
                    if not gs_result.success:
                        update_task_state(job_id, task_id, status="failed", finished_at=_iso_now(), error=gs_result.message)
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
                    architecture=None,
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
                    linting_tool_agent=agents.get("linting_tool_agent"),
                    build_fix_specialist=agents.get("build_fix_specialist"),
                )

                elapsed = time.monotonic() - task_start_time
                failure_reason = workflow_result.failure_reason or "Frontend workflow failed"
                with state_lock:
                    if workflow_result.success:
                        completed.add(task_id)
                        completed_code_task_ids.append(task_id)
                        update_task_state(job_id, task_id, status="done", finished_at=_iso_now())
                        execution_tracker.observe_loop(task_id, 1)
                        execution_tracker.finish_task(task_id)
                        _log_task_completion_banner(
                            task_id=task_id,
                            task_title=getattr(task, "title", "") or task_id,
                            assignee="frontend",
                            elapsed_seconds=elapsed,
                            log_prefix=log_prefix,
                            description=getattr(task, "description", "") or "",
                        )
                    else:
                        failed[task_id] = failure_reason
                        update_task_state(job_id, task_id, status="failed", finished_at=_iso_now(), error=failure_reason)
                        execution_tracker.observe_loop(task_id, 1)
                        execution_tracker.finish_task(task_id, blocked=True)
                        logger.warning("%s[%s] Frontend FAILED after %.1fs: %s", log_prefix, task_id, elapsed, failed[task_id])
                logger.info("%s[%s] <<< Frontend worker done (completed=%s)", log_prefix, task_id, workflow_result.success)
                if getattr(workflow_result, "llm_unreachable", False):
                    update_task_state(job_id, task_id, status="failed", finished_at=_iso_now(), error=LLM_UNREACHABLE_AFTER_RETRIES)
                    with state_lock:
                        llm_connectivity_failed[0] = True
                        failed[task_id] = LLM_UNREACHABLE_AFTER_RETRIES
                    logger.warning("Frontend reported LLM unreachable; pausing job %s", job_id)
                    break
            except (LLMError, httpx.HTTPError) as e:
                err_msg = (
                    OLLAMA_WEEKLY_LIMIT_MESSAGE if isinstance(e, LLMRateLimitError)
                    else "LLM rate limited or temporarily unavailable – please retry later" if isinstance(e, LLMTemporaryError)
                    else str(e) if isinstance(e, LLMPermanentError)
                    else f"LLM error: {e}"
                )
                update_task_state(job_id, task_id, status="failed", finished_at=_iso_now(), error=err_msg)
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
                elif isinstance(e, LLMPermanentError):
                    logger.warning("%s[%s] Frontend task generation failed validation: %s", log_prefix, task_id, e)
                else:
                    logger.warning("%s[%s] Frontend task LLM/HTTP error: %s", log_prefix, task_id, e)
                # On JSON parse failure, ask Tech Lead to break task into smaller subtasks (plan update).
                _is_json_parse_failure = isinstance(e, LLMJsonParseError) or (
                    isinstance(e, LLMPermanentError)
                    and "json" in str(e).lower()
                    and ("parse" in str(e).lower() or "invalid" in str(e).lower())
                )
                if _is_json_parse_failure:
                    failure_class = "json_parse" if isinstance(e, LLMJsonParseError) else None
                    task_update = TaskUpdate(
                        task_id=task_id,
                        agent_type="frontend",
                        status="failed",
                        summary="",
                        files_changed=[],
                        failure_reason=err_msg,
                        failure_class=failure_class,
                    )
                    remaining_ids = _remaining_queue_ids()
                    def _append_task_by_assignee(tid: str) -> None:
                        with state_lock:
                            t = all_tasks.get(tid)
                            assignee = getattr(t, "assignee", "") or "backend"
                            if assignee in ("frontend", "frontend-code-v2"):
                                frontend_queue.append(tid)
                            else:
                                backend_queue.append(tid)
                    try:
                        _run_tech_lead_review(
                            tech_lead=tech_lead,
                            task_update=task_update,
                            spec_content=spec_content,
                            architecture=architecture,
                            all_tasks=all_tasks,
                            completed=completed,
                            execution_queue=remaining_ids,
                            repo_path=frontend_dir,
                            doc_agent=None,
                            append_task_id_fn=_append_task_by_assignee,
                        )
                    except Exception as review_err:
                        logger.warning(
                            "%s[%s] Tech Lead review after JSON parse failure failed (non-blocking): %s",
                            log_prefix,
                            task_id,
                            review_err,
                        )
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
                    logger.info(
                        "%s[%s] Exception is repairable (%s). Next step -> Attempting automatic repair",
                        log_prefix, task_id, type(e).__name__,
                    )
                    repair_agent = agents.get("repair")
                    if repair_agent:
                        try:
                            from agent_repair_team.models import RepairInput
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
                                logger.info("%s[%s] Repair applied. Next step -> Re-queuing task for retry", log_prefix, task_id)
                        except Exception as repair_err:
                            logger.warning("%s[%s] Repair agent failed: %s", log_prefix, task_id, repair_err)
                if not repair_applied:
                    update_task_state(job_id, task_id, status="failed", finished_at=_iso_now(), error=str(e))
                    with state_lock:
                        failed[task_id] = f"Unhandled exception: {e}"
                    logger.error(
                        "%s[%s] Frontend task failed. Recovery summary: 1) Workflow execution failed, "
                        "2) Repair %s. Final error: %s",
                        log_prefix, task_id,
                        "attempted but unsuccessful" if type(e) in REPAIRABLE_EXCEPTIONS else "not applicable (non-repairable exception)",
                        e,
                    )
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


def run_orchestrator(
    job_id: str,
    repo_path: str | Path,
    *,
    spec_content_override: Optional[str] = None,
    resolved_questions_override: Optional[List[Dict[str, Any]]] = None,
    planning_only: bool = False,
) -> None:
    """
    Main orchestration loop. Runs in background thread.

    Work path (repo_path) is the folder where work is saved; it does not need to be a git repo.
    Backend and frontend each have their own repo at work_path/backend and work_path/frontend,
    initialized by the Git Setup Agent before first use. Backend and frontend tasks run in parallel.

    Optional overrides:
    - spec_content_override: use this instead of loading spec from repo
    - resolved_questions_override: user-provided answers from clarification; passed to Tech Lead
    - planning_only: when True, run spec intake through conformance then stop (no execution)
    """
    path = Path(repo_path).resolve()
    backend_dir = path / "backend"
    frontend_dir = path / "frontend"
    try:
        # Check for cancellation at start
        _check_cancellation(job_id)
        
        update_job(job_id, status=JOB_STATUS_RUNNING, phase="planning", status_text="Starting pipeline")

        agents = _get_agents()

        # 1. Read spec from work path or use override (no git required at root)
        from spec_parser import get_newest_spec_path, get_newest_spec_content, parse_spec_with_llm, gather_context_files
        initial_spec_path = None
        if spec_content_override is not None:
            spec_content = spec_content_override
        else:
            initial_spec_path = get_newest_spec_path(path)
            spec_content = get_newest_spec_content(path)
        
        # Gather all context files from the repo for PRA agent
        context_files = gather_context_files(path)
        if context_files:
            logger.info("Gathered %d context files for PRA agent", len(context_files))
        try:
            requirements = parse_spec_with_llm(spec_content, get_llm_for_agent("spec_intake"))
        except LLMRateLimitError:
            logger.warning("Ollama LLM usage limit exceeded for week. Job %s paused.", job_id)
            update_job(job_id, status="paused_llm_limit", error=OLLAMA_WEEKLY_LIMIT_MESSAGE)
            return
        except Exception as e:
            logger.error("Spec parsing failed (LLM unavailable or returned invalid output): %s", e)
            update_job(job_id, status=JOB_STATUS_FAILED, error=f"Spec parsing failed: {e}", phase="completed")
            return
        update_job(job_id, requirements_title=requirements.title, status_text="Specification parsed successfully")
        
        # Check for cancellation after spec parsing
        _check_cancellation(job_id)

        # Create plan folder after spec is ingested successfully (all planning artifacts go here)
        plan_dir = ensure_plan_dir(path)
        logger.info("Plan folder ensured at %s", plan_dir)

        # ── Step 1: Product Requirements Analysis Agent ───────────────────────
        # Validates spec, asks user questions, produces validated_spec.md
        from product_requirements_analysis_agent import ProductRequirementsAnalysisAgent

        PRA_PHASE_ORDER = ["spec_review", "communicate", "spec_update", "spec_cleanup"]

        def _pra_job_updater(**kwargs: Any) -> None:
            try:
                analysis_phase = kwargs.pop("current_phase", None)
                if analysis_phase:
                    kwargs["analysis_subprocess"] = analysis_phase
                    completed_phases = []
                    for p in PRA_PHASE_ORDER:
                        if p == analysis_phase:
                            break
                        completed_phases.append(p)
                    kwargs["analysis_completed_phases"] = completed_phases
                update_job(job_id, phase="product_analysis", **kwargs)
            except Exception:
                pass

        update_job(job_id, phase="product_analysis", message="Starting product requirements analysis...", status_text="Starting product requirements analysis")
        logger.info("Next step -> Running Product Requirements Analysis agent to validate spec and gather clarifications")
        pra_agent = ProductRequirementsAnalysisAgent(get_llm_for_agent("product_analysis"))
        pra_result = pra_agent.run_workflow(
            spec_content=spec_content,
            repo_path=path,
            job_id=job_id,
            job_updater=_pra_job_updater,
            context_files=context_files,
            initial_spec_path=initial_spec_path,
        )
        if not pra_result.success:
            err = pra_result.failure_reason or "Product Requirements Analysis did not complete successfully."
            logger.error("Product Requirements Analysis failed: %s", err)
            update_job(job_id, status=JOB_STATUS_FAILED, error=err, phase="completed")
            return

        # Use validated spec for all downstream agents
        validated_spec = pra_result.final_spec_content or spec_content
        logger.info(
            "Product Requirements Analysis complete: %d iterations, validated spec ready",
            pra_result.iterations,
        )
        
        # Check for cancellation after PRA
        _check_cancellation(job_id)

        # ── Step 2: Planning V2 Team ──────────────────────────────────────────
        # Receives validated spec, performs planning (skips spec review)
        update_job(job_id, phase="planning", message="Starting planning workflow...", status_text="Starting planning workflow")
        logger.info("Next step -> Running Planning V2 team to generate architecture and task breakdown")

        from planning_v2_team import PlanningV2TeamLead
        from planning_v2_adapter import adapt_planning_v2_result, PlanningV2AdapterResult

        PLANNING_V2_PHASE_ORDER = [
            "intake", "planning", "implementation", "review", "problem_solving", "deliver"
        ]

        def _planning_v2_job_updater(**kwargs: Any) -> None:
            try:
                planning_phase = kwargs.pop("current_phase", None)
                if planning_phase:
                    kwargs["planning_subprocess"] = planning_phase
                    completed_phases = []
                    for p in PLANNING_V2_PHASE_ORDER:
                        if p == planning_phase:
                            break
                        completed_phases.append(p)
                    kwargs["planning_completed_phases"] = completed_phases
                update_job(job_id, **kwargs)
            except Exception:
                pass

        # Optionally pass PRD from PRA output so Planning V2 does not need to read from disk
        prd_content: Optional[str] = None
        prd_file = path / "plan" / "product_analysis" / "product_requirements_document.md"
        if prd_file.exists():
            try:
                prd_content = prd_file.read_text(encoding="utf-8")
            except Exception as e:
                logger.warning("Could not read PRD from plan/product_analysis: %s", e)

        planning_v2_lead = PlanningV2TeamLead(get_llm_for_agent("project_planning"))
        p2_result = planning_v2_lead.run_workflow(
            spec_content=validated_spec,
            repo_path=path,
            inspiration_content=None,
            job_updater=_planning_v2_job_updater,
            validated_spec_content=validated_spec,
            prd_content=prd_content,
        )
        if not p2_result.success:
            err = p2_result.failure_reason or "Planning-v2 workflow did not complete successfully."
            logger.error("Planning-v2 failed: %s", err)
            update_job(job_id, status=JOB_STATUS_FAILED, error=err, phase="completed")
            return

        try:
            adapter_result: PlanningV2AdapterResult = adapt_planning_v2_result(p2_result, spec_title=requirements.title)
        except ValueError as e:
            logger.error("Planning-v2 adapter failed: %s", e)
            update_job(job_id, status=JOB_STATUS_FAILED, error=str(e), phase="completed")
            return

        requirements = adapter_result.requirements
        project_overview = adapter_result.project_overview
        spec_intake_open_questions = adapter_result.open_questions
        spec_intake_assumptions = adapter_result.assumptions
        # Use the final approved product spec from Planning V2 if available
        spec_content_for_planning = adapter_result.final_spec_content or spec_content
        update_job(job_id, requirements_title=requirements.title)

        # Check for cancellation after Planning V2
        _check_cancellation(job_id)

        # Planning process: (1) features doc from planning-v2 adapter; (2) tasks from Tech Lead; (3) architecture from Architecture Expert;
        # (4) consolidation; (5) execution.
        features_and_functionality_doc = (project_overview.get("features_and_functionality_doc") or "").strip()

        # Optional: Run Enterprise Architect for richer architecture context (SW_USE_ENTERPRISE_ARCHITECT=true)
        enterprise_arch_context: Optional[str] = None
        if (os.environ.get("SW_USE_ENTERPRISE_ARCHITECT") or "").strip().lower() in ("1", "true", "yes"):
            try:
                if _arch_dir.exists():
                    from integration import run_enterprise_architect
                    ea_result = run_enterprise_architect(
                        spec_content=spec_content_for_planning,
                        work_path=str(path),
                    )
                    if ea_result.get("success") and ea_result.get("architecture_overview"):
                        enterprise_arch_context = ea_result["architecture_overview"]
                        logger.info(
                            "Enterprise Architect produced architecture package at %s",
                            ea_result.get("outputs_path", ""),
                        )
                    elif ea_result.get("error"):
                        logger.warning("Enterprise Architect failed: %s", ea_result["error"])
            except Exception as e:
                logger.warning("Enterprise Architect integration skipped: %s", e)

        from architecture_expert.models import ArchitectureInput
        from tech_lead_agent.models import TechLeadInput

        from software_engineering_team.shared.context_sizing import compute_existing_code_chars

        arch_agent = agents["architecture"]
        tech_lead = agents["tech_lead"]
        max_code_chars = compute_existing_code_chars(tech_lead.llm)
        existing_code = _truncate_for_context(_read_repo_code(path), max_code_chars)

        # Single-pass planning: Tech Lead produces Initiative/Epic/Story hierarchy
        # If Planning V2 produced a hierarchy, pass it to Tech Lead to use directly
        planning_v2_hierarchy = adapter_result.hierarchy
        tech_lead_output = None
        assignment = None
        try:
            tech_lead_output = tech_lead.run(TechLeadInput(
                requirements=requirements,
                repo_path=str(path),
                spec_content=spec_content_for_planning,
                existing_codebase=existing_code if existing_code != "# No code files found" else None,
                project_overview=project_overview,
                open_questions=[q.question_text if hasattr(q, 'question_text') else str(q) for q in spec_intake_open_questions] if spec_intake_open_questions else None,
                assumptions=spec_intake_assumptions if spec_intake_assumptions else None,
                resolved_questions=resolved_questions_override,
                planning_hierarchy=planning_v2_hierarchy,
            ))
        except LLMRateLimitError:
            logger.warning("Ollama LLM usage limit exceeded for week. Job %s paused.", job_id)
            update_job(job_id, status="paused_llm_limit", error=OLLAMA_WEEKLY_LIMIT_MESSAGE)
            return

        if tech_lead_output.spec_clarification_needed:
            questions = tech_lead_output.clarification_questions or []
            if questions:
                structured_questions = _convert_to_structured_questions(
                    questions,
                    source="tech_lead",
                )
                add_pending_questions(job_id, structured_questions)
                logger.info(
                    "Job %s waiting for %d clarification answers from user",
                    job_id,
                    len(structured_questions),
                )
                _wait_for_user_answers(job_id)
                job_data = get_job(job_id)
                if job_data and job_data.get("status") == JOB_STATUS_FAILED:
                    return
            else:
                error_msg = "Spec is unclear but no clarification questions provided."
                logger.warning(error_msg)
                update_job(job_id, status=JOB_STATUS_FAILED, error=error_msg, phase="completed")
                return

        assignment = tech_lead_output.assignment
        if not assignment or not assignment.tasks:
            logger.error("Tech Lead produced no tasks; failing job")
            update_job(job_id, status=JOB_STATUS_FAILED, error="Tech Lead produced no tasks", phase="completed")
            return

        # Architecture (single pass, no iteration)
        architecture = None
        arch_input = ArchitectureInput(
            requirements=requirements,
            technology_preferences=["Python", "FastAPI", "PostgreSQL", "Docker"],
            project_overview=project_overview,
            features_and_functionality_doc=features_and_functionality_doc or None,
            existing_architecture=enterprise_arch_context,
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

        # Store execution order and per-task state for API tracking panel; set phase to execution
        _task_map = {t.id: t for t in assignment.tasks}

        def _extract_task_state(tid: str) -> dict:
            """Extract task state including hierarchy metadata."""
            task = _task_map.get(tid)
            metadata = getattr(task, "metadata", {}) or {}
            return {
                "status": "pending",
                "assignee": getattr(task, "assignee", "unknown") or "unknown",
                "title": getattr(task, "title", tid) or tid,
                "dependencies": getattr(task, "dependencies", []) or [],
                "initiative_id": metadata.get("initiative_id"),
                "epic_id": metadata.get("epic_id"),
                "story_id": metadata.get("story_id"),
            }

        task_states = {tid: _extract_task_state(tid) for tid in assignment.execution_order}

        # Build planning hierarchy summary for UI work breakdown tree
        hierarchy_summary = None
        if tech_lead_output and tech_lead_output.planning_hierarchy:
            hierarchy = tech_lead_output.planning_hierarchy
            initiatives = []
            epics = []
            stories = []
            for init in hierarchy.initiatives:
                initiatives.append({
                    "id": init.id,
                    "title": init.title,
                    "description": getattr(init, "description", "") or "",
                })
                for epic in init.epics:
                    epics.append({
                        "id": epic.id,
                        "title": epic.title,
                        "description": getattr(epic, "description", "") or "",
                        "initiative_id": init.id,
                    })
                    for story in epic.stories:
                        stories.append({
                            "id": story.id,
                            "title": story.title,
                            "description": getattr(story, "description", "") or "",
                            "epic_id": epic.id,
                            "initiative_id": init.id,
                        })
            hierarchy_summary = {
                "initiatives": initiatives,
                "epics": epics,
                "stories": stories,
            }

        update_job(
            job_id,
            execution_order=assignment.execution_order,
            task_states=task_states,
            planning_hierarchy=hierarchy_summary,
            phase="execution",
            status_text="Starting task execution",
        )
        
        # Check for cancellation before execution
        _check_cancellation(job_id)
        
        if planning_only:
            logger.info("Planning-only run: stopping before execution (re-plan-with-clarifications)")
            update_job(job_id, status="completed", phase="completed")
            return

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
        backend_queue: List[str] = []  # All backend work routed to backend_code_v2_team via backend_code_v2_queue
        backend_code_v2_queue: List[str] = [
            tid for tid in full_order
            if all_tasks.get(tid) and all_tasks[tid].assignee in ("backend", "backend-code-v2")
        ]
        frontend_queue: List[str] = []  # All frontend work routed to frontend_code_v2_team via frontend_code_v2_queue
        frontend_code_v2_queue: List[str] = [
            tid for tid in full_order
            if all_tasks.get(tid) and all_tasks[tid].assignee in ("frontend", "frontend-code-v2")
        ]
        total_tasks = len(prefix_queue) + len(backend_queue) + len(backend_code_v2_queue) + len(frontend_queue) + len(frontend_code_v2_queue)

        logger.info(
            "=== Starting task execution: prefix=%s, backend=%s, frontend=%s, frontend_code_v2=%s ===",
            len(prefix_queue), len(backend_code_v2_queue), len(frontend_queue), len(frontend_code_v2_queue),
        )

        # Run prefix tasks sequentially (work path; devops writes to path/devops, no git)
        for task_id in prefix_queue:
            task = all_tasks.get(task_id)
            if not task:
                continue
            assignee = getattr(task, "assignee", "unknown") or "unknown"
            update_job(job_id, current_task=task_id)
            update_task_state(job_id, task_id, status="in_progress", started_at=_iso_now())
            update_job_team_progress(job_id, assignee, current_task_id=task_id)
            execution_tracker.start_task(task_id)
            if task.type.value == "git_setup":
                completed.add(task_id)
                update_task_state(job_id, task_id, status="done", finished_at=_iso_now())
                execution_tracker.finish_task(task_id)
                _log_task_completion_banner(
                    task_id=task_id,
                    task_title=getattr(task, "title", "") or task_id,
                    assignee="git_setup",
                    elapsed_seconds=0.0,
                    description=getattr(task, "description", "") or "",
                )
                continue
            if task.assignee == "devops":
                # Defer containerization to after backend and frontend complete; skip early devops run.
                completed.add(task_id)
                update_task_state(job_id, task_id, status="done", finished_at=_iso_now())
                execution_tracker.finish_task(task_id)
                _log_task_completion_banner(
                    task_id=task_id,
                    task_title=getattr(task, "title", "") or task_id,
                    assignee="devops",
                    elapsed_seconds=0.0,
                    description=getattr(task, "description", "") or "",
                )
                continue

        # Backend-code-v2 worker: run in parallel with backend and frontend workers
        backend_code_v2_thread = None
        if backend_code_v2_queue:
            backend_code_v2_thread = threading.Thread(
                target=_backend_code_v2_worker,
                kwargs=dict(
                    job_id=job_id,
                    backend_code_v2_queue=backend_code_v2_queue,
                    all_tasks=all_tasks,
                    completed=completed,
                    failed=failed,
                    completed_code_task_ids=completed_code_task_ids,
                    architecture=None,
                    agents=agents,
                    repo_path=backend_dir,
                ),
            )
            backend_code_v2_thread.daemon = True
            backend_code_v2_thread.start()

        # Frontend-code-v2 worker: run in parallel with other workers
        frontend_code_v2_thread = None
        if frontend_code_v2_queue:
            frontend_code_v2_thread = threading.Thread(
                target=_frontend_code_v2_worker,
                kwargs=dict(
                    job_id=job_id,
                    frontend_code_v2_queue=frontend_code_v2_queue,
                    all_tasks=all_tasks,
                    completed=completed,
                    failed=failed,
                    completed_code_task_ids=completed_code_task_ids,
                    architecture=None,
                    agents=agents,
                    repo_path=frontend_dir,
                ),
            )
            frontend_code_v2_thread.daemon = True
            frontend_code_v2_thread.start()

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

        if backend_code_v2_thread is not None:
            backend_code_v2_thread.join()
        if frontend_code_v2_thread is not None:
            frontend_code_v2_thread.join()

        llm_limit_exceeded = any(v == OLLAMA_WEEKLY_LIMIT_MESSAGE for v in failed.values())
        llm_connectivity_failed = any(v == LLM_UNREACHABLE_AFTER_RETRIES for v in failed.values())
        remaining_in_queues = len(backend_code_v2_queue) + len(frontend_queue) + len(frontend_code_v2_queue)
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
                "Unprocessed tasks still in queues: backend=%s, frontend=%s, frontend_code_v2=%s",
                len(backend_code_v2_queue), len(frontend_queue), len(frontend_code_v2_queue),
            )

        # Integration phase: validate backend-frontend API contract alignment
        integration_agent = agents.get("integration")
        has_backend = backend_dir.is_dir() and any(backend_dir.rglob("*.py"))
        has_frontend = frontend_dir.is_dir() and any(frontend_dir.rglob("*.ts"))
        if integration_agent and has_backend and has_frontend and completed_code_task_ids:
            try:
                from integration_team import IntegrationInput
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
            logger.info("Next step -> Running DevOps agent to containerize backend repo")
            existing_pipeline = _read_repo_code(backend_dir, [".yml", ".yaml"])
            tech_lead.trigger_devops_for_backend(
                devops_agent, backend_dir, architecture, spec_content,
                existing_pipeline=existing_pipeline if existing_pipeline != "# No code files found" else None,
                build_verifier=_run_build_verification,
            )
        if devops_agent and frontend_dir.is_dir() and (frontend_dir / ".git").exists():
            logger.info("Next step -> Running DevOps agent to containerize frontend repo")
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
            has_backend = any(
                all_tasks.get(tid) and all_tasks[tid].assignee in ("backend", "backend-code-v2")
                for tid in completed_code_task_ids
            )
            has_frontend = any(all_tasks.get(tid) and all_tasks[tid].assignee in ("frontend", "frontend-code-v2") for tid in completed_code_task_ids)
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

        # Final documentation pass: always run comprehensive documentation review for each repo
        doc_agent = agents.get("documentation")
        if doc_agent and completed_code_task_ids:
            logger.info(
                "Final documentation pass: starting (completed_code_tasks=%d)",
                len(completed_code_task_ids),
            )
            for repo_name, repo_dir in [("backend", backend_dir), ("frontend", frontend_dir)]:
                if not repo_dir.is_dir() or not (repo_dir / ".git").exists():
                    continue
                logger.info(
                    "Final documentation pass: running comprehensive documentation review for %s repo",
                    repo_name,
                )
                try:
                    if hasattr(doc_agent, "run_final_review"):
                        doc_agent.run_final_review(
                            repo_path=repo_dir,
                            repo_name=repo_name,
                            spec_content=spec_content,
                            architecture=architecture,
                            completed_task_ids=completed_code_task_ids,
                        )
                    else:
                        from software_engineering_team.shared.context_sizing import compute_existing_code_chars
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
                            task_summary="Final comprehensive documentation review: update all project documentation, README, and CONTRIBUTORS.",
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

        if llm_connectivity_failed:
            update_job(
                job_id,
                status=JOB_STATUS_PAUSED_LLM_CONNECTIVITY,
                error=LLM_UNREACHABLE_AFTER_RETRIES,
                progress=100,
                current_task=None,
            )
        elif llm_limit_exceeded:
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
            update_job(job_id, status=JOB_STATUS_COMPLETED, progress=100, current_task=None, phase="completed", status_text="All tasks completed successfully")

    except CancellationError:
        logger.info("Orchestrator stopped due to job cancellation: %s", job_id)
        update_job(job_id, status=JOB_STATUS_CANCELLED, status_text="Job cancelled by user", phase="completed")
    except Exception as e:
        logger.exception("Orchestrator failed")
        update_job(job_id, status=JOB_STATUS_FAILED, error=str(e), phase="completed")


def run_failed_tasks(job_id: str) -> None:
    """
    Re-run only the failed tasks from a previous job.

    Reads the persisted failed task list and task objects from the job store,
    re-queues them, and executes them through the same pipeline.
    Runs in a background thread (same pattern as run_orchestrator).
    """
    from software_engineering_team.shared.job_store import get_job
    from software_engineering_team.shared.models import Task

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
        from spec_parser import get_latest_spec_content
        spec_content = get_latest_spec_content(path)

        # Reconstruct minimal architecture from stored overview
        from software_engineering_team.shared.models import SystemArchitecture
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
        retry_backend_queue: List[str] = []  # All backend retries go through backend_code_v2_team
        retry_backend_code_v2_queue = [
            tid for tid in failed_ids
            if all_tasks.get(tid) and all_tasks[tid].assignee in ("backend", "backend-code-v2")
        ]
        retry_frontend_queue: List[str] = []  # All frontend retries go through frontend_code_v2_team
        retry_frontend_code_v2_queue = [
            tid for tid in failed_ids
            if all_tasks.get(tid) and all_tasks[tid].assignee in ("frontend", "frontend-code-v2")
        ]
        total_tasks = len(retry_prefix) + len(retry_backend_queue) + len(retry_backend_code_v2_queue) + len(retry_frontend_queue) + len(retry_frontend_code_v2_queue)

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
                    description=getattr(task, "description", "") or "",
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
                        tech_stack=["Python", "FastAPI", "PostgreSQL", "Docker"],
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
                            description=getattr(task, "description", "") or "",
                        )
                    else:
                        failed_retry[task_id] = workflow_result.failure_reason or "DevOps workflow failed"
                except Exception as e:
                    failed_retry[task_id] = f"DevOps failed: {e}"

        # Run backend-code-v2 retry in parallel
        retry_bv2_thread = None
        if retry_backend_code_v2_queue:
            retry_bv2_thread = threading.Thread(
                target=_backend_code_v2_worker,
                kwargs=dict(
                    job_id=job_id,
                    backend_code_v2_queue=retry_backend_code_v2_queue,
                    all_tasks=all_tasks,
                    completed=completed,
                    failed=failed_retry,
                    completed_code_task_ids=completed_code_task_ids,
                    architecture=None,
                    agents=agents,
                    repo_path=backend_dir,
                ),
            )
            retry_bv2_thread.daemon = True
            retry_bv2_thread.start()

        # Run frontend-code-v2 retry in parallel
        retry_fv2_thread = None
        if retry_frontend_code_v2_queue:
            retry_fv2_thread = threading.Thread(
                target=_frontend_code_v2_worker,
                kwargs=dict(
                    job_id=job_id,
                    frontend_code_v2_queue=retry_frontend_code_v2_queue,
                    all_tasks=all_tasks,
                    completed=completed,
                    failed=failed_retry,
                    completed_code_task_ids=completed_code_task_ids,
                    architecture=None,
                    agents=agents,
                    repo_path=frontend_dir,
                ),
            )
            retry_fv2_thread.daemon = True
            retry_fv2_thread.start()

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

        if retry_bv2_thread is not None:
            retry_bv2_thread.join()
        if retry_fv2_thread is not None:
            retry_fv2_thread.join()

        llm_limit_exceeded = any(v == OLLAMA_WEEKLY_LIMIT_MESSAGE for v in failed_retry.values())
        llm_connectivity_failed = any(v == LLM_UNREACHABLE_AFTER_RETRIES for v in failed_retry.values())

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
            logger.warning(
                "=== Still-failed task report. Recovery summary: re-attempted %d tasks, "
                "%d completed successfully, %d remain failed ===",
                total_tasks, len(completed), len(failed_retry),
            )
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
        if llm_connectivity_failed:
            update_job(
                job_id,
                failed_tasks=failed_details,
                status=JOB_STATUS_PAUSED_LLM_CONNECTIVITY,
                error=LLM_UNREACHABLE_AFTER_RETRIES,
            )
        elif llm_limit_exceeded:
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
            update_job(job_id, failed_tasks=failed_details, status=JOB_STATUS_COMPLETED, current_task=None, status_text="Retry completed")

    except CancellationError:
        logger.info("Retry orchestrator stopped due to job cancellation: %s", job_id)
        update_job(job_id, status=JOB_STATUS_CANCELLED, status_text="Job cancelled by user")
    except Exception as e:
        logger.exception("Retry orchestrator failed")
        update_job(job_id, status=JOB_STATUS_FAILED, error=str(e))
