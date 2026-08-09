"""Shared module-level state and pure helpers for the SE team API.

Single owner of the mutable globals (the orchestrator-thread registry, status
sets, workspace/log config) and the small pure parse/validation helpers the
route modules reuse. Imports nothing from the route or background modules, so it
never participates in an import cycle.

Invariants:
    - The orchestrator-thread registry itself lives in
      ``shared.run_thread_registry.RunThreadRegistry``; background threads and routes go through
      the registry's methods (e.g. ``_is_orchestrator_alive``) rather than poking its internals.
"""

import logging
import os
import re
import tempfile
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import HTTPException
from pydantic import ValidationError

from shared.concurrency import BackgroundHeartbeat
from shared.hitl.progress import coerce_progress
from shared.run_thread_registry import RunThreadRegistry
from software_engineering_team.api.models import (
    CurrentActivityEntry,
    TaskStateEntry,
    TeamProgressEntry,
)
from software_engineering_team.shared.job_store import (
    JOB_STATUS_AGENT_CRASH,
    JOB_STATUS_ALREADY_COMPLETE,
    JOB_STATUS_CANCELLED,
    JOB_STATUS_COMPLETED,
    JOB_STATUS_FAILED,
    JOB_STATUS_PENDING,
    JOB_STATUS_RUNNING,
    get_stale_after_seconds,
    mark_stale_jobs_failed,
)

logger = logging.getLogger(__name__)

_stale_monitor_started = False
_stale_monitor_lock = threading.Lock()


def _start_stale_job_monitor_once() -> None:
    global _stale_monitor_started
    with _stale_monitor_lock:
        if _stale_monitor_started:
            return

        def _sweep() -> None:  # pragma: no cover - runs only in the background beater thread
            mark_stale_jobs_failed(
                stale_after_seconds=get_stale_after_seconds(),
                reason="Job heartbeat stale while pending/running",
            )

        BackgroundHeartbeat(
            _sweep,
            30.0,
            name="se-team-stale-job-monitor",
            beat_first=True,
            on_error=lambda exc: logger.warning("stale job monitor error: %s", exc),
        ).start()
        _stale_monitor_started = True


def _get_workspace_base_dir() -> Path:
    """Base dir for auto-created project workspaces.
    Fallback: SE_WORKSPACE_DIR -> ENV_WORKSPACE_ROOT -> ./se_workspaces
    """
    for var in ("SE_WORKSPACE_DIR", "ENV_WORKSPACE_ROOT"):
        val = os.environ.get(var, "").strip()
        if val:
            return Path(val)
    return Path.cwd() / "se_workspaces"


_SAFE_NAME_RE = re.compile(r"[^a-z0-9\-]")


def create_project_workspace(project_name: str, spec_content: bytes) -> Path:
    """Sanitize name, create timestamped folder, write initial_spec.md. Returns workspace Path."""
    name = project_name.strip().lower().replace(" ", "-")
    name = _SAFE_NAME_RE.sub("", name)
    name = re.sub(r"-{2,}", "-", name).strip("-")
    if not name:
        raise ValueError("project_name is empty after sanitization")
    spec_text = spec_content.decode("utf-8")
    if not spec_text.strip():
        raise ValueError("spec_file content is empty")
    folder = f"{name}_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}"
    base = _get_workspace_base_dir().resolve()
    workspace = (base / folder).resolve()
    try:
        workspace.relative_to(base)  # path-traversal guard
    except ValueError:
        raise ValueError(f"Workspace path escapes base dir: {workspace}")
    workspace.mkdir(parents=True, exist_ok=False)
    (workspace / "initial_spec.md").write_text(spec_text, encoding="utf-8")
    logger.info("Created workspace %s for project %r", workspace, project_name)
    return workspace


# Track active orchestrator threads so we can detect when a server restart killed one
_registry = RunThreadRegistry()
_is_orchestrator_alive = _registry.is_alive


