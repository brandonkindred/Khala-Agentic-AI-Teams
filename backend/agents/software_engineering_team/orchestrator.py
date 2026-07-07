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

# Path setup when run as module
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

_team_dir = Path(__file__).resolve().parent
if str(_team_dir) not in sys.path:
    sys.path.insert(0, str(_team_dir))
_arch_dir = _team_dir / "architect-agents"
if _arch_dir.exists() and str(_arch_dir) not in sys.path:
    sys.path.insert(0, str(_arch_dir))

from strands import Agent  # noqa: E402

from llm_service import (  # noqa: E402
    OLLAMA_WEEKLY_LIMIT_MESSAGE,
    LLMRateLimitError,
    get_client,
    get_strands_model,
    llm_attribution,
)
from shared_repo_context.repo_utils import (  # noqa: E402
    read_repo_code,
    truncate_for_context,
)
from software_engineering_team.shared import (  # noqa: E402
    cost_tracker,
    se_events,
)
from software_engineering_team.shared.execution_tracker import execution_tracker  # noqa: E402
from software_engineering_team.shared.job_store import (  # noqa: E402
    JOB_STATUS_CANCELLED,
    JOB_STATUS_COMPLETED,
    JOB_STATUS_FAILED,
    JOB_STATUS_RUNNING,
    LLM_SEMANTIC_EXHAUSTION,
    LLM_UNREACHABLE_AFTER_RETRIES,
    add_pending_questions,
    get_job,
    is_cancel_requested,
    is_waiting_for_answers,
    update_job,
)
from software_engineering_team.shared.models import TaskUpdate  # noqa: E402
from software_engineering_team.shared.plan_dir import ensure_plan_dir  # noqa: E402
from software_engineering_team.shared.task_utils import task_requirements  # noqa: E402

try:
    from unified_api.slack_notifier import notify_open_questions as slack_notify_open_questions
except ImportError:
    slack_notify_open_questions = None

logger = logging.getLogger(__name__)


def _iso_now() -> str:
    """Return current UTC time as ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


def _partition_tasks_by_completion(
    all_tasks: Dict[str, Any],
    completed_ids: set,
    remaining_ids: set,
) -> Tuple[List[Any], List[Any]]:
    """Split ``all_tasks`` into (completed, remaining) task lists in one pass.

    Replaces the duplicated pair of list comprehensions that each independently
    re-scanned ``all_tasks`` (one for completed ids, one for remaining ids).

    Preconditions:
        - ``completed_ids`` and ``remaining_ids`` are sets (O(1) membership).
    Postconditions:
        - Returns ``(completed_tasks, remaining_tasks)`` preserving ``all_tasks``
          iteration order. A task id present in *both* sets appears in both
          lists, exactly as the two separate comprehensions produced.
    """
    assert isinstance(completed_ids, set), f"completed_ids must be a set, got {type(completed_ids)}"
    assert isinstance(remaining_ids, set), f"remaining_ids must be a set, got {type(remaining_ids)}"
    completed_tasks: List[Any] = []
    remaining_tasks: List[Any] = []
    for tid, task in all_tasks.items():
        if tid in completed_ids:
            completed_tasks.append(task)
        if tid in remaining_ids:
            remaining_tasks.append(task)
    return completed_tasks, remaining_tasks


# The SE job owns the allocation of its progress bar across phases. Sub-agents
# (PRA, Planning, coding team) each report their OWN 0-100 progress; the job
# updaters rescale those onto the phase's band so the bar is monotone across the
# whole run instead of repeatedly sprinting to 100 and collapsing at each handoff.
PROGRESS_BAND_PRODUCT_ANALYSIS = (0, 15)
PROGRESS_BAND_PLANNING = (15, 15)
PROGRESS_BAND_CODING = (30, 65)

# Pytest node-id fragments that identify the generic-exception-handler test suite. When a build
# verification failure matches one of these, `_run_build_verification` appends a canonical FIX hint
# telling the fixer to preserve the /test-generic-error route and keep the handler returning a
# JSONResponse. Kept here (rather than in the retired per-task pipeline) because the build
# verification path is the sole remaining consumer.
EXCEPTION_HANDLER_TEST_PATTERNS = (
    "test-generic-error",
    "test_generic_exception_handler",
    "test_error_handlers",
)


def _scale_progress(pct: Any, band: "tuple[int, int]") -> Optional[int]:
    """Map a sub-agent's 0-100 progress onto the SE job's [base, base+span] band.

    Preconditions:
        - ``band`` is (base, span) with 0 <= base, 0 <= span, base + span <= 100.
    Postconditions:
        - Returns an int in [base, base+span]; non-numeric input yields None
          (callers drop the field rather than writing garbage); out-of-range
          input is clamped to the band.
    """
    base, span = band
    assert 0 <= base and 0 <= span and base + span <= 100, band
    try:
        value = float(pct)
    except (TypeError, ValueError):
        return None
    value = min(max(value, 0.0), 100.0)
    return base + int(span * value / 100.0)


PRA_PHASE_ORDER = ["spec_review", "communicate", "spec_update", "spec_cleanup"]
PLANNING_PHASE_ORDER = [
    "intake",
    "discovery",
    "requirements",
    "synthesis",
    "document_production",
    "sub_agent_provisioning",
]


def _make_pra_job_updater(job_id: str) -> Callable[..., None]:
    """Build the job updater handed to the PRA agent.

    Postconditions: the returned updater rewrites ``current_phase`` into
    ``analysis_subprocess``/``analysis_completed_phases``, rescales the agent's own
    0-100 ``progress`` onto PROGRESS_BAND_PRODUCT_ANALYSIS (garbage progress is
    dropped, never written), and swallows store errors (observability only).
    """

    def _updater(**kwargs: Any) -> None:
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
            # The PRA agent reports its own 0-100 progress; rescale onto this
            # phase's band so the job bar cannot hit 100 before coding starts.
            if "progress" in kwargs:
                scaled = _scale_progress(kwargs.pop("progress"), PROGRESS_BAND_PRODUCT_ANALYSIS)
                if scaled is not None:
                    kwargs["progress"] = scaled
            update_job(job_id, phase="product_analysis", **kwargs)
        except Exception:
            pass

    return _updater


def _make_planning_job_updater(job_id: str) -> Callable[..., None]:
    """Build the job updater handed to the Planning workflow.

    Postconditions: mirrors :func:`_make_pra_job_updater` for the planning phase —
    ``current_phase`` becomes ``planning_subprocess``/``planning_completed_phases``
    and ``progress`` is rescaled onto PROGRESS_BAND_PLANNING.
    """

    def _updater(**kwargs: Any) -> None:
        try:
            planning_phase = kwargs.pop("current_phase", None)
            if planning_phase:
                kwargs["planning_subprocess"] = planning_phase
                completed_phases = []
                for p in PLANNING_PHASE_ORDER:
                    if p == planning_phase:
                        break
                    completed_phases.append(p)
                kwargs["planning_completed_phases"] = completed_phases
            # Planning reports its own 0-100 progress; rescale onto this
            # phase's band so the job bar stays monotone into the coding phase.
            if "progress" in kwargs:
                scaled = _scale_progress(kwargs.pop("progress"), PROGRESS_BAND_PLANNING)
                if scaled is not None:
                    kwargs["progress"] = scaled
            update_job(job_id, **kwargs)
        except Exception:
            pass

    return _updater


def _llm_pause_error(failed: Dict[str, str]) -> str:
    """Job-level error message for an LLM-condition pause, derived from the failed map.

    Preconditions:
        - ``failed`` maps task ids to failure-reason strings; the caller has
          already determined that at least one value matched an LLM pause
          sentinel (the ``llm_connectivity_failed`` aggregation).
    Postconditions:
        - Returns ``LLM_SEMANTIC_EXHAUSTION`` when any failed task carries it —
          its remediation (simplify or split the prompt) must not be masked by
          the connectivity guidance — otherwise ``LLM_UNREACHABLE_AFTER_RETRIES``.
    """
    if any(v == LLM_SEMANTIC_EXHAUSTION for v in failed.values()):
        return LLM_SEMANTIC_EXHAUSTION
    return LLM_UNREACHABLE_AFTER_RETRIES


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

# Fallback options for clarification questions. Empty so the UI shows only the always-present
# free-text "other" field rather than misleading yes/no options for open-ended questions.
DEFAULT_CLARIFICATION_OPTIONS: List[Dict[str, Any]] = []

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
    - An empty options list (agents are prompted to supply context-specific options; when they
      don't, the UI falls back to the always-present free-text "other" field)
    """
    import uuid

    structured = []
    for idx, question_text in enumerate(questions):
        question_id = f"{source}_{idx}_{uuid.uuid4().hex[:8]}"
        structured.append(
            {
                "id": question_id,
                "question_text": question_text,
                "context": None,
                "options": DEFAULT_CLARIFICATION_OPTIONS.copy(),
                "required": True,
                "source": source,
            }
        )
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
    while (
        time.time() - start < timeout_seconds
    ):  # pragma: no cover  # integration-only: polling loop with real time.sleep
        if not is_waiting_for_answers(job_id):
            return True
        job_data = get_job(job_id)
        if job_data and job_data.get("status") in (JOB_STATUS_FAILED, JOB_STATUS_COMPLETED):
            return False
        time.sleep(ANSWER_WAIT_POLL_INTERVAL)
    logger.warning("Timed out waiting for user answers on job %s", job_id)
    update_job(job_id, status=JOB_STATUS_FAILED, error="Timed out waiting for user answers")
    return False


