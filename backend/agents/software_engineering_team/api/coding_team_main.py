"""FastAPI app for coding_team: route hub mounting jobs, hitl, github, and reviews routers.

This module is the thin app-assembly hub. Responsibility-focused sub-modules hold
the actual logic:

* ``models`` — Pydantic request/response schemas.
* ``state`` — run-thread registry, timing constants, answer/progress helpers.
* ``lifecycle`` — ASGI startup probe.
* ``orchestration`` — orchestrator wiring and the github-hook run flow.
* ``pr_review`` — PR-review execution machinery.
* ``git_ops`` — git/branch machinery and the github-hook run flow.
* ``routes/*`` — APIRouter modules grouped by concern.

Every symbol the original single-module app exposed is re-imported here so
``from software_engineering_team.api.coding_team_main import X`` and
``monkeypatch.setattr(main, "X", …)`` keep working unchanged: this module stays
the single owning namespace, and the sub-modules dereference the patched
collaborators through ``main`` at call time.

This module's own ``app`` (a standalone FastAPI app assembled purely from
coding_team's routers) is used directly by this package's own tests
(``TestClient(api.app)``). In production, ``software_engineering_team.api.main``
mounts the same router objects onto SE's single app instead of serving this one.
"""

from __future__ import annotations

import fcntl  # noqa: F401  (patched via main.fcntl.flock in tests)
import logging
import shutil  # noqa: F401  (patched via main.shutil.rmtree in tests)
import subprocess  # noqa: F401  (patched via main.subprocess.run in tests)
import threading  # noqa: F401  (patched via main.threading.Thread/Timer in tests)

from shared.app import create_team_app
from shared.git.git_utils import (  # noqa: F401
    DEVELOPMENT_BRANCH,
    commit_working_tree,
    git_identity_env,
)

# --- External collaborators (re-exported: read/patched on ``main`` by tests and
# imported from ``main`` by the Temporal worker + service composition root) ---
from software_engineering_team import hitl  # noqa: F401
from software_engineering_team.activity import ActivityBridge  # noqa: F401
from software_engineering_team.agent_status import build_agent_statuses  # noqa: F401