def _preflight_sprint_scope(sprint_id: Optional[str]) -> None:
    """Validate sprint exists *and has at least one executable story* before launch.

    Used by `POST /run-team`, `POST /run-team/{id}/resume`, and
    `POST /run-team/{id}/restart` to keep the failure mode synchronous
    (4xx) instead of async (job spins up, orchestrator hard-fails on
    empty scope). Codex review on PR #396 flagged two cases that still
    slipped through:

      * sprints that exist but were never planned;
      * sprints whose every planned story is in a terminal status
        (``done``/``completed``/``cancelled``/``closed``) — the
        orchestrator's synthesizer drops those before generating the
        spec, so an all-terminal sprint also hard-fails async.

    Raises ``HTTPException`` with the appropriate status code:

      * 404 if the sprint id is missing
      * 400 if the sprint has no planned stories or every planned
        story is terminal
      * 503 if product_delivery storage is unavailable / the module
        can't be imported (deployment topology issue)
    """
    if sprint_id is None:
        return
    try:
        from product_delivery import (  # noqa: PLC0415 — lazy cross-team import
            TERMINAL_STORY_STATUSES,
            ProductDeliveryStorageUnavailable,
            get_store,
        )
    except ImportError as e:
        raise HTTPException(
            status_code=503,
            detail=f"product_delivery store unavailable; cannot resolve sprint_id: {e}",
        ) from e
    try:
        sprint_view = get_store().get_sprint_with_stories(sprint_id)
    except ProductDeliveryStorageUnavailable as e:
        raise HTTPException(
            status_code=503,
            detail=f"product_delivery storage unavailable; cannot resolve sprint_id: {e}",
        ) from e
    if sprint_view is None:
        raise HTTPException(
            status_code=404,
            detail=f"sprint {sprint_id!r} does not exist",
        )
    if not sprint_view.stories:
        raise HTTPException(
            status_code=400,
            detail=(
                f"sprint {sprint_id!r} has no planned stories; "
                "run POST /api/product-delivery/sprints/{id}/plan first."
            ),
        )
    # Mirror the orchestrator-side terminal-status filter — case-
    # insensitive so a story marked `Done` instead of `done` doesn't
    # smuggle past the same way the orchestrator would catch it.
    executable = [
        s
        for s in sprint_view.stories
        if (s.status or "").strip().lower() not in TERMINAL_STORY_STATUSES
    ]
    if not executable:
        raise HTTPException(
            status_code=400,
            detail=(
                f"sprint {sprint_id!r} has no executable stories — every planned "
                "story is in a terminal status (done/completed/cancelled/closed)."
            ),
        )


def _parse_task_states(raw: Any) -> Optional[Dict[str, TaskStateEntry]]:
    """Convert raw task_states dict from job store to TaskStateEntry map."""
    if not raw or not isinstance(raw, dict):
        return None
    result: Dict[str, TaskStateEntry] = {}
    for task_id, entry in raw.items():
        if not isinstance(entry, dict):
            continue
        try:
            result[task_id] = TaskStateEntry(
                status=entry.get("status", "pending"),
                assignee=entry.get("assignee", "unknown"),
                title=entry.get("title"),
                dependencies=entry.get("dependencies") or [],
                started_at=entry.get("started_at"),
                finished_at=entry.get("finished_at"),
                error=entry.get("error"),
            )
        except Exception:
            continue
    return result if result else None


def _parse_team_progress(raw: Any) -> Optional[Dict[str, TeamProgressEntry]]:
    """Convert raw team_progress dict from job store to TeamProgressEntry map."""
    if not raw or not isinstance(raw, dict):
        return None
    result: Dict[str, TeamProgressEntry] = {}
    for team_id, entry in raw.items():
        if not isinstance(entry, dict):
            continue
        try:
            result[team_id] = TeamProgressEntry(
                current_phase=entry.get("current_phase"),
                progress=entry.get("progress"),
                current_task_id=entry.get("current_task_id"),
                current_microtask=entry.get("current_microtask"),
                current_microtask_phase=entry.get("current_microtask_phase"),
                phase_detail=entry.get("phase_detail"),
                current_microtask_index=entry.get("current_microtask_index"),
                microtasks_completed=entry.get("microtasks_completed"),
                microtasks_total=entry.get("microtasks_total"),
            )
        except Exception:
            continue
    return result if result else None