def _run_se_decision_gate(
    job_id: str, question_texts: List[str], source: str = "planning"
) -> "tuple[List[Dict[str, Any]], bool]":
    """Pause the job for the user to answer open questions, then return their resolved answers.

    Reuses the existing pending-questions / wait-for-answers machinery so an open question raised by
    planning never reaches implementation un-answered. Deterministic and fail-closed.

    Postconditions:
        - ``([], True)`` when there is nothing to ask.
        - ``(resolved, True)`` once the user answers (each entry carries the question text + answer,
          ready to thread into the coding team as ``resolved_questions``).
        - ``([], False)`` when the wait ended without answers (timeout/failure); ``_wait_for_user_answers``
          has already set the failure status on timeout, so the caller just stops.
    """
    from coding_team.hitl import answers_to_resolved

    structured = _convert_to_structured_questions(question_texts, source=source)
    if not structured:
        return [], True
    add_pending_questions(job_id, structured)
    if slack_notify_open_questions:
        slack_notify_open_questions(job_id, structured, source="run-team")
    logger.info(
        "Job %s waiting for %d clarification answer(s) from the user", job_id, len(structured)
    )
    if not _wait_for_user_answers(job_id):
        return [], False
    submitted = (get_job(job_id) or {}).get("submitted_answers") or []
    return answers_to_resolved(submitted, structured), True


def _build_planning_answer_callback(job_id: str) -> Callable[[list], list]:
    """Build an escalating answer callback for Planning PRA — surface questions, never auto-decide.

    When Planning's product-analysis phase asks clarification questions, this pauses the SE job
    and routes them to the user (instead of Planning auto-selecting a default). It preserves each
    PRA question's id/options so the submitted answers map straight back to PRA.

    Postconditions:
        - Returns a callable ``(questions) -> [{question_id, selected_option_id, other_text}]`` that
          only ever returns user-supplied answers (empty list if the wait ended without answers).
    """

    def _cb(questions: list) -> list:
        texts = [
            (q.get("question_text") or q.get("text") or "") if isinstance(q, dict) else str(q)
            for q in questions
        ]
        structured = _convert_to_structured_questions(texts, source="planning")
        for sq, oq in zip(structured, questions):
            if isinstance(oq, dict) and oq.get("id"):
                sq["id"] = str(oq["id"])
                if oq.get("options"):
                    sq["options"] = oq["options"]
        add_pending_questions(job_id, structured)
        if slack_notify_open_questions:
            slack_notify_open_questions(job_id, structured, source="run-team")
        if not _wait_for_user_answers(job_id):
            return []
        answered_ids = {sq["id"] for sq in structured}
        submitted = (get_job(job_id) or {}).get("submitted_answers") or []
        return [a for a in submitted if a.get("question_id") in answered_ids]

    return _cb


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
    title_display = task_title
    desc_display = description
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