# --- Moved sub-module symbols (re-exported for the public/import + patch surface) ---
from software_engineering_team.api.coding_team_lifecycle import (  # noqa: F401
    _startup,
    _warn_if_no_engine_provider,
)
from software_engineering_team.api.coding_team_models import (  # noqa: F401
    AnswerSubmission,
    JobListItem,
    PendingQuestion,
    QuestionOption,
    ReviewPrRequest,
    ReviewPrResponse,
    ReviewRunItem,
    RunFromGitHubRequest,
    RunFromGitHubResponse,
    RunRequest,
    RunResponse,
    StatusResponse,
    SubmitAnswersRequest,
)
from software_engineering_team.api.coding_team_state import (  # noqa: F401
    _BISECT_CONTINUATION_BODY,
    _HEARTBEAT_CLOCK_SKEW_TOLERANCE_S,
    _HTTP_UNPROCESSABLE,
    _claim_run_thread,
    _clear_run_thread,
    _coerce_progress,
    _is_run_thread_alive,
    _register_run_thread,
    _run_thread_lock,
    _starting_run_jobs,
    _validate_answers,
)
from software_engineering_team.api.git_ops import (  # noqa: F401
    ACTIVE_ISSUE_CONFIG_KEY,
    RESCUE_BRANCH_PREFIX,
    _cleanup_issue_checkout,
    _clear_active_issue,
    _clear_active_issue_if_matches,
    _ephemeral_checkout_target,
    _fast_forward,
    _git,
    _git_auth_env,
    _is_ahead,
    _is_ephemeral_checkout_path,
    _latest_issue_rescue_ref,
    _prepare_issue_branch,
    _preserve_if_would_orphan,
    _push_branch,
    _reachable_from,
    _read_active_issue,
    _recover_dirty_tree,
    _rescue_branch_name,
    _scrub_auth_header_values,
    _utc_timestamp,
    _working_tree_dirty,
    _write_active_issue,
)
from software_engineering_team.api.orchestration import (  # noqa: F401
    _defer_terminal_success,
    _failed_tasks,
    _format_failed_tasks,
    _has_merged_tasks,
    _record_failure,
    _record_review_outage,
    _run_with_github_hooks,
    _running_job_for_issue,
    _truncate_title,
    plan_from_input,
    run_orchestrator_wired,
)
from software_engineering_team.api.pr_review import (  # noqa: F401
    _REVIEW_ADMISSION_LOCK,
    _REVIEW_GUARD_HEARTBEAT_STALE_S,
    _REVIEW_HEARTBEAT_INTERVAL_S,
    ReviewCode,
    _build_review_code,
    _infer_review_language,
    _pr_review_admission,
    _review_author,
    _review_job_heartbeat_live,
    _run_pr_review,
    _run_pr_review_body,
    _running_review_for_pr,
    _running_sibling_on_checkout,
    _safe_comment,
    _start_pr_review_thread,
)
from software_engineering_team.api.pr_review_issues import (  # noqa: F401
    MultipleIssueCreationErrors,
    RepoMismatchError,
    ReviewNotFoundError,
    create_review_issues,
)
from software_engineering_team.clone_workspace import (  # noqa: F401
    clone_lock_path,
    is_per_issue_dir,
    is_within_ephemeral_workspace,
)
from software_engineering_team.coding_team_orchestrator import (
    run_coding_team_orchestrator,  # noqa: F401
)
from software_engineering_team.engine_provider import get_engine_provider  # noqa: F401
from software_engineering_team.github_source import (  # noqa: F401
    GitHubAPIError,
    GitHubClient,
    Issue,
    NotAnIssueError,
    build_review_body,
    choose_event,
    inline_comment_to_timeline_body,
    is_ready,
    issue_to_plan_input,
    map_issues_to_comments,
    parse_valid_lines,
    pick_ready_issue,
    render_annotated_hunks,
    scrub_token_from_text,
    split_review_comments,
)
from software_engineering_team.github_source.client import _is_safe_ref  # noqa: F401
from software_engineering_team.github_source.review_submit import (  # noqa: F401
    _bisect_submit,
    _submit_review,
)
from software_engineering_team.hitl import _format_questions_comment  # noqa: F401
from software_engineering_team.job_store import (  # noqa: F401
    DEFAULT_CACHE_DIR,
    create_job,
    get_job,
    heartbeat_job,
    list_jobs,
    update_job,
)
from software_engineering_team.job_store import (
    append_submitted_answers as store_append_submitted_answers,  # noqa: F401
)
from software_engineering_team.job_store import (
    submit_answers as store_submit_answers,  # noqa: F401
)
from software_engineering_team.models import (  # noqa: F401
    AgentStatusEntry,
    CodingTeamPlanInput,
)
from software_engineering_team.postgres import SCHEMA as SE_POSTGRES_SCHEMA
from software_engineering_team.review_history_store import (  # noqa: F401
    get_review,
    get_review_transcript,
    list_reviews,
    record_review_start,
    update_review,
)
from software_engineering_team.token_crypto import (  # noqa: F401
    decrypt_token,
    encrypt_token,
)

logger = logging.getLogger(__name__)

app = create_team_app(
    service_name="coding-team",
    team_key="coding_team",
    title="Coding Team API",
    description="Tech Lead with frontend_v2/backend_v2 implementation teams and Task Graph. "
    "POST /run to start a job; poll GET /status/{job_id}.",
    version="0.1.0",
    # ``code_review_runs`` now lives in SE's merged schema (see
    # software_engineering_team/postgres/__init__.py); registration is
    # idempotent, so re-registering it here for this module's own standalone
    # ``app`` (used directly by this package's tests) is harmless.
    postgres_schema=SE_POSTGRES_SCHEMA,
    on_startup=_startup,
)

# Mount the concern-grouped routers. Imported last so the route modules'
# ``from software_engineering_team.api import coding_team_main as _main`` binds
# a fully-populated hub.
from software_engineering_team.api.routes import coding_team_hitl as hitl_routes  # noqa: E402
from software_engineering_team.api.routes import coding_team_jobs as jobs  # noqa: E402
from software_engineering_team.api.routes import github, issue_grooming, reviews  # noqa: E402
from software_engineering_team.api.routes._common import (  # noqa: E402
    register_job_service_unavailable_handlers,
)

register_job_service_unavailable_handlers(app)

app.include_router(jobs.router)
app.include_router(hitl_routes.router)
app.include_router(github.router)
app.include_router(issue_grooming.router)
app.include_router(reviews.router)

# A few route handlers are also invoked directly (not only over HTTP) by sibling
# modules — e.g. the answers route returns get_status(job_id). Expose them on the
# hub so those in-process calls (``_main.get_status``) resolve.
get_status = jobs.get_status
post_run = jobs.post_run
resume_job = hitl_routes.resume_job
submit_pending_answers = hitl_routes.submit_pending_answers