def _coerce_progress(value: Any) -> Optional[int]:
    """Coerce a stored progress value to an int in [0, 100], or None.

    Thin wrapper over ``shared.hitl.progress.coerce_progress`` (see it for the full
    contract). Kept as a named function on this module so callers importing
    ``_coerce_progress`` are unchanged. Unlike SE's previous local version, the shared
    helper clamps to [0, 100], so a corrupt record can no longer render an
    out-of-range progress bar.
    """
    return coerce_progress(value)


def _coerce_current_activity(value: Any) -> Optional[CurrentActivityEntry]:
    """Coerce a stored current_activity value into the response model, or None.

    Postconditions: a non-dict or a dict with malformed field values (e.g. a
    non-numeric fraction) yields None — the optional activity detail degrades,
    it never turns the whole status endpoint into a 500.
    """
    if not isinstance(value, dict):
        return None
    try:
        return CurrentActivityEntry.model_validate(value)
    except ValidationError:
        logger.warning("Malformed current_activity on job record (ignored): %r", value)
        return None


RESUMABLE_STATUSES = (
    JOB_STATUS_PENDING,
    JOB_STATUS_RUNNING,
    JOB_STATUS_AGENT_CRASH,
    JOB_STATUS_FAILED,
    # A job paused for a coding-team decision whose orchestrator thread died (e.g. server restart)
    # must be resumable after the user answers — otherwise it is stuck at waiting_for_user forever.
    "waiting_for_user",
)
RESTARTABLE_STATUSES = (
    JOB_STATUS_COMPLETED,
    JOB_STATUS_FAILED,
    JOB_STATUS_CANCELLED,
    JOB_STATUS_AGENT_CRASH,
    # A terminal success like completed: a run-team job that delegated to the coding team can end
    # here, and the dashboard offers Restart for it, so the endpoint must accept it (not 400).
    JOB_STATUS_ALREADY_COMPLETE,
)


def _real_question_options(question_data: Dict[str, Any]) -> list[dict]:
    """Return a question's selectable options, excluding the synthetic ``other`` placeholder.

    ``_convert_to_pending_questions`` inserts an ``{"id": "other"}`` entry when a
    question has no structured options; callers checking whether *real* options
    exist must skip it.

    Preconditions: ``question_data`` is a dict (its ``options`` may be absent/None).
    Postconditions: returns a list (possibly empty) of option dicts whose lowercased
        ``id`` is not ``"other"``; never raises on a missing/None ``options`` value.
    """
    return [
        o for o in (question_data.get("options") or []) if (o.get("id") or "").lower() != "other"
    ]


def _get_spec_content_for_job(data: Dict[str, Any]) -> str:
    """Get latest spec content for a job from its repo path. Returns '' if no spec file found."""
    repo_path = data.get("repo_path")
    if not repo_path:
        return ""

    repo = Path(repo_path)
    try:
        from software_engineering_team.spec_parser import get_latest_spec_content

        content = get_latest_spec_content(repo)
        return content
    except FileNotFoundError:
        return ""


# ---------------------------------------------------------------------------
# Product Analysis Endpoints
# ---------------------------------------------------------------------------


PROJECT_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")
ENV_WORKSPACE_ROOT = "WORKSPACE_ROOT"
DEFAULT_PROJECTS_DIR_NAME = "khala_projects"


def _get_projects_root() -> Path:
    """Resolve the root directory for created projects. When WORKSPACE_ROOT is set, use it/projects; else tempdir/khala_projects."""
    workspace_root_str = os.environ.get(ENV_WORKSPACE_ROOT)
    if workspace_root_str:
        root = Path(workspace_root_str).resolve() / "projects"
    else:
        root = Path(tempfile.gettempdir()) / DEFAULT_PROJECTS_DIR_NAME
    root.mkdir(parents=True, exist_ok=True)
    return root


SUPERVISOR_LOG_DIR = Path("/var/log/supervisor")
ALLOWED_SERVICES = frozenset(
    {
        "sw_api",
        "blogging_api",
        "market_research_api",
        "soc2_compliance_api",
        "social_marketing_api",
        "blog_research_api",
        "agent_provisioning_api",
        "postgresql",
        "nginx",
        "dockerd",
    }
)