def _parse_traceback_for_crash(
    exception: BaseException,
) -> tuple[str | None, int | None, str | None]:
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
    logger.error(
        "  *** AGENT CRASH (%s) ***%s", agent_type.capitalize(), "  [RETRY]" if log_prefix else ""
    )
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
                logger.warning(
                    "Repair: line range %d-%d out of bounds for %s", line_start, line_end, target
                )
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
    logger.info(
        "  Total: %d completed | %d failed | %d total", len(completed), failed_count, total_tasks
    )
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
    Each agent uses get_client(key) for per-agent model configuration.
    Main pipeline uses planning_team for planning; spec_intake/project_planning/domain planning agents
    are not used in the main flow (clarification_store may still use Spec Intake elsewhere)."""
    from acceptance_verifier_agent import AcceptanceVerifierAgent
    from accessibility_agent import AccessibilityExpertAgent
    from architecture_expert import ArchitectureExpertAgent
    from build_fix_specialist import BuildFixSpecialistAgent
    from code_review_agent import CodeReviewAgent
    from devops_team import DevOpsTeamLeadAgent
    from git_setup_agent import GitSetupAgent
    from integration_team import IntegrationAgent
    from linting_tool_agent import LintingToolAgent
    from qa_agent import QAExpertAgent
    from security_agent import CybersecurityExpertAgent
    from tech_lead_agent import TechLeadAgent
    from technical_writers.dbc_comments_agent import DbcCommentsAgent
    from technical_writers.documentation_agent import DocumentationAgent

    from agent_repair_team import RepairExpertAgent

    return {
        "architecture": ArchitectureExpertAgent(get_client("architecture")),
        "integration": IntegrationAgent(get_client("integration")),
        "acceptance_verifier": AcceptanceVerifierAgent(get_client("acceptance_verifier")),
        "tech_lead": TechLeadAgent(get_client("tech_lead")),
        "devops": DevOpsTeamLeadAgent(get_client("devops")),
        "backend": _lazy_init_backend_code_v2_team(),
        "frontend_code_v2": _lazy_init_frontend_code_v2_team(),
        "security": CybersecurityExpertAgent(get_client("security")),
        "qa": QAExpertAgent(get_client("qa")),
        "accessibility": AccessibilityExpertAgent(get_client("accessibility")),
        "code_review": CodeReviewAgent(get_client("code_review")),
        "dbc_comments": DbcCommentsAgent(get_client("dbc_comments")),
        "documentation": DocumentationAgent(get_client("documentation")),
        "git_setup": GitSetupAgent(),
        "repair": RepairExpertAgent(get_client("repair")),
        "linting_tool_agent": LintingToolAgent(get_client("linting_tool_agent")),
        "build_fix_specialist": BuildFixSpecialistAgent(get_client("build_fix_specialist")),
    }


def _lazy_init_backend_code_v2_team():
    """Instantiate the backend team lead (backend_code_v2_team; lazy import)."""
    from backend_code_v2_team import BackendCodeV2TeamLead

    return BackendCodeV2TeamLead(get_client("backend"))


def _lazy_init_frontend_code_v2_team():
    """Instantiate the frontend team lead (frontend_code_v2_team; lazy import)."""
    from frontend_code_v2_team import FrontendCodeV2TeamLead

    return FrontendCodeV2TeamLead(get_client("frontend"))


_task_requirements = task_requirements

MAX_REVIEW_ITERATIONS = 15
MAX_CLARIFICATION_REFINEMENTS = 10  # Max times to refine a task based on specialist clarification
MAX_CODE_REVIEW_ITERATIONS = 10  # Max rounds of code review -> fix -> re-review


# _read_repo_code and _truncate_for_context are now in shared.repo_utils
_read_repo_code = read_repo_code
_truncate_for_context = truncate_for_context


def _build_coding_team_plan_input(
    adapter_result: Any,
    repo_path: str,
    existing_code_summary: Optional[str] = None,
    resolved_questions: Optional[List[Dict[str, Any]]] = None,
) -> Any:
    """Build CodingTeamPlanInput from PlanningAdapterResult for coding_team orchestrator."""
    from coding_team.models import CodingTeamPlanInput

    req = adapter_result.requirements
    open_q = getattr(adapter_result, "open_questions", None) or []
    open_questions = [
        getattr(q, "question_text", str(q)) if hasattr(q, "question_text") else str(q)
        for q in open_q
    ]
    return CodingTeamPlanInput(
        requirements_title=getattr(req, "title", "Project"),
        requirements_description=getattr(req, "description", "") or "",
        project_overview=getattr(adapter_result, "project_overview", {}) or {},
        hierarchy=getattr(adapter_result, "hierarchy", None),
        final_spec_content=getattr(adapter_result, "final_spec_content", None),
        repo_path=repo_path,
        architecture_overview=getattr(adapter_result, "architecture_overview", None),
        existing_code_summary=existing_code_summary,
        resolved_questions=resolved_questions,
        open_questions=open_questions,
        assumptions=getattr(adapter_result, "assumptions", None) or [],
    )


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

    remaining_ids = set(execution_queue)
    completed_tasks, remaining_tasks = _partition_tasks_by_completion(
        all_tasks, completed, remaining_ids
    )
    max_code_chars = compute_existing_code_chars(
        tech_lead.llm
    )  # pragma: no cover  # integration-only: tech-lead review uses live LLM
    codebase_summary = _truncate_for_context(
        _read_repo_code(repo_path), max_code_chars
    )  # pragma: no cover  # integration-only

    new_tasks = tech_lead.review_progress(  # pragma: no cover  # integration-only: LLM call
        task_update=task_update,
        spec_content=spec_content,
        architecture=architecture,
        completed_tasks=completed_tasks,
        remaining_tasks=remaining_tasks,
        codebase_summary=codebase_summary,
    )

    if new_tasks:  # pragma: no cover  # integration-only: downstream of live LLM review
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
    if (
        doc_agent
    ):  # pragma: no cover  # integration-only: documentation agent runs LLM + writes docs to repo
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
    files: Dict[str, str] | None = None,
):
    """
    Run the code review agent on the given code.

    When *files* (a ``{path: content}`` mapping of the task's changed files) is
    provided it takes precedence over *code_to_review*; the agent ignores the
    legacy concatenated string and bounds its own per-call prompts.
    Returns the CodeReviewOutput.
    """
    from code_review_agent.models import build_code_review_input

    # No pre-truncation: the coordinator bounds its own per-call prompts, and
    # its full-coverage guarantee only holds when it sees all the code.
    review_input = build_code_review_input(
        files=files,
        code=None if files is not None else code_to_review,
        spec_content=spec_content,
        task_description=task.description,
        task_requirements=_task_requirements(task),
        acceptance_criteria=getattr(task, "acceptance_criteria", []) or [],
        language=language,
        architecture=architecture,
        existing_codebase=existing_codebase,
    )
    return agents["code_review"].run(review_input)


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
    from shared_command_runner.runner import (
        run_command,
        run_ng_build_with_nvm_fallback,
        run_pytest,
        run_python_syntax_check,
    )

    if (
        agent_type == "frontend"
    ):  # pragma: no cover  # integration-only: invokes ng build and downstream LLM fix loop
        # repo_path may be frontend repo root (package.json here) or work path (frontend/ subdir)
        frontend_dir = (
            repo_path if (repo_path / "package.json").exists() else (repo_path / "frontend")
        )
        if not (frontend_dir / "package.json").exists():
            logger.info("Build verification: no frontend project found, skipping frontend build")
            return True, ""
        from shared_command_runner.runner import is_ng_build_environment_failure

        result = run_ng_build_with_nvm_fallback(frontend_dir)
        if not result.success:
            if is_ng_build_environment_failure(result):
                # Environment (e.g. Node version) - caller should fail task, not retry
                return False, "ENV:" + result.error_summary
            # Try tool-agent build fix (review all issues, fix one at a time)
            fixed, fix_error = _try_build_fix_one_at_a_time(repo_path, agent_type, task_id)
            if fixed:
                logger.info(
                    "Build verification passed for frontend task %s after tool-agent fix", task_id
                )
                return True, ""
            failures = result.parsed_failures("ng_build")
            if failures:
                from shared_command_runner.error_parsing import (
                    build_agent_feedback,
                    get_failure_class_tag,
                )

                feedback = build_agent_feedback(failures)
                logger.warning(
                    "Build verification failed for task %s: %s",
                    task_id,
                    get_failure_class_tag(failures[0].failure_class),
                )
                return False, feedback
            logger.warning(
                "Build verification failed for task %s: %s", task_id, result.error_summary
            )
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
        if not result.success:  # pragma: no cover  # integration-only: syntax-check + LLM fix loop
            logger.warning("Syntax check failed for task %s: %s", task_id, result.error_summary)
            fixed, fix_error = _try_build_fix_one_at_a_time(repo_path, agent_type, task_id)
            if fixed:
                logger.info(
                    "Build verification passed for backend task %s after tool-agent fix", task_id
                )
                return True, ""
            return False, result.error_summary
        # Also try pytest if tests directory exists
        tests_dir = backend_dir / "tests"
        if tests_dir.exists() and any(tests_dir.rglob("test_*.py")):
            # Install deps before pytest so agent-added packages (e.g. sqlalchemy) are available
            req_txt = backend_dir / "requirements.txt"
            if (
                req_txt.exists()
            ):  # pragma: no cover  # integration-only: shells out to `pip install`
                try:
                    pip_result = run_command(
                        [sys.executable, "-m", "pip", "install", "-r", "requirements.txt"],
                        cwd=backend_dir,
                        timeout=120,
                    )
                    if not pip_result.success:
                        logger.warning(
                            "pip install -r requirements.txt failed (non-fatal): %s",
                            pip_result.error_summary,
                        )
                except Exception as e:
                    logger.warning("pip install before pytest failed (non-fatal): %s", e)
            test_result = run_pytest(backend_dir, python_exe=sys.executable)
            if not test_result.success:
                failures = test_result.parsed_failures("pytest")
                if failures:
                    from shared_command_runner.error_parsing import (
                        build_agent_feedback,
                        get_failure_class_tag,
                    )

                    summary = build_agent_feedback(failures)
                    logger.warning(
                        "Tests failed for task %s: %s",
                        task_id,
                        get_failure_class_tag(failures[0].failure_class),
                    )
                else:
                    summary = test_result.pytest_error_summary()
                # When failure matches exception-handler test patterns, append canonical FIX line
                if any(p in summary for p in EXCEPTION_HANDLER_TEST_PATTERNS):
                    summary += (
                        "\n\nFIX: Preserve the /test-generic-error route in app/main.py and "
                        "ensure the exception handler returns JSONResponse; do not re-raise."
                    )
                fixed, fix_error = _try_build_fix_one_at_a_time(repo_path, agent_type, task_id)
                if fixed:
                    logger.info(
                        "Build verification passed for backend task %s after tool-agent fix",
                        task_id,
                    )
                    return True, ""
                return False, summary
        logger.info("Build verification passed for backend task %s", task_id)
        return True, ""

    elif (
        agent_type == "devops"
    ):  # pragma: no cover  # integration-only: docker build + yaml parsing on real workflow files
        # Validate YAML files and run docker build if Dockerfile exists
        import yaml

        from shared_command_runner.runner import run_command

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
                    logger.warning(
                        "Docker build failed for task %s: %s", task_id, result.error_summary
                    )
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
    from shared_command_runner.runner import (
        run_command,
        run_ng_build_with_nvm_fallback,
        run_pytest,
        run_python_syntax_check,
    )

    if (
        agent_type == "frontend"
    ):  # pragma: no cover  # integration-only: invokes ng build + LLM repair loop
        project_dir = repo_path if (repo_path / "package.json").exists() else repo_path / "frontend"
        if not (project_dir / "package.json").exists():
            return False, "No frontend project found"
        try:
            from shared_command_runner.runner import (
                is_ng_build_environment_failure,
            )

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
            issues.append(
                {
                    "description": (f.message or f.raw_excerpt or ""),
                    "file_path": (f.file_path or ""),
                    "recommendation": (f.suggestion or f.playbook_hint or "Fix the build error."),
                }
            )
        if not issues:
            issues.append(
                {
                    "description": result.error_summary,
                    "file_path": "",
                    "recommendation": "Fix the build error.",
                }
            )
        language = "typescript"
        prompt_module = "frontend_code_v2_team.prompts"
    elif (
        agent_type == "backend"
    ):  # pragma: no cover  # integration-only: runs python syntax check + pytest + LLM repair loop
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
                        issues.append(
                            {
                                "description": msg,
                                "file_path": path,
                                "recommendation": "Fix the syntax error in this file.",
                            }
                        )
            if not issues:
                issues.append(
                    {
                        "description": result.error_summary,
                        "file_path": "",
                        "recommendation": "Fix the syntax errors.",
                    }
                )
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
                        issues.append(
                            {
                                "description": (f.message or f.raw_excerpt or ""),
                                "file_path": (f.file_path or ""),
                                "recommendation": (
                                    f.suggestion
                                    or f.playbook_hint
                                    or "Fix the test or implementation."
                                ),
                            }
                        )
                    if not issues:
                        issues.append(
                            {
                                "description": test_result.pytest_error_summary(),
                                "file_path": "",
                                "recommendation": "Fix the failing tests.",
                            }
                        )
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
    ext_map = {
        "frontend": (".ts", ".tsx", ".html", ".scss", ".css", ".js", ".jsx"),
        "backend": (".py",),
    }
    exts = ext_map.get(agent_type, (".py",))
    max_chars = 30_000
    total = 0
    for ext in exts:
        for f in project_dir.rglob(f"*{ext}"):
            if not f.is_file() or any(
                p in f.parts
                for p in ("node_modules", ".git", "dist", "build", "__pycache__", ".angular")
            ):
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
        # response_format="text": the build-fix loop parses the assistant
        # content as the template-based output of
        # parse_problem_solving_single_issue_template, not JSON. JSON mode
        # would break the template parser.
        _build_fix_model = get_strands_model("build_fix_specialist", response_format="text")
    except Exception as e:
        logger.warning("Build fix: could not get model: %s", e)
        return False, result.error_summary if agent_type == "frontend" else (
            result.error_summary if "result" in dir() else "Build failed"
        )

    from backend_code_v2_team.output_templates import parse_problem_solving_single_issue_template

    if prompt_module == "frontend_code_v2_team.prompts":
        from frontend_code_v2_team.prompts import PROBLEM_SOLVING_SINGLE_ISSUE_PROMPT as FIX_PROMPT

        language_conventions = ""
    else:
        from backend_code_v2_team.prompts import (
            JAVA_CONVENTIONS,
            PYTHON_CONVENTIONS,
        )
        from backend_code_v2_team.prompts import (
            PROBLEM_SOLVING_SINGLE_ISSUE_PROMPT as FIX_PROMPT,
        )

        language_conventions = JAVA_CONVENTIONS if language == "java" else PYTHON_CONVENTIONS

    max_fix_attempts = 15
    for attempt in range(
        max_fix_attempts
    ):  # pragma: no cover  # integration-only: LLM fix loop reruns build/test after each repair
        if not issues:
            break
        issue = issues.pop(0)
        desc = issue["description"]
        logger.info(
            "[%s] Build fix attempt %d/%d: Next step -> Fixing issue: %s",
            task_id,
            attempt + 1,
            max_fix_attempts,
            desc[:80],
        )
        file_path = issue.get("file_path") or ""
        rec = issue.get("recommendation") or "Fix the issue."
        # Build relevant code snippet
        if file_path and file_path in current_files:
            relevant_code = f"--- {file_path} ---\n{current_files[file_path][:50_000]}"
        else:
            parts = []
            remaining = 50_000
            for p, c in current_files.items():
                if remaining <= 0:
                    break
                snippet = c[:remaining]
                parts.append(f"--- {p} ---\n{snippet}")
                remaining -= len(snippet)
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
            _agent = Agent(model=_build_fix_model)
            _result = _agent(prompt)
            raw = str(_result).strip()
        except Exception as e:
            logger.warning(
                "[%s] Build fix attempt %d/%d failed: LLM call error: %s. Next step -> Skipping to next issue",
                task_id,
                attempt + 1,
                max_fix_attempts,
                e,
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
            issues = [
                {
                    "description": (f.message or f.raw_excerpt or ""),
                    "file_path": (f.file_path or ""),
                    "recommendation": (f.suggestion or f.playbook_hint or "Fix."),
                }
                for f in failures
            ]
            if not issues:
                issues.append(
                    {
                        "description": result.error_summary,
                        "file_path": "",
                        "recommendation": "Fix.",
                    }
                )
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
                                issues.append(
                                    {
                                        "description": msg,
                                        "file_path": path,
                                        "recommendation": "Fix syntax.",
                                    }
                                )
                if not issues:
                    issues.append(
                        {
                            "description": result.error_summary,
                            "file_path": "",
                            "recommendation": "Fix.",
                        }
                    )
            else:
                test_result = run_pytest(project_dir, python_exe=sys.executable)
                result = test_result
                if not result.success:
                    issues = [
                        {
                            "description": (f.message or f.raw_excerpt or ""),
                            "file_path": (f.file_path or ""),
                            "recommendation": (f.suggestion or f.playbook_hint or "Fix."),
                        }
                        for f in result.parsed_failures("pytest")
                    ]
                    if not issues:
                        issues.append(
                            {
                                "description": result.pytest_error_summary(),
                                "file_path": "",
                                "recommendation": "Fix.",
                            }
                        )

    error_summary = (
        result.error_summary
        if hasattr(result, "error_summary")
        else "Build still failing after fix attempts"
    )
    logger.error(
        "[%s] Build fix exhausted. Recovery summary: attempted %d fix iterations, "
        "each applying LLM-generated patches then re-running build. Final error: %s",
        task_id,
        max_fix_attempts,
        error_summary,
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


def _maybe_ship_sprint_release(
    *,
    sprint_id: Optional[str],
    plan_dir: Path,
    int_result: Any,
    integration_outcome: str,
    job_id: str,
) -> None:
    """Hook into the SE pipeline: ship a release if this is a sprint run.

    Phase 3 of #243 / issue #371. Runs immediately after the Integration
    phase. No-op when ``sprint_id`` is None — preserves the byte-identical
    one-shot path for non-sprint runs.

    ``integration_outcome``:
      * ``"not_run"`` — Integration not applicable (no backend, no
        frontend, or no code). Ship the release; there's nothing to
        gate on.
      * ``"succeeded"`` — Integration ran and returned. Ship the
        release; ``int_result.issues`` is the failure list.
      * ``"failed"`` — Integration was attempted but threw. **Defer the
        release** and open a high-severity ``release-manager-skipped``
        feedback item so the next groom catches the gap. Codex review
        on PR #424: shipping after an Integration outage would mint a
        false release row + skip failure-feedback intake entirely.

    Behavior:
      * Skip silently when not all planned stories have reached a
        terminal status (the sprint is still in flight).
      * Otherwise call ``ReleaseManagerAgent.ship`` with the Integration
        issues list so failures land as ``feedback_items`` tagged with
        the sprint and the release notes go to ``plan/releases/<v>.md``.

    Failure mode (per #371's "Failures are non-fatal" requirement):
      * ``ReleaseManagerAgent.ship`` raising → log + open a high-severity
        ``release-manager-error`` feedback item so the next groom sees
        the gap.
      * Anything else → log only; we never propagate from this hook.
    """
    if not sprint_id:
        return
    try:
        from product_delivery import (  # noqa: PLC0415 — lazy cross-team import
            get_store as _pd_get_store,
        )
        from product_delivery.release_manager_agent import (  # noqa: PLC0415
            ReleaseManagerAgent,
        )

        pd_store = _pd_get_store()
        # Check sprint completion *before* classifying Integration as
        # "failed" so we don't open a release-manager-skipped feedback
        # for a sprint that's still mid-flight (PR #424 Codex P2 round
        # 4: the deferral is already implied by open stories — emitting
        # extra "skipped" alerts pollutes the next groom). Only when
        # the sprint is shippable AND Integration failed do we surface
        # the gap.
        open_count = pd_store.count_open_stories_in_sprint(sprint_id)
        if open_count > 0:
            logger.info(
                "Release manager: sprint %s has %d open story(ies); deferring release.",
                sprint_id,
                open_count,
            )
            return
        if integration_outcome == "failed":
            logger.warning(
                "Release manager: Integration phase failed for sprint %s; deferring "
                "release. Opening release-manager-skipped feedback so the next groom "
                "sees the gap.",
                sprint_id,
            )
            pid = pd_store.get_product_id_for_sprint(sprint_id)
            if pid:
                pd_store.create_feedback_item(
                    product_id=pid,
                    source="release-manager-skipped",
                    raw_payload={
                        "reason": "integration_phase_failed",
                        "job_id": job_id,
                    },
                    severity="high",
                    linked_story_id=None,
                    author="release-manager",
                    sprint_id=sprint_id,
                )
            return
        issues = list(getattr(int_result, "issues", []) or [])
        release = ReleaseManagerAgent(pd_store).ship(
            sprint_id=sprint_id,
            plan_dir=plan_dir,
            integration_issues=issues,
        )
        logger.info(
            "Release manager: shipped %s for sprint %s -> %s (%d integration issue(s) promoted)",
            release.version,
            sprint_id,
            release.notes_path,
            len(issues),
        )
    except Exception as rm_err:
        logger.warning(
            "Release manager hook failed for sprint %s: %s",
            sprint_id,
            rm_err,
            exc_info=True,
        )
        # Best-effort: drop a feedback row so the next groom sees the
        # release-manager outage. Wrapped in its own try/except so a
        # double failure (release manager + feedback intake) still
        # leaves the SE run alone.
        try:
            from product_delivery import (  # noqa: PLC0415
                get_store as _pd_get_store,
            )

            pd_store = _pd_get_store()
            pid = pd_store.get_product_id_for_sprint(sprint_id)
            if pid:
                pd_store.create_feedback_item(
                    product_id=pid,
                    source="release-manager-error",
                    raw_payload={"error": str(rm_err), "job_id": job_id},
                    severity="high",
                    linked_story_id=None,
                    author="release-manager",
                    sprint_id=sprint_id,
                )
        except Exception:
            logger.exception(
                "Release manager hook: failed to record release-manager-error feedback"
            )


def _load_requirements_from_sprint(sprint_id: str) -> Tuple[Any, str]:
    """Synthesize ``(ProductRequirements, spec_markdown)`` from a sprint's stories.

    Phase 2 of #243. Imports are lazy so the SE team doesn't take an
    import-time dependency on product_delivery (the two are sibling
    teams). Raises ``UnknownProductDeliveryEntity`` when the sprint id
    is missing, ``ValueError`` when the sprint has no planned stories
    (we never silently fall back to repo spec parsing — the caller asked
    for a sprint run).
    """
    from product_delivery import (  # noqa: PLC0415 — lazy to avoid cross-team import at module load
        TERMINAL_STORY_STATUSES,
        UnknownProductDeliveryEntity,
        get_store,
    )
    from software_engineering_team.shared.models import ProductRequirements

    sprint_view = get_store().get_sprint_with_stories(sprint_id)
    if sprint_view is None:
        raise UnknownProductDeliveryEntity(f"unknown sprint: {sprint_id}")
    if not sprint_view.stories:
        raise ValueError(
            f"sprint {sprint_id!r} has no planned stories; run "
            "POST /api/product-delivery/sprints/{id}/plan first."
        )
    sprint = sprint_view.sprint
    # Filter terminal-status stories before synthesis so the SE
    # pipeline doesn't re-execute work that's already done /
    # cancelled / closed (Codex review on PR #396). Stories may be
    # marked terminal *after* planning — the planner only excludes
    # them at *selection* time, so without this filter execution and
    # planning would diverge. Uses the same `TERMINAL_STORY_STATUSES`
    # set the planner does, with case-insensitive compare so a row
    # stored as ``Done`` doesn't smuggle past the lowercase set.
    executable_stories = [
        s
        for s in sprint_view.stories
        if (s.status or "").strip().lower() not in TERMINAL_STORY_STATUSES
    ]
    if not executable_stories:
        raise ValueError(
            f"sprint {sprint_id!r} has no executable stories — every planned "
            "story is in a terminal status (done/completed/cancelled/closed)."
        )
    story_ids = [s.id for s in executable_stories]

    # Markdown synthesis: per-story heading + user_story + bulleted ACs.
    # `acceptance_criteria_by_story_id` was populated by
    # `get_sprint_with_stories` inside the same REPEATABLE READ
    # transaction as the story fetch (Codex review on PR #396), so the
    # AC rows we render here are guaranteed consistent with the story
    # rows — no risk of a stale stories + fresh ACs mix from
    # concurrent backlog edits.
    flat_ac_strings: list[str] = []
    sections: list[str] = [f"# Sprint: {sprint.name}", ""]
    if sprint.starts_at or sprint.ends_at:
        window = []
        if sprint.starts_at:
            window.append(f"start={sprint.starts_at.isoformat()}")
        if sprint.ends_at:
            window.append(f"end={sprint.ends_at.isoformat()}")
        sections.append("> " + ", ".join(window))
        sections.append("")
    acs_by_story = sprint_view.acceptance_criteria_by_story_id or {}
    for story in executable_stories:
        sections.append(f"## {story.title}")
        if story.user_story:
            sections.append(f"**User Story:** {story.user_story}")
        ac_rows = acs_by_story.get(story.id, [])
        if ac_rows:
            sections.append("")
            sections.append("**Acceptance criteria:**")
            for ac in ac_rows:
                sections.append(f"- {ac.text}")
                flat_ac_strings.append(ac.text)
        sections.append("")
    spec_markdown = "\n".join(sections).rstrip() + "\n"

    requirements = ProductRequirements(
        title=sprint.name,
        description=spec_markdown,
        acceptance_criteria=flat_ac_strings or ["Deliver according to planned story scope."],
        constraints=[],
        priority="medium",
        metadata={
            "sprint_id": sprint_id,
            "story_ids": story_ids,
            "synthesized_from_sprint": True,
        },
    )
    return requirements, spec_markdown


def _parse_iso(value: Any) -> Optional[datetime]:
    """Parse an ISO-8601 string to an aware datetime, or None when absent/invalid."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _emit_coding_team_metrics(job_id: str) -> None:
    """Emit DORA lifecycle events from the coding-team run's persisted task graph.

    The coding-team path runs entirely inside ``run_coding_team_orchestrator`` and
    persists its task graph to the job record (``task_graph_snapshot``). This reads
    that snapshot and emits, per merged task, a ``task_created`` + ``task_merged``
    pair (lead time = job creation → task merge, with the creation time carried on
    the merge event so it survives a metrics-window boundary), a ``gate_reentry``
    when the task needed revisions (the change-failure signal), one ``merge_to_main``
    for a completed job (deployment frequency), and a final cost flush.

    Preconditions: ``job_id`` is a non-empty string.
    Postconditions: best-effort — never raises; emits nothing when the job or its
        snapshot is unavailable or Postgres is disabled. Idempotent per
        ``(job, task)``: a resumed/re-run job emits only the events it has not
        already recorded, so newly-merged tasks are captured without re-counting
        the ones a prior run logged. Always flushes job cost.
    """
    if not job_id:
        # Empty job_id is a caller contract violation; surfaced as a no-op rather
        # than raised, matching this helper's best-effort/never-raises contract
        # (querying with a blank key would otherwise read the wrong job or None).
        return
    try:
        # Per-task idempotency: emit only the lifecycle events not already recorded
        # for this job. Guarding the whole batch on a single event's existence would
        # drop a resumed run's newly-merged tasks (and the job's merge_to_main), so
        # we fetch the (event_type, task_id) pairs already written and skip just
        # those — capturing new merges without double-counting prior ones.
        emitted = se_events.emitted_event_keys(job_id)
        job = get_job(job_id) or {}
        snapshot = job.get("task_graph_snapshot") or []
        created_ts = _parse_iso(job.get("created_at"))
        created_iso = created_ts.isoformat() if created_ts else None
        job_status = (job.get("status") or "").lower()
        for t in snapshot:
            if not isinstance(t, dict):
                continue
            tid = t.get("id") or ""
            status = (t.get("status") or "").lower()
            if (se_events.TASK_CREATED, tid) not in emitted:
                se_events.record_event(
                    se_events.TASK_CREATED,
                    job_id=job_id,
                    task_id=tid,
                    phase="design",
                    ts=created_ts,
                )
            if status == "merged" and not t.get("resolved_without_changes"):
                merged_ts = _parse_iso(t.get("merged_at"))
                if (se_events.TASK_MERGED, tid) not in emitted:
                    detail = {"created_ts": created_iso} if created_iso else None
                    se_events.record_event(
                        se_events.TASK_MERGED,
                        job_id=job_id,
                        task_id=tid,
                        phase="execution",
                        ts=merged_ts,
                        detail=detail,
                    )
                # A merged task that needed >= 1 revision is a change that initially
                # failed a quality gate — the DORA change-failure signal.
                if (
                    int(t.get("revision_count") or 0) > 0
                    and (
                        se_events.GATE_REENTRY,
                        tid,
                    )
                    not in emitted
                ):
                    se_events.record_event(
                        se_events.GATE_REENTRY,
                        job_id=job_id,
                        task_id=tid,
                        phase="execution",
                        ts=merged_ts,
                    )
        if (
            job_status in ("completed", "completed_with_failures")
            and (
                se_events.MERGE_TO_MAIN,
                "",
            )
            not in emitted
        ):
            se_events.record_event(se_events.MERGE_TO_MAIN, job_id=job_id, phase="integration")
    except Exception:
        logger.debug("failed to emit coding-team DORA metrics for %s", job_id, exc_info=True)
    finally:
        cost_tracker.flush(job_id)


def run_orchestrator(
    job_id: str,
    repo_path: str | Path,
    *,
    spec_content_override: Optional[str] = None,
    resolved_questions_override: Optional[List[Dict[str, Any]]] = None,
    planning_only: bool = False,
    sprint_id: Optional[str] = None,
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
    - sprint_id: when set (#370), pull the planned scope from the
      product_delivery sprint's stories and synthesize requirements
      directly — Discovery's LLM spec-parse and the PRA agent are
      skipped. Mutually exclusive with ``spec_content_override``.
    """
    path = Path(repo_path).resolve()
    try:  # pragma: no cover  # integration-only: end-to-end 4-phase orchestration pipeline (LLM + git + npm/pytest)
        # Check for cancellation at start
        _check_cancellation(job_id)

        update_job(
            job_id,
            status=JOB_STATUS_RUNNING,
            phase="product_analysis",
            status_text="Starting pipeline",
        )

        agents = _get_agents()

        # 1. Read spec from work path or use override (no git required at root)
        from spec_parser import (
            gather_context_files,
            get_newest_spec_content,
            get_newest_spec_path,
            parse_spec_with_llm,
        )

        initial_spec_path = None
        # Sprint path (#370): when sprint_id is set, the synthesized spec
        # comes from the product_delivery sprint's planned stories. Both
        # the LLM spec-parse and the PRA agent are skipped — the spec is
        # already structured (per-story user_story + ACs) and validated
        # by the upstream Sprint Planner.
        if sprint_id is not None:
            from product_delivery import UnknownProductDeliveryEntity  # noqa: PLC0415

            if spec_content_override is not None:
                err = (
                    "run_orchestrator received both sprint_id and spec_content_override; "
                    "they are mutually exclusive."
                )
                logger.error(err)
                update_job(job_id, status=JOB_STATUS_FAILED, error=err, phase="completed")
                return
            try:
                requirements, spec_content = _load_requirements_from_sprint(sprint_id)
            except UnknownProductDeliveryEntity as e:
                logger.error("Sprint %s not found: %s", sprint_id, e)
                update_job(
                    job_id,
                    status=JOB_STATUS_FAILED,
                    error=f"Sprint scope load failed: {e}",
                    phase="completed",
                )
                return
            except ValueError as e:
                logger.error("Sprint %s scope is empty: %s", sprint_id, e)
                update_job(
                    job_id,
                    status=JOB_STATUS_FAILED,
                    error=f"Sprint scope load failed: {e}",
                    phase="completed",
                )
                return
        elif spec_content_override is not None:
            spec_content = spec_content_override
        else:
            initial_spec_path = get_newest_spec_path(path)
            spec_content = get_newest_spec_content(path)

        # Gather all context files from the repo for PRA agent
        context_files = gather_context_files(path)
        if context_files:
            logger.info("Gathered %d context files for PRA agent", len(context_files))

        if sprint_id is None:
            try:
                requirements = parse_spec_with_llm(spec_content, get_client("spec_intake"))
            except LLMRateLimitError:
                logger.warning("Ollama LLM usage limit exceeded for week. Job %s paused.", job_id)
                update_job(job_id, status="paused_llm_limit", error=OLLAMA_WEEKLY_LIMIT_MESSAGE)
                return
            except Exception as e:
                logger.error(
                    "Spec parsing failed (LLM unavailable or returned invalid output): %s", e
                )
                update_job(
                    job_id,
                    status=JOB_STATUS_FAILED,
                    error=f"Spec parsing failed: {e}",
                    phase="completed",
                )
                return
        update_job(
            job_id,
            requirements_title=requirements.title,
            status_text="Specification parsed successfully",
        )

        # Check for cancellation after spec parsing
        _check_cancellation(job_id)

        # Create plan folder after spec is ingested successfully (all planning artifacts go here)
        plan_dir = ensure_plan_dir(path)
        logger.info("Plan folder ensured at %s", plan_dir)

        if sprint_id is not None:
            # Sprint path: the spec is already structured + validated, so
            # PRA's review/communicate/update/cleanup loop has nothing to
            # do. Use the synthesized spec directly as the validated spec
            # for downstream stages.
            validated_spec = spec_content
            logger.info(
                "Sprint %s: skipped Product Requirements Analysis; using synthesized spec",
                sprint_id,
            )
        else:
            # ── Step 1: Product Requirements Analysis Agent ───────────────────────
            # Validates spec, asks user questions, produces validated_spec.md
            from product_requirements_analysis_agent import ProductRequirementsAnalysisAgent

            _pra_job_updater = _make_pra_job_updater(job_id)

            update_job(
                job_id,
                phase="product_analysis",
                message="Starting product requirements analysis...",
                status_text="Starting product requirements analysis",
            )
            logger.info(
                "Next step -> Running Product Requirements Analysis agent to validate spec and gather clarifications"
            )
            pra_agent = ProductRequirementsAnalysisAgent(get_client("product_analysis"))
            pra_result = pra_agent.run_workflow(
                spec_content=spec_content,
                repo_path=path,
                job_id=job_id,
                job_updater=_pra_job_updater,
                context_files=context_files,
                initial_spec_path=initial_spec_path,
            )
            if not pra_result.success:
                err = (
                    pra_result.failure_reason
                    or "Product Requirements Analysis did not complete successfully."
                )
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

        # ── Step 2: Planning Team ──────────────────────────────────────────
        # Receives validated spec, performs planning (intake → discovery → requirements → synthesis → document production)
        update_job(
            job_id,
            phase="planning",
            message="Starting planning workflow...",
            status_text="Starting planning workflow",
        )
        logger.info("Next step -> Running Planning team to generate handoff and context")

        from planning_adapter import PlanningAdapterResult, adapt_planning_result

        from planning_team.orchestrator import run_workflow as run_planning_workflow

        _planning_job_updater = _make_planning_job_updater(job_id)

        def _run_architecture_for_planning(  # pragma: no cover  # integration-only: runs ArchitectureExpert LLM
            spec_content: str,
            prd_content: Optional[str],
            repo_path: str,
            client_context: Optional[Dict[str, Any]],
        ) -> Optional[str]:
            """Produce architecture overview during Planning document production (merged Architecture Expert)."""
            from architecture_expert.models import ArchitectureInput

            from software_engineering_team.shared.models import ProductRequirements

            req_desc = (spec_content or "").strip()
            if (prd_content or "").strip():
                req_desc = (req_desc + "\n\n" + prd_content.strip()).strip()
            if not req_desc:
                req_desc = "See Planning handoff artifacts."
            acceptance = ["Deliver according to spec and planning artifacts."]
            if client_context and client_context.get("success_criteria"):
                acceptance = list(client_context["success_criteria"])
            requirements = ProductRequirements(
                title="Project",
                description=req_desc,
                acceptance_criteria=acceptance,
                constraints=[],
                priority="medium",
                metadata={},
            )
            features_parts = []
            if prd_content:
                features_parts.append(prd_content)
            if client_context:
                if client_context.get("problem_summary"):
                    features_parts.append(
                        "## Problem summary\n" + (client_context["problem_summary"] or "")
                    )
                if client_context.get("opportunity_statement"):
                    features_parts.append(
                        "## Opportunity\n" + (client_context["opportunity_statement"] or "")
                    )
            features_doc = "\n\n".join(features_parts) if features_parts else ""
            goals = ""
            if client_context and (
                client_context.get("problem_summary") or client_context.get("opportunity_statement")
            ):
                goals = (
                    (client_context.get("problem_summary") or "")
                    + "\n"
                    + (client_context.get("opportunity_statement") or "")
                )
            project_overview = {
                "features_and_functionality_doc": features_doc,
                "goals": goals.strip(),
            }
            arch_agent = agents["architecture"]
            arch_input = ArchitectureInput(
                requirements=requirements,
                technology_preferences=["Python", "FastAPI", "PostgreSQL", "Docker"],
                project_overview=project_overview,
                features_and_functionality_doc=features_doc or None,
            )
            try:
                arch_output = arch_agent.run(arch_input)
                return (
                    (arch_output.architecture.overview or "")
                    if arch_output and arch_output.architecture
                    else None
                )
            except Exception:
                return None

        planning_result = run_planning_workflow(
            repo_path=str(path),
            spec_content=validated_spec,
            use_product_analysis=False,
            llm=get_client("project_planning"),
            job_updater=_planning_job_updater,
            run_architecture_fn=_run_architecture_for_planning,
            # Never let Planning silently auto-decide a clarification question on this path:
            # escalate to the user, and fail closed if escalation is somehow unavailable.
            answer_callback=_build_planning_answer_callback(job_id),
            auto_answer_questions=False,
        )
        if not planning_result.get("success"):
            err = (
                planning_result.get("failure_reason")
                or "Planning workflow did not complete successfully."
            )
            logger.error("Planning failed: %s", err)
            update_job(job_id, status=JOB_STATUS_FAILED, error=err, phase="completed")
            return

        try:
            adapter_result: PlanningAdapterResult = adapt_planning_result(
                planning_result, spec_title=requirements.title, repo_path=str(path)
            )
        except ValueError as e:
            logger.error("Planning adapter failed: %s", e)
            update_job(job_id, status=JOB_STATUS_FAILED, error=str(e), phase="completed")
            return

        adapter_result.shared_planning_doc_path = str(
            Path(path) / "plan" / "planning_team" / "planning_document.md"
        )
        requirements = adapter_result.requirements
        update_job(job_id, requirements_title=requirements.title)

        # Check for cancellation after Planning
        _check_cancellation(job_id)

        # Human-in-the-loop decision gate (default path). If planning surfaced open questions that
        # the user has not already answered, pause the job and block until they do — an open
        # question must never reach implementation un-answered. The answers thread into the coding
        # team via resolved_questions_override; the open questions are then cleared so the
        # coding-team gate does not re-pause on them.
        if adapter_result.open_questions and not resolved_questions_override:
            resolved, ok = _run_se_decision_gate(
                job_id, list(adapter_result.open_questions), source="planning"
            )
            if not ok:
                return
            resolved_questions_override = resolved
            adapter_result.open_questions = []

        # planning_only: spec intake + planning (+ any decision gate) are done;
        # stop before execution per the documented contract. The Temporal path
        # honors this independently; the thread path must too.
        if planning_only:
            logger.info("Planning-only run: stopping before execution (job %s)", job_id)
            update_job(job_id, status=JOB_STATUS_COMPLETED, phase="completed")
            return

        # Execution: delegate to the coding_team orchestrator (it replaces the former
        # Tech Lead + Architecture Expert + backend/frontend code-v2 worker pipeline).
        existing_code_summary = _truncate_for_context(_read_repo_code(path), 8000)
        if existing_code_summary == "# No code files found":
            existing_code_summary = None
        plan_input = _build_coding_team_plan_input(
            adapter_result, str(path), existing_code_summary, resolved_questions_override
        )
        from coding_team.orchestrator import run_coding_team_orchestrator
        from software_engineering_team.coding_engine_provider import SECodeEngineProvider

        base, span = PROGRESS_BAND_CODING
        # get_llm deliberately NOT passed: the coding team's default getter wraps
        # the LLM clients with reasoning-stream capture, whose periodic flush is
        # the only thing that refreshes job activity DURING a multi-minute LLM
        # call — passing the raw get_client here made every long implement call
        # look like a stall to the UI's activity-based warning.
        #
        # Bind team/job_id attribution around the whole coding run so every LLM
        # call it makes (sequential + via strands' asyncio.to_thread, which
        # copies the context) is attributed to this SE job — that is what the
        # cost tracker keys on. Without this the live path records no job cost.
        with llm_attribution(team="software_engineering", job_id=job_id, phase="execution"):
            run_coding_team_orchestrator(
                job_id,
                str(path),
                plan_input,
                update_job_fn=lambda **kw: update_job(job_id, **kw),
                get_job_fn=lambda jid: get_job(jid),
                progress_base=base,
                progress_span=span,
                engine_provider=SECodeEngineProvider(),
            )
        # Emit DORA lifecycle events (deployment/lead-time/change-failure) from
        # the coding-team task graph the run persisted, and flush final cost.
        _emit_coding_team_metrics(job_id)
        # run_coding_team_orchestrator owns its terminal status on every exit path (completed /
        # completed_with_failures / already_complete / failed / cancelled), so there is nothing
        # to finalize here — writing COMPLETED would clobber a failure, a partial-success, or an
        # already-complete result it already set. ``already_complete`` (the work was already
        # done — no changes needed) is a terminal success and is left intact.
        return

    except (
        CancellationError
    ):  # pragma: no cover  # integration-only: paired with integration-only try block
        logger.info("Orchestrator stopped due to job cancellation: %s", job_id)
        update_job(
            job_id,
            status=JOB_STATUS_CANCELLED,
            status_text="Job cancelled by user",
            phase="completed",
        )
    except (
        Exception
    ) as e:  # pragma: no cover  # integration-only: paired with integration-only try block
        logger.exception("Orchestrator failed")
        update_job(job_id, status=JOB_STATUS_FAILED, error=str(e), phase="completed")


def run_failed_tasks(job_id: str) -> None:
    """Retry the FAILED tasks of a prior coding-team run.

    Resumes the run's persisted task graph (``task_graph_snapshot``) with FAILED tasks demoted to
    TO_DO, then delegates to ``run_coding_team_orchestrator`` — the same engine the main run uses,
    with ``retry_failed=True`` the only difference. The coding-team orchestrator owns the terminal
    job status on every success/partial/failure exit path (mirroring the main run), so this does not
    finalize a success status itself; it only maps cancellation / unexpected errors to a terminal
    status the way the main run does.

    Runs in a background thread (same pattern as ``run_orchestrator``).

    Preconditions:
        - ``job_id`` refers to a stored job whose record carries ``repo_path`` and a
          ``task_graph_snapshot`` (produced by a prior coding-team run). A job that never ran the
          coding team — no snapshot — cannot be resumed and raises ``ValueError``.
    Postconditions:
        - The coding-team orchestrator has run to a terminal status for ``job_id`` (or a terminal
          cancelled/failed status was written on interruption).
    """
    from software_engineering_team.shared.job_store import get_job

    job_data = get_job(job_id)
    if not job_data:
        raise ValueError(f"Job {job_id} not found")
    repo_path = job_data.get("repo_path")
    if not repo_path:
        raise ValueError(f"Job {job_id} has no repo_path")
    if not (job_data.get("task_graph_snapshot") or []):
        # The coding team persists a task-graph snapshot every round; its absence means this job
        # never ran the coding team (or predates it) and so has no failed task graph to resume.
        raise ValueError(f"Job {job_id} has no task graph snapshot to retry")

    path = Path(repo_path).resolve()
    logger.info("=== Retrying failed tasks for job %s (repo %s) ===", job_id, path)

    from coding_team.models import CodingTeamPlanInput
    from coding_team.orchestrator import run_coding_team_orchestrator
    from software_engineering_team.coding_engine_provider import SECodeEngineProvider

    # On the snapshot-resume path plan_input is barely used (repo_path + any HITL question folding);
    # a PlanningAdapterResult is not available on retry, so build a minimal input from the stored
    # job record. resolved_questions is folded so a prior decision-gate answer is not re-asked.
    plan_input = CodingTeamPlanInput(
        repo_path=str(path),
        requirements_title=job_data.get("requirements_title") or "Project",
        architecture_overview=(
            job_data.get("architecture_overview") or job_data.get("_architecture_overview")
        ),
        resolved_questions=job_data.get("resolved_questions") or [],
    )

    base, span = PROGRESS_BAND_CODING
    # current_activity from the failed run is stale by definition here; clear it so the retry does
    # not render the old run's frozen sub-bar. Clear failed_tasks too: the coding-team run only
    # writes task_graph_snapshot, never failed_tasks, so the persisted list the status endpoint and
    # retry gate read (api/routes/jobs.py) would otherwise keep reporting the pre-retry failures.
    update_job(
        job_id, status=JOB_STATUS_RUNNING, failed_tasks=[], error=None, current_activity=None
    )
    try:
        # Bind team/job_id attribution around the whole coding run so every LLM call it makes is
        # attributed to this SE job — that is what the cost tracker keys on (see the main run).
        with llm_attribution(team="software_engineering", job_id=job_id, phase="execution"):
            run_coding_team_orchestrator(
                job_id,
                str(path),
                plan_input,
                update_job_fn=lambda **kw: update_job(job_id, **kw),
                get_job_fn=lambda jid: get_job(jid),
                progress_base=base,
                progress_span=span,
                engine_provider=SECodeEngineProvider(),
                retry_failed=True,
            )
        # Emit DORA lifecycle events from the persisted task graph and flush final cost.
        _emit_coding_team_metrics(job_id)
        # run_coding_team_orchestrator owns its terminal status on every exit path (completed /
        # completed_with_failures / already_complete / failed / cancelled), so there is nothing to
        # finalize here.
    except CancellationError:
        logger.info("Retry orchestrator stopped due to job cancellation: %s", job_id)
        update_job(
            job_id,
            status=JOB_STATUS_CANCELLED,
            status_text="Job cancelled by user",
            phase="completed",
        )
    except Exception as e:
        logger.exception("Retry orchestrator failed")
        update_job(job_id, status=JOB_STATUS_FAILED, error=str(e), phase="completed")
