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
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import httpx

_team_dir = Path(__file__).resolve().parent
if str(_team_dir) not in sys.path:
    sys.path.insert(0, str(_team_dir))
_arch_dir = _team_dir / "architect-agents"
if _arch_dir.exists() and str(_arch_dir) not in sys.path:
    sys.path.insert(0, str(_arch_dir))

from strands import Agent  # noqa: E402

from llm_service import (  # noqa: E402
    OLLAMA_WEEKLY_LIMIT_MESSAGE,
    LLMError,
    LLMJsonParseError,
    LLMPermanentError,
    LLMRateLimitError,
    LLMTemporaryError,
    get_client,
    get_strands_model,
    llm_attribution,
)
from software_engineering_team.shared import (  # noqa: E402
    cost_tracker,
    se_events,
)
from software_engineering_team.shared.execution_tracker import execution_tracker  # noqa: E402
from software_engineering_team.shared.git_utils import (  # noqa: E402
    DEVELOPMENT_BRANCH,
    checkout_branch,
)
from software_engineering_team.shared.job_store import (  # noqa: E402
    JOB_STATUS_AGENT_CRASH,
    JOB_STATUS_CANCELLED,
    JOB_STATUS_COMPLETED,
    JOB_STATUS_FAILED,
    JOB_STATUS_PAUSED_LLM_CONNECTIVITY,
    JOB_STATUS_RUNNING,
    LLM_SEMANTIC_EXHAUSTION,
    LLM_UNREACHABLE_AFTER_RETRIES,
    add_pending_questions,
    get_job,
    is_cancel_requested,
    is_waiting_for_answers,
    update_job,
    update_job_team_progress,
    update_task_state,
)
from software_engineering_team.shared.models import TaskUpdate  # noqa: E402
from software_engineering_team.shared.plan_dir import ensure_plan_dir  # noqa: E402
from software_engineering_team.shared.repo_utils import (  # noqa: E402
    read_repo_code,
    truncate_for_context,
)
from software_engineering_team.shared.task_utils import task_requirements  # noqa: E402

try:
    from unified_api.slack_notifier import notify_open_questions as slack_notify_open_questions
except ImportError:
    slack_notify_open_questions = None

logger = logging.getLogger(__name__)


def _iso_now() -> str:
    """Return current UTC time as ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


# The SE job owns the allocation of its progress bar across phases. Sub-agents
# (PRA, Planning V3, coding team) each report their OWN 0-100 progress; the job
# updaters rescale those onto the phase's band so the bar is monotone across the
# whole run instead of repeatedly sprinting to 100 and collapsing at each handoff.
PROGRESS_BAND_PRODUCT_ANALYSIS = (0, 15)
PROGRESS_BAND_PLANNING = (15, 15)
PROGRESS_BAND_CODING = (30, 65)


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
PLANNING_V3_PHASE_ORDER = [
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


def _make_planning_v3_job_updater(job_id: str) -> Callable[..., None]:
    """Build the job updater handed to the Planning V3 workflow.

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
                for p in PLANNING_V3_PHASE_ORDER:
                    if p == planning_phase:
                        break
                    completed_phases.append(p)
                kwargs["planning_completed_phases"] = completed_phases
            # Planning V3 reports its own 0-100 progress; rescale onto this
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


def _build_planning_v3_answer_callback(job_id: str) -> Callable[[list], list]:
    """Build an escalating answer callback for Planning V3 PRA — surface questions, never auto-decide.

    When Planning V3's product-analysis phase asks clarification questions, this pauses the SE job
    and routes them to the user (instead of Planning V3 auto-selecting a default). It preserves each
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
    Main pipeline uses planning_v3_team for planning; spec_intake/project_planning/domain planning agents
    are not used in the main flow (clarification_store may still use Spec Intake elsewhere)."""
    from acceptance_verifier_agent import AcceptanceVerifierAgent
    from accessibility_agent import AccessibilityExpertAgent
    from architecture_expert import ArchitectureExpertAgent
    from build_fix_specialist import BuildFixSpecialistAgent
    from code_review_agent import CodeReviewAgent
    from devops_team import DevOpsTeamLeadAgent
    from frontend_team_deprecated.feature_agent import FrontendExpertAgent
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
        "frontend": FrontendExpertAgent(get_client("frontend")),
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


