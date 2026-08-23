"""
Software Engineering orchestrator: runs the full pipeline with feature branches.

Pipeline:
1. Discovery — resolve the spec source and run Product Requirements Analysis.
2. Planning — the standalone ``planning_team`` workflow produces the handoff,
   adapted by ``planning_adapter``; the Architecture Expert produces the
   architecture (injected into planning as a callback).
3. HITL gate — open planning questions pause the job for answers.
4. Execution — the adapted plan is handed to ``coding_team``
   (``run_coding_team_orchestrator``), whose Tech Lead owns task planning,
   the Task Graph, and the per-task backend/frontend v2 workers.
5. Finalize — reconcile the coding-team snapshot into the job's terminal
   status and emit delivery metrics.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from llm_service import (
    OLLAMA_WEEKLY_LIMIT_MESSAGE,
    get_client,
    llm_attribution,
)
from shared.observability import bind_trace_id, current_trace_id, new_trace_id
from shared.repo_context.repo_utils import (
    read_repo_code,
    truncate_for_context,
)
from software_engineering_team.discovery import (
    # Re-exported (F401) for ``tests/test_orchestrator_sprint_path.py``, which
    # imports it off the orchestrator module (``_orchestrator._load_requirements_
    # from_sprint``) at six call sites. The sprint path itself calls it via
    # ``resolve_spec_source``/``discovery``; this re-export exists solely so the
    # test patch surface ``from orchestrator import _load_requirements_from_sprint``
    # keeps working without the tests reaching into ``discovery`` directly.
    _load_requirements_from_sprint,  # noqa: F401
    resolve_spec_source,
    run_product_requirements_analysis,
)
from software_engineering_team.shared import (
    cost_tracker,
    planning_audit,
    se_events,
)
from software_engineering_team.shared.execution_tracker import execution_tracker
from software_engineering_team.shared.job_store import (
    JOB_STATUS_CANCELLED,
    JOB_STATUS_COMPLETED,
    JOB_STATUS_FAILED,
    JOB_STATUS_PAUSED_LLM_CONNECTIVITY,
    JOB_STATUS_PAUSED_LLM_LIMIT,
    JOB_STATUS_RUNNING,
    LLM_SEMANTIC_EXHAUSTION,
    LLM_UNREACHABLE_AFTER_RETRIES,
    add_pending_questions,
    get_job,
    is_cancel_requested,
    is_waiting_for_answers,
    update_job,
)
from software_engineering_team.shared.plan_dir import ensure_plan_dir
from software_engineering_team.shared.project_overview_builder import build_project_overview

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


def _make_phase_job_updater(
    job_id: str,
    *,
    subprocess_key: str,
    completed_key: str,
    phase_order: List[str],
    progress_band: "tuple[int, int]",
    phase: Optional[str] = None,
) -> Callable[..., None]:
    """Build the job updater handed to a phase's sub-agent (PRA, Planning, ...).

    Preconditions:
        - ``subprocess_key``/``completed_key`` are non-empty strings naming the
          phase-specific job fields to write (e.g. ``"analysis_subprocess"``/
          ``"analysis_completed_phases"``).
        - ``phase_order`` is a list of phase-name strings in pipeline order; an
          empty list is valid (no phase ever completes, only the current one is set).
        - ``progress_band`` is a ``(base, span)`` tuple valid per :func:`_scale_progress`.
        - ``phase`` is either ``None`` (forward the caller's own ``phase`` kwarg, if
          any, unmodified) or a job-status phase string to force on every write.
    Postconditions:
        - Returns an updater that pops ``current_phase`` from kwargs and rewrites
          it into ``subprocess_key``/``completed_key`` (the latter being every
          ``phase_order`` entry preceding ``current_phase``); rescales a ``progress``
          kwarg onto ``progress_band`` (garbage progress is dropped, never written);
          forces ``phase=phase`` on the ``update_job`` call when ``phase`` is not
          ``None``; forwards all remaining kwargs untouched; and swallows store
          errors (observability only — never raises).
    """
    assert isinstance(subprocess_key, str) and subprocess_key, subprocess_key
    assert isinstance(completed_key, str) and completed_key, completed_key
    assert isinstance(phase_order, list), f"phase_order must be a list, got {type(phase_order)}"

    def _updater(**kwargs: Any) -> None:
        try:
            current_phase = kwargs.pop("current_phase", None)
            if current_phase:
                kwargs[subprocess_key] = current_phase
                completed_phases = []
                for p in phase_order:
                    if p == current_phase:
                        break
                    completed_phases.append(p)
                kwargs[completed_key] = completed_phases
            # The sub-agent reports its own 0-100 progress; rescale onto this
            # phase's band so the job bar is monotone across the whole run.
            if "progress" in kwargs:
                scaled = _scale_progress(kwargs.pop("progress"), progress_band)
                if scaled is not None:
                    kwargs["progress"] = scaled
            if phase is not None:
                update_job(job_id, phase=phase, **kwargs)
            else:
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
        logger.info(
            "Cancellation detected for job %s", job_id, extra={"trace_id": current_trace_id()}
        )
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
    logger.warning(
        "Timed out waiting for user answers on job %s",
        job_id,
        extra={"trace_id": current_trace_id()},
    )
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
    from software_engineering_team.hitl import answers_to_resolved

    structured = _convert_to_structured_questions(question_texts, source=source)
    if not structured:
        return [], True
    add_pending_questions(job_id, structured)
    if slack_notify_open_questions:
        slack_notify_open_questions(job_id, structured, source="run-team")
    logger.info(
        "Job %s waiting for %d clarification answer(s) from the user",
        job_id,
        len(structured),
        extra={"trace_id": current_trace_id()},
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


# Fallback technology_preferences for the Architecture Expert when the client hasn't
# specified tech_constraints during Planning intake/discovery; this repo's own stack.
_DEFAULT_TECHNOLOGY_PREFERENCES = ["Python", "FastAPI", "PostgreSQL", "Docker"]


def _run_architecture_for_planning(
    arch_agent: Any,
    spec_content: str,
    prd_content: Optional[str],
    repo_path: str,  # part of the Planning callback contract; unused here
    client_context: Optional[Dict[str, Any]],
) -> Optional[str]:
    """Produce an architecture overview from Planning's spec / PRD / client context.

    Module-level so it can be unit-tested directly, without going through the
    ``_make_planning_architecture_fn`` factory that binds it to a specific ``arch_agent``.

    Preconditions:
        - ``arch_agent`` exposes ``run(ArchitectureInput) -> ArchitectureOutput`` (the SE
          ``ArchitectureExpertAgent``; duck-typed so tests may pass a mock).
        - Invoked with ``spec_content``, ``prd_content``, ``repo_path``, ``client_context``
          by keyword (the shape Planning's document-production phase uses); ``spec_content``
          may be empty and ``prd_content`` / ``client_context`` may be ``None``.
    Postconditions:
        - Returns ``architecture.overview`` (possibly ``""``) on success, or ``None`` when
          the agent yields no architecture or raises.
        - ``technology_preferences`` passed to the architecture agent are drawn from
          ``client_context["tech_constraints"]`` (the ``ClientContext.tech_constraints``
          gathered during Planning intake/discovery) when present and non-empty, falling
          back to ``_DEFAULT_TECHNOLOGY_PREFERENCES`` otherwise.
    Invariants:
        - Never propagates an exception into the Planning workflow.
    """
    from shared.dev_models.models import ProductRequirements
    from software_engineering_team.architect_agents.architecture_expert.models import (
        ArchitectureInput,
    )

    req_desc = (spec_content or "").strip()
    if (prd_content or "").strip():
        req_desc = (req_desc + "\n\n" + prd_content.strip()).strip()
    if not req_desc:
        req_desc = "See Planning handoff artifacts."
    acceptance = ["Deliver according to spec and planning artifacts."]
    if client_context and client_context.get("success_criteria"):
        acceptance = list(client_context["success_criteria"])
    technology_preferences = (
        list(client_context["tech_constraints"])
        if client_context and client_context.get("tech_constraints")
        else list(_DEFAULT_TECHNOLOGY_PREFERENCES)
    )
    requirements = ProductRequirements(
        title="Project",
        description=req_desc,
        acceptance_criteria=acceptance,
        constraints=[],
        priority="medium",
        metadata={},
    )
    try:
        project_overview = build_project_overview(
            prd_content=prd_content, client_context=client_context
        )
        features_doc = project_overview["features_and_functionality_doc"]
        arch_input = ArchitectureInput(
            requirements=requirements,
            technology_preferences=technology_preferences,
            project_overview=project_overview,
            features_and_functionality_doc=features_doc or None,
        )
        arch_output = arch_agent.run(arch_input)
        return (
            (arch_output.architecture.overview or "")
            if arch_output and arch_output.architecture
            else None
        )
    except Exception:
        return None


def _make_planning_architecture_fn(
    arch_agent_provider: Callable[[], Any],
) -> Callable[..., Optional[str]]:
    """Build the architecture callback handed to the Planning document-production phase.

    The merged Architecture Expert runs inside Planning: this factory binds the SE
    architecture agent into the callback Planning invokes (by keyword) to turn the
    validated spec / PRD / client context into a high-level architecture overview.

    The agent is resolved *lazily* through ``arch_agent_provider`` when Planning first
    invokes the callback, and *defensively*: a provider that raises (e.g. the agent
    cannot be built because no LLM provider is configured) degrades to no architecture
    overview rather than aborting the Planning phase. Passing a provider — rather than an
    already-resolved agent — keeps the ``_LazyAgentRegistry`` subscript out of the caller's
    ``try`` boundary so its construction cost and failures land inside this guard.

    Preconditions:
        - ``arch_agent_provider`` is a zero-argument callable returning an object that
          exposes ``run(ArchitectureInput) -> ArchitectureOutput`` (the SE
          ``ArchitectureExpertAgent``; duck-typed so tests may pass a mock). It is called
          once per callback invocation and may raise.
    Postconditions:
        - Returns a callback ``(spec_content, prd_content, repo_path, client_context)
          -> Optional[str]`` that never raises: it yields the architecture overview string
          (possibly ``""``) on success, or ``None`` when the provider raises, the agent
          produces no architecture, or the agent raises.
    """

    def _fn(spec_content, prd_content, repo_path, client_context) -> Optional[str]:
        try:
            arch_agent = arch_agent_provider()
        except Exception:
            return None
        return _run_architecture_for_planning(
            arch_agent, spec_content, prd_content, repo_path, client_context
        )

    return _fn


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


class _LazyAgentRegistry(Mapping):
    """Role-keyed mapping of SE agents, each built on first access and cached.

    Values are produced by zero-argument factories the first time their key is
    subscripted, then memoized. Membership tests and iteration never trigger
    construction — only ``registry[key]`` does. This is why the thread-mode /
    Temporal pipeline, which reads only ``registry["architecture"]``, no longer
    pays the ``get_client`` provider-store read (and, for ``devops``, a large
    sub-agent fan-out) for the roles it never consumes.

    Preconditions:
        - ``factories`` maps role names (str) to zero-argument callables.
    Postconditions:
        - ``self[key]`` returns the cached result of ``factories[key]()``; the
          factory runs at most once per key. A key absent from ``factories``
          raises ``KeyError``.
    Invariants:
        - ``__contains__`` / ``__iter__`` / ``__len__`` reflect the factory key
          set without constructing any agent.

    Note: ``values()`` / ``items()`` (inherited from ``Mapping``) would force
    construction of every remaining agent; no caller uses them.
    """

    def __init__(self, factories: Dict[str, Callable[[], Any]]) -> None:
        assert all(callable(f) for f in factories.values()), "factories must be zero-arg callables"
        self._factories = factories
        self._cache: Dict[str, Any] = {}

    def __getitem__(self, key: str) -> Any:
        if key not in self._factories:
            raise KeyError(key)
        if key not in self._cache:
            self._cache[key] = self._factories[key]()
        return self._cache[key]

    def __contains__(self, key: object) -> bool:
        # Override ``Mapping``'s default, which would call ``__getitem__`` and
        # thereby construct the agent — the exact cost this registry avoids.
        return key in self._factories

    def __iter__(self):
        return iter(self._factories)

    def __len__(self) -> int:
        return len(self._factories)


def _get_agents() -> Mapping[str, Any]:
    """Build the lazy SE agent fleet, keyed by role.

    Each agent uses ``get_client(key)`` for per-agent model configuration. The
    main pipeline uses ``planning_team`` for planning; the spec-intake /
    project-planning / domain-planning agents are not used in the main flow
    (``clarification_store`` may still use Spec Intake elsewhere).

    Audit (kept honest here so future readers do not assume the dict is fully
    consumed): the two production callers of ``_get_agents`` — the thread-mode
    orchestrator below and ``temporal/activities.py`` — read only
    ``agents["architecture"]`` from the returned mapping. Per-task
    backend/frontend work is delegated to the coding-team / code-v2 sub-teams,
    which construct their own tool agents via ``_build_tool_agents`` rather than
    reading them from this mapping. The remaining entries are retained because
    the integration tests in ``test_backend_code_v2_integration.py`` and
    ``test_frontend_code_v2_integration.py`` pin the presence of the v2 team
    leads, and this function is the canonical fleet factory for the thread-mode
    pipeline.

    Returns a lazy mapping (:class:`_LazyAgentRegistry`): each role's agent is
    constructed on first subscript and then cached; membership tests and
    iteration never construct. Since only ``architecture`` is read in
    production, the other roles cost nothing unless a caller asks for them —
    which avoids eagerly paying ``get_client`` (and the ``devops`` sub-agent
    fan-out) on every call.

    Postconditions:
        - Returns a ``Mapping[str, Any]`` over the SE role names; ``result[key]``
          lazily builds and caches the corresponding agent.
    """
    from agent_repair_team import RepairExpertAgent
    from software_engineering_team.accessibility_agent import AccessibilityExpertAgent
    from software_engineering_team.architect_agents.architecture_expert import (
        ArchitectureExpertAgent,
    )
    from software_engineering_team.build_fix_specialist import BuildFixSpecialistAgent
    from software_engineering_team.devops_team import DevOpsTeamLeadAgent
    from software_engineering_team.git_setup_agent import GitSetupAgent
    from software_engineering_team.integration_team import IntegrationAgent
    from software_engineering_team.linting_tool_agent import LintingToolAgent
    from software_engineering_team.technical_writers.documentation_agent import (
        DocumentationAgent,
    )

    return _LazyAgentRegistry(
        {
            "architecture": lambda: ArchitectureExpertAgent(get_client("architecture")),
            "integration": lambda: IntegrationAgent(get_client("integration")),
            "devops": lambda: DevOpsTeamLeadAgent(get_client("devops")),
            "backend": _lazy_init_backend_code_v2_team,
            "frontend_code_v2": _lazy_init_frontend_code_v2_team,
            "accessibility": lambda: AccessibilityExpertAgent(get_client("accessibility")),
            "documentation": lambda: DocumentationAgent(get_client("documentation")),
            "git_setup": GitSetupAgent,
            "repair": lambda: RepairExpertAgent(get_client("repair")),
            "linting_tool_agent": lambda: LintingToolAgent(get_client("linting_tool_agent")),
            "build_fix_specialist": lambda: BuildFixSpecialistAgent(
                get_client("build_fix_specialist")
            ),
        }
    )


def _lazy_init_backend_code_v2_team():
    """Instantiate the backend team lead (backend_code_v2_team; lazy import)."""
    from software_engineering_team.backend_code_v2_team import BackendCodeV2TeamLead

    return BackendCodeV2TeamLead(get_client("backend"))


def _lazy_init_frontend_code_v2_team():
    """Instantiate the frontend team lead (frontend_code_v2_team; lazy import)."""
    from software_engineering_team.frontend_code_v2_team import FrontendCodeV2TeamLead

    return FrontendCodeV2TeamLead(get_client("frontend"))


MAX_REVIEW_ITERATIONS = 15
MAX_CLARIFICATION_REFINEMENTS = 10  # Max times to refine a task based on specialist clarification
MAX_CODE_REVIEW_ITERATIONS = 10  # Max rounds of code review -> fix -> re-review


def _build_coding_team_plan_input(
    adapter_result: Any,
    repo_path: str,
    existing_code_summary: Optional[str] = None,
    resolved_questions: Optional[List[Dict[str, Any]]] = None,
) -> Any:
    """Build CodingTeamPlanInput from PlanningAdapterResult for coding_team orchestrator."""
    from software_engineering_team.models import CodingTeamPlanInput

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
        logger.debug(
            "failed to emit coding-team DORA metrics for %s",
            job_id,
            exc_info=True,
            extra={"trace_id": current_trace_id()},
        )
    finally:
        cost_tracker.flush(job_id)


def _fail_job(job_id: str, error: str, *, phase: str = "completed") -> None:
    """Mark ``job_id`` FAILED with ``error``.

    Collapses the ``update_job(status=JOB_STATUS_FAILED, error=..., phase="completed")``
    shape shared by several of ``run_orchestrator``'s failure paths. Callers keep their
    own ``logger.error``/``logger.exception`` call (the message differs per site)
    immediately before invoking this.
    """
    update_job(job_id, status=JOB_STATUS_FAILED, error=error, phase=phase)


def _mark_cancelled(job_id: str) -> None:
    """Mark ``job_id`` CANCELLED — the terminal status both SE entry points write on cancellation.

    Preconditions: ``job_id`` refers to a stored job.
    Postconditions: the job is CANCELLED with the shared status text and ``completed`` phase.
    """
    update_job(
        job_id,
        status=JOB_STATUS_CANCELLED,
        status_text="Job cancelled by user",
        phase="completed",
    )


def run_orchestrator(
    job_id: str,
    repo_path: str | Path,
    *,
    spec_content_override: Optional[str] = None,
    resolved_questions_override: Optional[List[Dict[str, Any]]] = None,
    planning_only: bool = False,
    sprint_id: Optional[str] = None,
    trace_id: Optional[str] = None,
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
    - sprint_id: when set, pull the planned scope from the
      product_delivery sprint's stories and synthesize requirements
      directly — Discovery's LLM spec-parse and the PRA agent are
      skipped. Mutually exclusive with ``spec_content_override``.
    - trace_id: caller-supplied trace id (e.g. from a Temporal activity) to bind
      for this run instead of generating a fresh one; every phase — Discovery,
      Design, Execution, Integration — shares the single id bound here.
    """
    path = Path(repo_path).resolve()
    with bind_trace_id(trace_id or new_trace_id()):
        _run_orchestrator_body(
            job_id,
            path,
            spec_content_override=spec_content_override,
            resolved_questions_override=resolved_questions_override,
            planning_only=planning_only,
            sprint_id=sprint_id,
        )


def _run_orchestrator_body(
    job_id: str,
    path: Path,
    *,
    spec_content_override: Optional[str],
    resolved_questions_override: Optional[List[Dict[str, Any]]],
    planning_only: bool,
    sprint_id: Optional[str],
) -> None:
    """Body of :func:`run_orchestrator`, run inside its ``bind_trace_id`` block.

    Preconditions: a trace id is already bound (callers must go through
        :func:`run_orchestrator`); ``path`` is an absolute, resolved work path.
    Postconditions: the job reaches a terminal status, and every phase this drives
        observes the caller's bound trace id via ``current_trace_id()``.
    """
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

        # 1. Resolve the spec source (sprint / override / newest-on-disk) and parse it.
        source = resolve_spec_source(
            job_id,
            path,
            sprint_id=sprint_id,
            spec_content_override=spec_content_override,
            update_job_fn=update_job,
        )
        if source is None:
            return
        requirements = source.requirements

        update_job(
            job_id,
            requirements_title=requirements.title,
            status_text="Specification parsed successfully",
        )

        # Check for cancellation after spec parsing
        _check_cancellation(job_id)

        # Create plan folder after spec is ingested successfully (all planning artifacts go here)
        plan_dir = ensure_plan_dir(path)
        logger.info("Plan folder ensured at %s", plan_dir, extra={"trace_id": current_trace_id()})

        # ── Step 1: Product Requirements Analysis Agent (skipped on the sprint path) ──
        _pra_job_updater = _make_phase_job_updater(
            job_id,
            subprocess_key="analysis_subprocess",
            completed_key="analysis_completed_phases",
            phase_order=PRA_PHASE_ORDER,
            progress_band=PROGRESS_BAND_PRODUCT_ANALYSIS,
            phase="product_analysis",
        )
        validated_spec = run_product_requirements_analysis(
            job_id,
            path,
            source,
            sprint_id=sprint_id,
            pra_job_updater=_pra_job_updater,
            update_job_fn=update_job,
        )
        if validated_spec is None:
            return

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
        logger.info(
            "Next step -> Running Planning team to generate handoff and context",
            extra={"trace_id": current_trace_id()},
        )

        from planning_team.orchestrator import run_workflow as run_planning_workflow
        from software_engineering_team.planning_adapter import (
            PlanningAdapterResult,
            adapt_planning_result,
        )

        _planning_job_updater = _make_phase_job_updater(
            job_id,
            subprocess_key="planning_subprocess",
            completed_key="planning_completed_phases",
            phase_order=PLANNING_PHASE_ORDER,
            progress_band=PROGRESS_BAND_PLANNING,
        )

        planning_result = run_planning_workflow(
            repo_path=str(path),
            spec_content=validated_spec,
            use_product_analysis=False,
            llm=get_client("project_planning"),
            job_updater=_planning_job_updater,
            run_architecture_fn=_make_planning_architecture_fn(lambda: agents["architecture"]),
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
            logger.error("Planning failed: %s", err, extra={"trace_id": current_trace_id()})
            return _fail_job(job_id, err)

        planning_audit.record_se_planning_run(job_id, planning_result)

        try:
            adapter_result: PlanningAdapterResult = adapt_planning_result(
                planning_result, spec_title=requirements.title, repo_path=str(path)
            )
        except ValueError as e:
            logger.error("Planning adapter failed: %s", e, extra={"trace_id": current_trace_id()})
            return _fail_job(job_id, str(e))

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
            logger.info(
                "Planning-only run: stopping before execution (job %s)",
                job_id,
                extra={"trace_id": current_trace_id()},
            )
            update_job(job_id, status=JOB_STATUS_COMPLETED, phase="completed")
            return

        # Execution: delegate to the coding_team orchestrator (it replaces the former
        # Tech Lead + Architecture Expert + backend/frontend code-v2 worker pipeline).
        existing_code_summary = truncate_for_context(read_repo_code(path), 8000)
        if existing_code_summary == "# No code files found":
            existing_code_summary = None
        plan_input = _build_coding_team_plan_input(
            adapter_result, str(path), existing_code_summary, resolved_questions_override
        )
        _run_coding_and_finalize(job_id, path, plan_input)
        return

    except (
        CancellationError
    ):  # pragma: no cover  # integration-only: paired with integration-only try block
        logger.info(
            "Orchestrator stopped due to job cancellation: %s",
            job_id,
            extra={"trace_id": current_trace_id()},
        )
        _mark_cancelled(job_id)
    except (
        Exception
    ) as e:  # pragma: no cover  # integration-only: paired with integration-only try block
        logger.exception("Orchestrator failed", extra={"trace_id": current_trace_id()})
        _fail_job(job_id, str(e))


def _latest_failure_reason(task: Dict[str, Any]) -> str:
    """Return the most recent actionable failure reason recorded for a snapshot task.

    ``revision_feedback`` entries are not uniform: an engineer/Tech-Lead bounce uses ``reason``, a
    build-gate failure uses ``{"type": "build", "error": ...}``, and a review issue may use
    ``description``. Scanning newest-first for the first meaningful string across those keys keeps the
    public ``failed_tasks`` reason (and the LLM-pause detection) tied to the *current* failure — not a
    blank entry, and not a stale marker from an earlier attempt whose history ``reset_failed``
    preserved.

    Preconditions:
        - ``task`` is a task dict from a persisted ``task_graph_snapshot``.
    Postconditions:
        - Returns the newest non-empty ``reason``/``description``/``error`` string, or ``""`` when the
          task carries no actionable feedback.
    """
    for fb in reversed(task.get("revision_feedback") or []):
        if not isinstance(fb, dict):
            continue
        for key in ("reason", "description", "error"):
            val = fb.get(key)
            if isinstance(val, str) and val.strip():
                return val
    return ""


def _finalize_from_coding_snapshot(job_id: str) -> None:
    """Reconcile the SE job-status contract from a completed coding-team run's task graph.

    The coding-team orchestrator persists only ``task_graph_snapshot`` plus a coarse status; it
    never writes the SE ``failed_tasks`` list that ``GET /run-team/{job_id}`` and the
    ``/retry-failed`` gate read, and it does not translate LLM weekly-limit / connectivity failures
    into the ``paused_llm_*`` statuses the resume-after-LLM-check flow depends on. This helper
    derives both from the persisted snapshot after the run so those flows keep working — for the
    main run and the retry alike.

    Preconditions:
        - Called after ``run_coding_team_orchestrator`` returned for ``job_id`` (it has persisted the
          final ``task_graph_snapshot`` and its terminal status).
    Postconditions:
        - ``failed_tasks`` reflects the snapshot's FAILED tasks (``[]`` when none failed).
        - When a failure reason carries an Ollama weekly-limit / connectivity marker, the job status
          is overridden to ``paused_llm_limit`` / ``paused_llm_connectivity`` (else the coding-team
          terminal status is left intact). No-op when the job or its snapshot is absent.
    """
    data = get_job(job_id)
    if not data:
        return
    snapshot = data.get("task_graph_snapshot") or []
    if not snapshot:
        return

    failed = [t for t in snapshot if isinstance(t, dict) and t.get("status") == "failed"]
    failed_details: List[Dict[str, Any]] = []
    # Only the CURRENT (latest) failure reason per still-failed task drives the failed_tasks detail
    # and the LLM-pause decision — never the preserved history — so a stale weekly-limit/connectivity
    # marker from an earlier attempt cannot re-pause a retry that failed for an unrelated reason.
    latest_reasons: List[str] = []
    for t in failed:
        reason = _latest_failure_reason(t)
        latest_reasons.append(reason)
        failed_details.append(
            {
                "task_id": str(t.get("id", "")),
                "title": str(t.get("title") or t.get("id") or ""),
                "reason": reason or "Task failed during the coding run",
            }
        )

    # Repopulate the SE failed_tasks list so status/retry APIs see the current failures (the
    # coding-team run only wrote task_graph_snapshot). Empty list on a clean run is intentional.
    update_job(job_id, failed_tasks=failed_details)

    # Restore the LLM-pause statuses the old retry path produced: a weekly-limit or connectivity
    # failure that recurred during the run should pause for recovery rather than read as a plain
    # completed-with-failures, so the documented resume-after-LLM-check flow still applies.
    if any(OLLAMA_WEEKLY_LIMIT_MESSAGE in r for r in latest_reasons):
        update_job(
            job_id,
            status=JOB_STATUS_PAUSED_LLM_LIMIT,
            error=OLLAMA_WEEKLY_LIMIT_MESSAGE,
            current_task=None,
        )
    elif any(
        LLM_SEMANTIC_EXHAUSTION in r or LLM_UNREACHABLE_AFTER_RETRIES in r for r in latest_reasons
    ):
        error = (
            LLM_SEMANTIC_EXHAUSTION
            if any(LLM_SEMANTIC_EXHAUSTION in r for r in latest_reasons)
            else LLM_UNREACHABLE_AFTER_RETRIES
        )
        update_job(job_id, status=JOB_STATUS_PAUSED_LLM_CONNECTIVITY, error=error)


def _run_coding_and_finalize(
    job_id: str, path: Path, plan_input: Any, *, retry_failed: bool = False
) -> None:
    """Run the coding-team orchestrator for an SE job and reconcile SE status from its snapshot.

    Shared by the main run (:func:`run_orchestrator`) and the retry path
    (:func:`run_failed_tasks`); the only difference is ``retry_failed``.

    ``get_llm`` is deliberately NOT passed: the coding team's default getter wraps the LLM clients
    with reasoning-stream capture, whose periodic flush is the only thing that refreshes job activity
    DURING a multi-minute LLM call — passing the raw ``get_client`` here made every long implement
    call look like a stall to the UI's activity-based warning. LLM attribution is bound around the
    whole coding run so every call it makes (sequential + via strands' ``asyncio.to_thread``, which
    copies the context) is attributed to this SE job — what the cost tracker keys on.

    ``run_coding_team_orchestrator`` owns its terminal status on every exit path (completed /
    completed_with_failures / already_complete / failed / cancelled), so this does not finalize a
    success status; it only emits DORA lifecycle metrics and reconciles the SE ``failed_tasks`` list
    (and any LLM-pause status) from the persisted snapshot via
    :func:`_finalize_from_coding_snapshot`, so partial failures stay visible and retryable.

    Preconditions: ``path`` is the resolved work path; ``plan_input`` is a ``CodingTeamPlanInput``.
    Postconditions: the coding-team orchestrator has run to a terminal status for ``job_id`` and the
    SE ``failed_tasks`` / LLM-pause status reflect the persisted snapshot.
    """
    from software_engineering_team.coding_engine_provider import SECodeEngineProvider
    from software_engineering_team.coding_team_orchestrator import run_coding_team_orchestrator

    base, span = PROGRESS_BAND_CODING
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
            retry_failed=retry_failed,
        )
    _emit_coding_team_metrics(job_id)
    _finalize_from_coding_snapshot(job_id)


def run_failed_tasks(job_id: str, *, trace_id: Optional[str] = None) -> None:
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
        - ``trace_id`` (caller-supplied, or freshly generated) is bound for the Execution/Integration
          re-entry the retry performs, matching ``run_orchestrator``'s per-job trace id.
    """
    with bind_trace_id(trace_id or new_trace_id()):
        _run_failed_tasks_body(job_id)


def _run_failed_tasks_body(job_id: str) -> None:
    """Body of :func:`run_failed_tasks`, run inside its ``bind_trace_id`` block.

    Preconditions: a trace id is already bound (callers must go through
        :func:`run_failed_tasks`); ``job_id`` carries ``repo_path`` and a task-graph snapshot.
    Postconditions: as :func:`run_failed_tasks` — the retry reaches a terminal status, with
        the caller's bound trace id visible to the Execution/Integration work it re-enters.
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
    logger.info(
        "=== Retrying failed tasks for job %s (repo %s) ===",
        job_id,
        path,
        extra={"trace_id": current_trace_id()},
    )

    from software_engineering_team.models import CodingTeamPlanInput

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

    # current_activity from the failed run is stale by definition here; clear it so the retry does
    # not render the old run's frozen sub-bar. Clear failed_tasks too: the coding-team run only
    # writes task_graph_snapshot, never failed_tasks, so the persisted list the status endpoint and
    # retry gate read (api/routes/jobs.py) would otherwise keep reporting the pre-retry failures.
    update_job(
        job_id, status=JOB_STATUS_RUNNING, failed_tasks=[], error=None, current_activity=None
    )
    try:
        _run_coding_and_finalize(job_id, path, plan_input, retry_failed=True)
    except CancellationError:
        logger.info(
            "Retry orchestrator stopped due to job cancellation: %s",
            job_id,
            extra={"trace_id": current_trace_id()},
        )
        _mark_cancelled(job_id)
    except Exception as e:
        logger.exception("Retry orchestrator failed", extra={"trace_id": current_trace_id()})
        _fail_job(job_id, str(e))