def _issues_to_dicts(
    qa_bugs: Any, sec_vulns: Any
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Convert QA/Security outputs to dict lists for coding agent input."""
    qa_list = [b.model_dump() if hasattr(b, "model_dump") else b.dict() for b in (qa_bugs or [])]
    sec_list = [v.model_dump() if hasattr(v, "model_dump") else v.dict() for v in (sec_vulns or [])]
    return qa_list, sec_list


# _read_repo_code and _truncate_for_context are now in shared.repo_utils
_read_repo_code = read_repo_code
_truncate_for_context = truncate_for_context


def _build_coding_team_plan_input(
    adapter_result: Any,
    repo_path: str,
    existing_code_summary: Optional[str] = None,
    resolved_questions: Optional[List[Dict[str, Any]]] = None,
) -> Any:
    """Build CodingTeamPlanInput from PlanningV2AdapterResult for coding_team orchestrator."""
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


def _build_task_update(
    task_id: str, agent_type: str, result: Any, status: str = "completed"
) -> TaskUpdate:
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

    try:  # pragma: no cover  # integration-only: DbC agent runs live LLM + writes commits
        dbc_code = _read_repo_code(repo_path)
        if not dbc_code or dbc_code == "# No code files found":
            logger.info("[%s] DbC: no code files to review, skipping", task_id)
            return

        dbc_result = agents["dbc_comments"].run(
            DbcCommentsInput(
                code=dbc_code,
                language=language,
                task_description=task_description,
                architecture=architecture,
            )
        )

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
    except (
        Exception
    ) as e:  # pragma: no cover  # integration-only: paired with integration-only try block
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


def _code_review_issues_to_dicts(issues: Any) -> List[Dict[str, Any]]:
    """Convert CodeReviewIssue objects to dicts for coding agent input."""
    return [i.model_dump() if hasattr(i, "model_dump") else i.dict() for i in (issues or [])]


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
            task_id,
            i,
            issue.severity,
            issue.category,
            issue.description,
            issue.file_path or "n/a",
        )
        if issue.suggestion:
            logger.warning(
                "[%s]     Suggestion: %s",
                task_id,
                issue.suggestion[:300],
            )
    if review_result.summary:
        logger.info("[%s]   Review summary: %s", task_id, review_result.summary[:300])
    if review_result.spec_compliance_notes:
        logger.info(
            "[%s]   Spec compliance: %s", task_id, review_result.spec_compliance_notes[:300]
        )
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
    from software_engineering_team.shared.command_runner import (
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
        from software_engineering_team.shared.command_runner import is_ng_build_environment_failure

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
                from software_engineering_team.shared.error_parsing import (
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
                "Build verification failed for task %s: %s", task_id, result.error_summary[:200]
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
            logger.warning(
                "Syntax check failed for task %s: %s", task_id, result.error_summary[:200]
            )
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
                            pip_result.error_summary[:200],
                        )
                except Exception as e:
                    logger.warning("pip install before pytest failed (non-fatal): %s", e)
            test_result = run_pytest(backend_dir, python_exe=sys.executable)
            if not test_result.success:
                failures = test_result.parsed_failures("pytest")
                if failures:
                    from software_engineering_team.shared.error_parsing import (
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
                from backend_agent.agent import EXCEPTION_HANDLER_TEST_PATTERNS

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
                    logger.warning(
                        "Docker build failed for task %s: %s", task_id, result.error_summary[:200]
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
    from software_engineering_team.shared.command_runner import (
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
            from software_engineering_team.shared.command_runner import (
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
                    "description": (f.message or f.raw_excerpt or "")[:500],
                    "file_path": (f.file_path or "")[:300],
                    "recommendation": (f.suggestion or f.playbook_hint or "Fix the build error.")[
                        :500
                    ],
                }
            )
        if not issues:
            issues.append(
                {
                    "description": result.error_summary[:500],
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
                                "description": msg[:500],
                                "file_path": path[:300],
                                "recommendation": "Fix the syntax error in this file.",
                            }
                        )
            if not issues:
                issues.append(
                    {
                        "description": result.error_summary[:500],
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
                                "description": (f.message or f.raw_excerpt or "")[:500],
                                "file_path": (f.file_path or "")[:300],
                                "recommendation": (
                                    f.suggestion
                                    or f.playbook_hint
                                    or "Fix the test or implementation."
                                )[:500],
                            }
                        )
                    if not issues:
                        issues.append(
                            {
                                "description": test_result.pytest_error_summary()[:500],
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
                    "description": (f.message or f.raw_excerpt or "")[:500],
                    "file_path": (f.file_path or "")[:300],
                    "recommendation": (f.suggestion or f.playbook_hint or "Fix.")[:500],
                }
                for f in failures
            ]
            if not issues:
                issues.append(
                    {
                        "description": result.error_summary[:500],
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
                                        "description": msg[:500],
                                        "file_path": path[:300],
                                        "recommendation": "Fix syntax.",
                                    }
                                )
                if not issues:
                    issues.append(
                        {
                            "description": result.error_summary[:500],
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
                            "description": (f.message or f.raw_excerpt or "")[:500],
                            "file_path": (f.file_path or "")[:300],
                            "recommendation": (f.suggestion or f.playbook_hint or "Fix.")[:500],
                        }
                        for f in result.parsed_failures("pytest")
                    ]
                    if not issues:
                        issues.append(
                            {
                                "description": result.pytest_error_summary()[:500],
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
        error_summary[:200],
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


def _code_v2_worker(
    *,
    job_id: str,
    queue: List[str],
    all_tasks: Dict[str, Any],
    completed: set,
    failed: Dict[str, str],
    completed_code_task_ids: List[str],
    architecture: Any,
    agents: Dict[str, Any],
    repo_path: Path,
    agents_key: str,
    team_label: str,
    label: str,
    default_assignee: str,
    forward_tech_lead: bool,
    surface_rate_limit: bool,
) -> None:
    """Drain ``queue`` by calling a code-v2 team-lead's ``run_workflow``, one task at a time.

    The backend (``backend_code_v2_team``) and frontend (``frontend_code_v2_team``) workers
    are identical except for which team lead they invoke, their progress/log labels, and two
    backend-only knobs — hence the parametrization. Designed to run in its own thread,
    parallel with the sibling worker.

    Preconditions:
        - ``agents_key`` indexes the team lead in ``agents`` (``None`` ⇒ every queued task is
          failed with "``label`` team not registered" and the worker returns immediately).
        - ``forward_tech_lead`` forwards ``tech_lead``/``build_fix_specialist`` to
          ``run_workflow`` (backend only); ``surface_rate_limit`` maps an
          ``LLMRateLimitError`` to ``OLLAMA_WEEKLY_LIMIT_MESSAGE`` (backend only).
    Postconditions:
        - Each drained task ends in exactly one of ``completed`` (and
          ``completed_code_task_ids``) or ``failed``; ``queue`` is left empty on normal exit.
        - A ``TASK_MERGED`` event is recorded for every successful task.
    """
    from software_engineering_team.shared.models import SystemArchitecture

    team_lead = agents.get(agents_key)
    if team_lead is None:
        for tid in queue:
            failed[tid] = f"{label} team not registered"
        return

    while queue:  # pragma: no cover  # integration-only: drains queue by calling code-v2 run_workflow
        # Check for cancellation before starting each task
        if is_cancel_requested(job_id):
            logger.info("%s worker: cancellation detected, stopping", label)
            return

        task_id = queue.pop(0)
        task = all_tasks.get(task_id)
        if not task:
            continue

        update_job(job_id, current_task=task_id)
        update_task_state(job_id, task_id, status="in_progress", started_at=_iso_now())
        update_job_team_progress(job_id, team_label, current_task_id=task_id)
        logger.info("[%s] >>> %s worker starting task", task_id, label)
        task_start = time.monotonic()

        def _job_updater(**kwargs: Any) -> None:
            update_job_team_progress(job_id, team_label, **kwargs)

        try:
            arch = (
                architecture
                if isinstance(architecture, SystemArchitecture)
                else (SystemArchitecture(overview=str(architecture)) if architecture else None)
            )
            workflow_kwargs: Dict[str, Any] = dict(
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
            if forward_tech_lead:
                workflow_kwargs["tech_lead"] = agents.get("tech_lead")
                workflow_kwargs["build_fix_specialist"] = agents.get("build_fix_specialist")
            # Attribute every LLM call this task makes to the job/task/phase so
            # telemetry spans and per-job cost accounting can slice by them.
            with llm_attribution(
                team="software_engineering",
                job_id=job_id,
                task_id=task_id,
                phase="execution",
            ):
                result = team_lead.run_workflow(**workflow_kwargs)
            elapsed = time.monotonic() - task_start
            if result.success:
                completed.add(task_id)
                completed_code_task_ids.append(task_id)
                update_task_state(job_id, task_id, status="done", finished_at=_iso_now())
                se_events.record_event(
                    se_events.TASK_MERGED, job_id=job_id, task_id=task_id, phase="execution"
                )
                _log_task_completion_banner(
                    task_id=task_id,
                    task_title=getattr(task, "title", "") or task_id,
                    assignee=getattr(task, "assignee", default_assignee),
                    elapsed_seconds=elapsed,
                    description=getattr(task, "description", "") or "",
                )
            else:
                reason = result.failure_reason or f"{label} workflow did not succeed"
                failed[task_id] = reason
                update_task_state(
                    job_id, task_id, status="failed", finished_at=_iso_now(), error=reason
                )
                logger.warning("[%s] %s task failed: %s", task_id, label, reason)
        except Exception as exc:
            if surface_rate_limit and isinstance(exc, LLMRateLimitError):
                failed[task_id] = OLLAMA_WEEKLY_LIMIT_MESSAGE
                update_task_state(
                    job_id,
                    task_id,
                    status="failed",
                    finished_at=_iso_now(),
                    error=OLLAMA_WEEKLY_LIMIT_MESSAGE,
                )
                logger.warning("[%s] LLM rate limit exceeded in %s worker: %s", task_id, label, exc)
            else:
                failed[task_id] = f"{label} exception: {exc}"
                update_task_state(
                    job_id, task_id, status="failed", finished_at=_iso_now(), error=str(exc)
                )
                logger.exception("[%s] %s worker exception", task_id, label)


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
    """Drain ``backend_code_v2_queue`` via ``backend_code_v2_team.run_workflow`` (see ``_code_v2_worker``)."""
    _code_v2_worker(
        job_id=job_id,
        queue=backend_code_v2_queue,
        all_tasks=all_tasks,
        completed=completed,
        failed=failed,
        completed_code_task_ids=completed_code_task_ids,
        architecture=architecture,
        agents=agents,
        repo_path=repo_path,
        agents_key="backend",
        team_label="backend-code-v2",
        label="backend",
        default_assignee="backend",
        forward_tech_lead=True,
        surface_rate_limit=True,
    )


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
    """Drain ``frontend_code_v2_queue`` via ``frontend_code_v2_team.run_workflow`` (see ``_code_v2_worker``)."""
    _code_v2_worker(
        job_id=job_id,
        queue=frontend_code_v2_queue,
        all_tasks=all_tasks,
        completed=completed,
        failed=failed,
        completed_code_task_ids=completed_code_task_ids,
        architecture=architecture,
        agents=agents,
        repo_path=repo_path,
        agents_key="frontend_code_v2",
        team_label="frontend-code-v2",
        label="frontend_code_v2",
        default_assignee="frontend-code-v2",
        forward_tech_lead=False,
        surface_rate_limit=False,
    )


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
    # Tasks that crashed and are awaiting a successful retry (MTTR). Seeded from
    # durable se_events so a crash detected in a prior run still resolves (and pairs
    # for MTTR) when this resumed/retry run succeeds.
    crashed_tasks: set[str] = se_events.unresolved_crashed_task_ids(job_id)
    agent_source_path = Path(__file__).resolve().parent  # software_engineering_team/

    def _remaining_queue_ids() -> List[str]:
        with state_lock:
            return list(backend_queue) + list(frontend_queue)

    DEP_WAIT_SLEEP = 0.5  # seconds to wait when no runnable task (dependencies pending)

    def _backend_worker() -> None:
        while True:  # pragma: no cover  # integration-only: legacy parallel-worker loop, LLM + git + subprocess
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
                from software_engineering_team.shared.command_runner import (
                    ensure_backend_project_initialized,
                )

                init_result = ensure_backend_project_initialized(backend_dir)
                if not init_result.success:
                    update_task_state(
                        job_id,
                        task_id,
                        status="failed",
                        finished_at=_iso_now(),
                        error=init_result.error_summary,
                    )
                    with state_lock:
                        failed[task_id] = f"Backend init failed: {init_result.error_summary}"
                    continue
                if not (backend_dir / ".git").exists():
                    gs_result = agents["git_setup"].run(backend_dir)
                    if not gs_result.success:
                        update_task_state(
                            job_id,
                            task_id,
                            status="failed",
                            finished_at=_iso_now(),
                            error=gs_result.message,
                        )
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
                _refine_contract = False
                crash_resolved_now = False
                with state_lock:
                    if workflow_result.success:
                        completed.add(task_id)
                        completed_code_task_ids.append(task_id)
                        update_task_state(job_id, task_id, status="done", finished_at=_iso_now())
                        # MTTR: a previously-crashed task is only *resolved* once it
                        # actually succeeds on retry (not when a fix was merely applied).
                        # The event is emitted after the lock is released (below).
                        if task_id in crashed_tasks:
                            crashed_tasks.discard(task_id)
                            crash_resolved_now = True
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
                        _contract_refineable = (
                            failure_reason.startswith("Task contract is incomplete.")
                            and agents.get("project_planning") is not None
                            and task_id not in repaired_tasks
                        )
                        if _contract_refineable:
                            _refine_contract = True
                            logger.info(
                                "%s[%s] Task contract incomplete – attempting refinement",
                                log_prefix,
                                task_id,
                            )
                        else:
                            failed[task_id] = failure_reason
                            update_task_state(
                                job_id,
                                task_id,
                                status="failed",
                                finished_at=_iso_now(),
                                error=failure_reason,
                            )
                            execution_tracker.observe_loop(task_id, 1)
                            execution_tracker.finish_task(task_id, blocked=True)
                            logger.warning(
                                "%s[%s] Backend FAILED after %.1fs: %s",
                                log_prefix,
                                task_id,
                                elapsed,
                                failed[task_id],
                            )
                # Emit outside the lock: keep the blocking Postgres write off the
                # critical section the other worker contends on.
                if crash_resolved_now:
                    se_events.record_event(
                        se_events.CRASH_RESOLVED, job_id=job_id, task_id=task_id, phase="execution"
                    )
                if _refine_contract:
                    try:
                        project_planning_agent = agents.get("project_planning")
                        planning_output = project_planning_agent.run(spec_content=spec_content)
                        nf_reqs = []
                        if hasattr(planning_output, "overview") and hasattr(
                            planning_output.overview, "non_functional_requirements"
                        ):
                            nf_reqs = planning_output.overview.non_functional_requirements or []
                        refined = tech_lead.refine_task(
                            task=task, clarification_requests=[], spec_content=spec_content
                        )
                        contract_metadata = {
                            "goal": getattr(refined, "description", None)
                            or getattr(task, "description", ""),
                            "scope": getattr(refined, "description", None)
                            or getattr(task, "description", ""),
                            "constraints": nf_reqs,
                            "non_functional_requirements": nf_reqs,
                            "inputs_outputs": getattr(refined, "requirements", None)
                            or getattr(task, "requirements", "")
                            or "",
                        }
                        existing_metadata = getattr(refined, "metadata", None) or {}
                        updated_task = refined.model_copy(
                            update={"metadata": {**existing_metadata, **contract_metadata}}
                        )
                        with state_lock:
                            all_tasks[task_id] = updated_task
                            repaired_tasks.add(task_id)
                            backend_queue.append(task_id)
                        update_job(job_id, status=JOB_STATUS_RUNNING, error=None)
                        logger.info(
                            "%s[%s] Task contract refined. Re-queuing task.", log_prefix, task_id
                        )
                    except Exception as refine_err:
                        logger.warning(
                            "%s[%s] Contract refinement failed: %s", log_prefix, task_id, refine_err
                        )
                        update_task_state(
                            job_id,
                            task_id,
                            status="failed",
                            finished_at=_iso_now(),
                            error=failure_reason,
                        )
                        with state_lock:
                            failed[task_id] = failure_reason
            except (LLMError, httpx.HTTPError) as e:
                err_msg = (
                    OLLAMA_WEEKLY_LIMIT_MESSAGE
                    if isinstance(e, LLMRateLimitError)
                    else "LLM rate limited or temporarily unavailable – please retry later"
                    if isinstance(e, LLMTemporaryError)
                    else str(e)
                    if isinstance(e, LLMPermanentError)
                    else f"LLM error: {e}"
                )
                update_task_state(
                    job_id, task_id, status="failed", finished_at=_iso_now(), error=err_msg
                )
                with state_lock:
                    if isinstance(e, LLMRateLimitError):
                        llm_limit_exceeded[0] = True
                        failed[task_id] = OLLAMA_WEEKLY_LIMIT_MESSAGE
                    elif isinstance(e, LLMTemporaryError):
                        failed[task_id] = (
                            "LLM rate limited or temporarily unavailable – please retry later"
                        )
                    elif isinstance(e, LLMPermanentError):
                        failed[task_id] = str(e)
                    else:
                        failed[task_id] = f"LLM error: {e}"
                if isinstance(e, LLMRateLimitError):
                    logger.warning(
                        "Ollama LLM usage limit exceeded for week. Job %s paused.", job_id
                    )
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
                update_job(
                    job_id,
                    status=JOB_STATUS_AGENT_CRASH,
                    error=str(e),
                    agent_crash_details=agent_crash_details,
                )
                se_events.record_event(
                    se_events.CRASH_DETECTED,
                    job_id=job_id,
                    task_id=task_id,
                    phase="execution",
                    detail={"exception_type": type(e).__name__},
                )
                # resolved only when the retry actually succeeds; mutate under the
                # same lock the resolve path uses, so the shared set stays consistent.
                with state_lock:
                    crashed_tasks.add(task_id)
                repair_applied = False
                if type(e) in REPAIRABLE_EXCEPTIONS and task_id not in repaired_tasks:
                    logger.info(
                        "%s[%s] Exception is repairable (%s). Next step -> Attempting automatic repair",
                        log_prefix,
                        task_id,
                        type(e).__name__,
                    )
                    repair_agent = agents.get("repair")
                    if repair_agent:
                        try:
                            from agent_repair_team.models import RepairInput

                            result = repair_agent.run(
                                RepairInput(
                                    traceback=traceback.format_exc(),
                                    exception_type=type(e).__name__,
                                    exception_message=str(e),
                                    task_id=task_id,
                                    agent_type="backend",
                                    agent_source_path=agent_source_path,
                                )
                            )
                            if result.suggested_fixes and _apply_repair_fixes(
                                agent_source_path, result.suggested_fixes
                            ):
                                repair_applied = True
                                with state_lock:
                                    repaired_tasks.add(task_id)
                                    backend_queue.append(task_id)
                                update_job(
                                    job_id,
                                    status=JOB_STATUS_RUNNING,
                                    error=None,
                                    agent_crash_details=None,
                                )
                                logger.info(
                                    "%s[%s] Repair applied. Next step -> Re-queuing task for retry",
                                    log_prefix,
                                    task_id,
                                )
                        except Exception as repair_err:
                            logger.warning(
                                "%s[%s] Repair agent failed: %s", log_prefix, task_id, repair_err
                            )
                if not repair_applied:
                    update_task_state(
                        job_id, task_id, status="failed", finished_at=_iso_now(), error=str(e)
                    )
                    with state_lock:
                        failed[task_id] = f"Unhandled exception: {e}"
                    logger.error(
                        "%s[%s] Backend task failed. Recovery summary: 1) Workflow execution failed, "
                        "2) Repair %s. Final error: %s",
                        log_prefix,
                        task_id,
                        "attempted but unsuccessful"
                        if type(e) in REPAIRABLE_EXCEPTIONS
                        else "not applicable (non-repairable exception)",
                        e,
                    )
                logger.exception("%s[%s] Backend task exception", log_prefix, task_id)
            logger.info("%s[%s] <<< Backend worker done", log_prefix, task_id)

        # After backend agent is done with all tasks for this repo, containerize it
        devops_agent = agents.get("devops")
        if devops_agent and backend_dir.is_dir() and (backend_dir / ".git").exists():
            existing_pipeline = _read_repo_code(backend_dir, [".yml", ".yaml"])
            tech_lead.trigger_devops_for_backend(
                devops_agent,
                backend_dir,
                architecture,
                spec_content,
                existing_pipeline=existing_pipeline
                if existing_pipeline != "# No code files found"
                else None,
                build_verifier=_run_build_verification,
            )

    def _frontend_worker() -> None:
        while True:  # pragma: no cover  # integration-only: legacy parallel-worker loop, LLM + ng build + subprocess
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
                from software_engineering_team.shared.command_runner import (
                    ensure_frontend_project_initialized,
                )
                from software_engineering_team.shared.frontend_framework import (
                    resolve_frontend_framework,
                )

                # Detect framework from task metadata, project files, or spec
                task_meta = getattr(task, "metadata", None) or {}
                detected_framework = resolve_frontend_framework(
                    task_meta, spec_content, frontend_dir
                )
                init_result = ensure_frontend_project_initialized(
                    frontend_dir, framework=detected_framework
                )
                if not init_result.success:
                    update_task_state(
                        job_id,
                        task_id,
                        status="failed",
                        finished_at=_iso_now(),
                        error=init_result.error_summary,
                    )
                    with state_lock:
                        failed[task_id] = f"Frontend init failed: {init_result.error_summary}"
                    continue
                if not (frontend_dir / ".git").exists():
                    gs_result = agents["git_setup"].run(frontend_dir)
                    if not gs_result.success:
                        update_task_state(
                            job_id,
                            task_id,
                            status="failed",
                            finished_at=_iso_now(),
                            error=gs_result.message,
                        )
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
                crash_resolved_now = False
                with state_lock:
                    if workflow_result.success:
                        completed.add(task_id)
                        completed_code_task_ids.append(task_id)
                        update_task_state(job_id, task_id, status="done", finished_at=_iso_now())
                        if task_id in crashed_tasks:
                            crashed_tasks.discard(task_id)
                            crash_resolved_now = True
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
                        update_task_state(
                            job_id,
                            task_id,
                            status="failed",
                            finished_at=_iso_now(),
                            error=failure_reason,
                        )
                        execution_tracker.observe_loop(task_id, 1)
                        execution_tracker.finish_task(task_id, blocked=True)
                        logger.warning(
                            "%s[%s] Frontend FAILED after %.1fs: %s",
                            log_prefix,
                            task_id,
                            elapsed,
                            failed[task_id],
                        )
                if crash_resolved_now:
                    se_events.record_event(
                        se_events.CRASH_RESOLVED, job_id=job_id, task_id=task_id, phase="execution"
                    )
                logger.info(
                    "%s[%s] <<< Frontend worker done (completed=%s)",
                    log_prefix,
                    task_id,
                    workflow_result.success,
                )
                if getattr(workflow_result, "llm_unreachable", False):
                    # Propagate the workflow's sentinel reason (connectivity vs
                    # semantic exhaustion) instead of clobbering it, so the
                    # operator sees the actual terminal condition on the task.
                    pause_reason = (
                        getattr(workflow_result, "failure_reason", None)
                        or LLM_UNREACHABLE_AFTER_RETRIES
                    )
                    update_task_state(
                        job_id,
                        task_id,
                        status="failed",
                        finished_at=_iso_now(),
                        error=pause_reason,
                    )
                    with state_lock:
                        llm_connectivity_failed[0] = True
                        failed[task_id] = pause_reason
                    logger.warning(
                        "Frontend reported terminal LLM condition (%s); pausing job %s",
                        pause_reason,
                        job_id,
                    )
                    break
            except (LLMError, httpx.HTTPError) as e:
                err_msg = (
                    OLLAMA_WEEKLY_LIMIT_MESSAGE
                    if isinstance(e, LLMRateLimitError)
                    else "LLM rate limited or temporarily unavailable – please retry later"
                    if isinstance(e, LLMTemporaryError)
                    else str(e)
                    if isinstance(e, LLMPermanentError)
                    else f"LLM error: {e}"
                )
                update_task_state(
                    job_id, task_id, status="failed", finished_at=_iso_now(), error=err_msg
                )
                with state_lock:
                    if isinstance(e, LLMRateLimitError):
                        llm_limit_exceeded[0] = True
                        failed[task_id] = OLLAMA_WEEKLY_LIMIT_MESSAGE
                    elif isinstance(e, LLMTemporaryError):
                        failed[task_id] = (
                            "LLM rate limited or temporarily unavailable – please retry later"
                        )
                    elif isinstance(e, LLMPermanentError):
                        failed[task_id] = str(e)
                    else:
                        failed[task_id] = f"LLM error: {e}"
                if isinstance(e, LLMRateLimitError):
                    logger.warning(
                        "Ollama LLM usage limit exceeded for week. Job %s paused.", job_id
                    )
                elif isinstance(e, LLMPermanentError):
                    logger.warning(
                        "%s[%s] Frontend task generation failed validation: %s",
                        log_prefix,
                        task_id,
                        e,
                    )
                else:
                    logger.warning(
                        "%s[%s] Frontend task LLM/HTTP error: %s", log_prefix, task_id, e
                    )
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
                update_job(
                    job_id,
                    status=JOB_STATUS_AGENT_CRASH,
                    error=str(e),
                    agent_crash_details=agent_crash_details,
                )
                se_events.record_event(
                    se_events.CRASH_DETECTED,
                    job_id=job_id,
                    task_id=task_id,
                    phase="execution",
                    detail={"exception_type": type(e).__name__},
                )
                # resolved only when the retry actually succeeds; mutate under the
                # same lock the resolve path uses, so the shared set stays consistent.
                with state_lock:
                    crashed_tasks.add(task_id)
                repair_applied = False
                if type(e) in REPAIRABLE_EXCEPTIONS and task_id not in repaired_tasks:
                    logger.info(
                        "%s[%s] Exception is repairable (%s). Next step -> Attempting automatic repair",
                        log_prefix,
                        task_id,
                        type(e).__name__,
                    )
                    repair_agent = agents.get("repair")
                    if repair_agent:
                        try:
                            from agent_repair_team.models import RepairInput

                            result = repair_agent.run(
                                RepairInput(
                                    traceback=traceback.format_exc(),
                                    exception_type=type(e).__name__,
                                    exception_message=str(e),
                                    task_id=task_id,
                                    agent_type="frontend",
                                    agent_source_path=agent_source_path,
                                )
                            )
                            if result.suggested_fixes and _apply_repair_fixes(
                                agent_source_path, result.suggested_fixes
                            ):
                                repair_applied = True
                                with state_lock:
                                    repaired_tasks.add(task_id)
                                    frontend_queue.append(task_id)
                                update_job(
                                    job_id,
                                    status=JOB_STATUS_RUNNING,
                                    error=None,
                                    agent_crash_details=None,
                                )
                                logger.info(
                                    "%s[%s] Repair applied. Next step -> Re-queuing task for retry",
                                    log_prefix,
                                    task_id,
                                )
                        except Exception as repair_err:
                            logger.warning(
                                "%s[%s] Repair agent failed: %s", log_prefix, task_id, repair_err
                            )
                if not repair_applied:
                    update_task_state(
                        job_id, task_id, status="failed", finished_at=_iso_now(), error=str(e)
                    )
                    with state_lock:
                        failed[task_id] = f"Unhandled exception: {e}"
                    logger.error(
                        "%s[%s] Frontend task failed. Recovery summary: 1) Workflow execution failed, "
                        "2) Repair %s. Final error: %s",
                        log_prefix,
                        task_id,
                        "attempted but unsuccessful"
                        if type(e) in REPAIRABLE_EXCEPTIONS
                        else "not applicable (non-repairable exception)",
                        e,
                    )
                logger.exception("%s[%s] Frontend task exception", log_prefix, task_id)
                checkout_branch(frontend_dir, DEVELOPMENT_BRANCH)

            # After frontend agent is done with all tasks for this repo, containerize it
            devops_agent = agents.get("devops")
            if devops_agent and frontend_dir.is_dir() and (frontend_dir / ".git").exists():
                existing_pipeline = _read_repo_code(frontend_dir, [".yml", ".yaml"])
                tech_lead.trigger_devops_for_frontend(
                    devops_agent,
                    frontend_dir,
                    architecture,
                    spec_content,
                    existing_pipeline=existing_pipeline
                    if existing_pipeline != "# No code files found"
                    else None,
                    build_verifier=_run_build_verification,
                )

    logger.info("Running with parallel workers: 1 backend task, 1 frontend task at a time")
    t_backend = threading.Thread(target=_backend_worker)
    t_frontend = threading.Thread(target=_frontend_worker)
    t_backend.start()
    t_frontend.start()
    t_backend.join()
    t_frontend.join()


def _frontend_has_typescript(frontend_dir: Path) -> bool:
    """Return True when ``frontend_dir`` contains TypeScript source.

    Matches what ``_read_repo_code`` pulls for the actual Integration
    call (``.ts`` + ``.tsx`` + ``.html`` + ``.scss``). A React-style
    TSX-only frontend would slip past a ``*.ts``-only check (PR #424
    Codex P1 round 4) and bypass the integration gate the release
    hook relies on. We check ``.ts`` and ``.tsx`` only — HTML/SCSS
    without TS source isn't an integration-relevant frontend.
    """
    if not frontend_dir.is_dir():
        return False
    # Two rglob walks (rather than scanning every file once) so the
    # any() short-circuits as soon as the first match hits.
    return any(frontend_dir.rglob("*.ts")) or any(frontend_dir.rglob("*.tsx"))


def _initial_integration_outcome(
    *,
    integration_agent: Any,
    has_backend: bool,
    has_frontend: bool,
    completed_code_task_ids: Any,
) -> str:
    """Compute the *pre-run* integration outcome.

    Returns one of:
      * ``"not_run"`` — Integration not applicable (no backend or no
        frontend or no completed code tasks). Release hook ships.
      * ``"failed"`` — Integration was applicable but the agent is
        missing from ``agents``. Treated as a misconfiguration, not
        N/A (PR #424 Codex P2 round 3): an environment with both
        backend and frontend code that has no integration agent
        would otherwise silently mint a release without contract
        validation. Release hook gates the ship.
      * ``"pending"`` — Integration is applicable and the agent is
        present; caller should run it and upgrade the outcome to
        ``"succeeded"`` (clean return) or ``"failed"`` (the call
        threw). Sentinel value, not visible to the release hook.

    Centralising the static branching here keeps the new
    misconfiguration handling unit-testable without driving the
    whole ``run_orchestrator``.
    """
    integration_applicable = bool(has_backend and has_frontend and completed_code_task_ids)
    if not integration_applicable:
        return "not_run"
    if integration_agent is None:
        return "failed"
    return "pending"


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
        snapshot is unavailable or Postgres is disabled. Idempotent per job (a
        resumed/re-run job does not re-emit and double-count). Always flushes job cost.
    """
    try:
        # Idempotency: a job whose terminal metrics were already emitted (i.e. it
        # has a merge_to_main event) must not re-emit on resume/re-run, or it would
        # double-count the deployment and re-add gate re-entries.
        if se_events.job_has_events(job_id, se_events.MERGE_TO_MAIN):
            return
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
            se_events.record_event(
                se_events.TASK_CREATED, job_id=job_id, task_id=tid, phase="design", ts=created_ts
            )
            if status == "merged" and not t.get("resolved_without_changes"):
                merged_ts = _parse_iso(t.get("merged_at"))
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
                if int(t.get("revision_count") or 0) > 0:
                    se_events.record_event(
                        se_events.GATE_REENTRY,
                        job_id=job_id,
                        task_id=tid,
                        phase="execution",
                        ts=merged_ts,
                    )
        if job_status in ("completed", "completed_with_failures"):
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

        # ── Step 2: Planning V3 Team ──────────────────────────────────────────
        # Receives validated spec, performs planning (intake → discovery → requirements → synthesis → document production)
        update_job(
            job_id,
            phase="planning",
            message="Starting planning workflow...",
            status_text="Starting planning workflow",
        )
        logger.info("Next step -> Running Planning V3 team to generate handoff and context")

        from planning_v3_adapter import PlanningV2AdapterResult, adapt_planning_v3_result

        from planning_v3_team.orchestrator import run_workflow as run_planning_v3_workflow

        _planning_v3_job_updater = _make_planning_v3_job_updater(job_id)

        def _run_architecture_for_planning_v3(  # pragma: no cover  # integration-only: runs ArchitectureExpert LLM
            spec_content: str,
            prd_content: Optional[str],
            repo_path: str,
            client_context: Optional[Dict[str, Any]],
        ) -> Optional[str]:
            """Produce architecture overview during Planning V3 document production (merged Architecture Expert)."""
            from architecture_expert.models import ArchitectureInput

            from software_engineering_team.shared.models import ProductRequirements

            req_desc = (spec_content or "").strip()
            if (prd_content or "").strip():
                req_desc = (req_desc + "\n\n" + prd_content.strip()).strip()
            if not req_desc:
                req_desc = "See Planning V3 handoff artifacts."
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

        p3_result = run_planning_v3_workflow(
            repo_path=str(path),
            spec_content=validated_spec,
            use_product_analysis=False,
            use_planning_v2=False,
            llm=get_client("project_planning"),
            job_updater=_planning_v3_job_updater,
            run_architecture_fn=_run_architecture_for_planning_v3,
            # Never let Planning V3 silently auto-decide a clarification question on this path:
            # escalate to the user, and fail closed if escalation is somehow unavailable.
            answer_callback=_build_planning_v3_answer_callback(job_id),
            auto_answer_questions=False,
        )
        if not p3_result.get("success"):
            err = (
                p3_result.get("failure_reason")
                or "Planning V3 workflow did not complete successfully."
            )
            logger.error("Planning V3 failed: %s", err)
            update_job(job_id, status=JOB_STATUS_FAILED, error=err, phase="completed")
            return

        try:
            adapter_result: PlanningV2AdapterResult = adapt_planning_v3_result(
                p3_result, spec_title=requirements.title, repo_path=str(path)
            )
        except ValueError as e:
            logger.error("Planning V3 adapter failed: %s", e)
            update_job(job_id, status=JOB_STATUS_FAILED, error=str(e), phase="completed")
            return

        adapter_result.shared_planning_doc_path = str(
            Path(path) / "plan" / "planning_team" / "planning_document.md"
        )
        requirements = adapter_result.requirements
        update_job(job_id, requirements_title=requirements.title)

        # Check for cancellation after Planning V3
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

        # Execution: delegate to the coding_team orchestrator (it replaces the former
        # Tech Lead + Architecture Expert + backend/frontend code-v2 worker pipeline).
        existing_code_summary = _truncate_for_context(_read_repo_code(path), 8000)
        if existing_code_summary == "# No code files found":
            existing_code_summary = None
        plan_input = _build_coding_team_plan_input(
            adapter_result, str(path), existing_code_summary, resolved_questions_override
        )
        from coding_team.orchestrator import run_coding_team_orchestrator

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
    logger.info(
        "=== Retrying %s failed tasks for job %s: %s ===", len(failed_ids), job_id, failed_ids
    )

    path = Path(repo_path).resolve()
    backend_dir = path / "backend"
    frontend_dir = path / "frontend"
    try:  # pragma: no cover  # integration-only: re-runs failed tasks through full per-task pipeline (LLM + git + subprocess)
        # current_activity from the failed run is stale by definition here; clear it
        # so the retry does not render the old run's frozen sub-bar.
        update_job(
            job_id,
            status=JOB_STATUS_RUNNING,
            failed_tasks=[],
            error=None,
            current_activity=None,
        )

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

        arch_overview = (
            job_data.get("_architecture_overview") or job_data.get("architecture_overview") or ""
        )
        architecture = SystemArchitecture(overview=arch_overview)

        tech_lead = agents["tech_lead"]

        # Partition failed tasks into backend/frontend queues; handle devops/git_setup in prefix
        completed = set()
        failed_retry: Dict[str, str] = {}
        completed_code_task_ids: List[str] = []

        retry_prefix = [
            tid
            for tid in failed_ids
            if all_tasks.get(tid)
            and (all_tasks[tid].type.value == "git_setup" or all_tasks[tid].assignee == "devops")
        ]
        retry_backend_queue: List[str] = []  # All backend retries go through backend_code_v2_team
        retry_backend_code_v2_queue = [
            tid
            for tid in failed_ids
            if all_tasks.get(tid) and all_tasks[tid].assignee in ("backend", "backend-code-v2")
        ]
        retry_frontend_queue: List[
            str
        ] = []  # All frontend retries go through frontend_code_v2_team
        retry_frontend_code_v2_queue = [
            tid
            for tid in failed_ids
            if all_tasks.get(tid) and all_tasks[tid].assignee in ("frontend", "frontend-code-v2")
        ]
        total_tasks = (
            len(retry_prefix)
            + len(retry_backend_queue)
            + len(retry_backend_code_v2_queue)
            + len(retry_frontend_queue)
            + len(retry_frontend_code_v2_queue)
        )

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
                        existing_pipeline=existing_pipeline
                        if existing_pipeline != "# No code files found"
                        else None,
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
                        failed_retry[task_id] = (
                            workflow_result.failure_reason or "DevOps workflow failed"
                        )
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
                len(retry_backend_queue),
                len(retry_frontend_queue),
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
        llm_connectivity_failed = any(
            v in (LLM_UNREACHABLE_AFTER_RETRIES, LLM_SEMANTIC_EXHAUSTION)
            for v in failed_retry.values()
        )

        # Final summary with task breakdown
        logger.info(
            "=== Retry finished: %s completed, %s still failed (of %s retried) ===",
            len(completed),
            len(failed_retry),
            total_tasks,
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
                total_tasks,
                len(completed),
                len(failed_retry),
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
                devops_agent,
                backend_dir,
                architecture,
                spec_content,
                existing_pipeline=existing_pipeline
                if existing_pipeline != "# No code files found"
                else None,
                build_verifier=_run_build_verification,
            )
        if devops_agent and frontend_dir.is_dir() and (frontend_dir / ".git").exists():
            existing_pipeline = _read_repo_code(frontend_dir, [".yml", ".yaml"])
            tech_lead.trigger_devops_for_frontend(
                devops_agent,
                frontend_dir,
                architecture,
                spec_content,
                existing_pipeline=existing_pipeline
                if existing_pipeline != "# No code files found"
                else None,
                build_verifier=_run_build_verification,
            )

        failed_details = [
            {
                "task_id": tid,
                "reason": reason,
                "title": (all_tasks.get(tid).title if all_tasks.get(tid) else tid),
            }
            for tid, reason in failed_retry.items()
        ]
        if llm_connectivity_failed:
            update_job(
                job_id,
                failed_tasks=failed_details,
                status=JOB_STATUS_PAUSED_LLM_CONNECTIVITY,
                error=_llm_pause_error(failed_retry),
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
            update_job(
                job_id,
                failed_tasks=failed_details,
                status=JOB_STATUS_COMPLETED,
                current_task=None,
                status_text="Retry completed",
            )

    except (
        CancellationError
    ):  # pragma: no cover  # integration-only: paired with integration-only try block
        logger.info("Retry orchestrator stopped due to job cancellation: %s", job_id)
        update_job(job_id, status=JOB_STATUS_CANCELLED, status_text="Job cancelled by user")
    except (
        Exception
    ) as e:  # pragma: no cover  # integration-only: paired with integration-only try block
        logger.exception("Retry orchestrator failed")
        update_job(job_id, status=JOB_STATUS_FAILED, error=str(e))
