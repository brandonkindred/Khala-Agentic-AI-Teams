"""FastAPI app for coding_team: GET /health, POST /run, GET /status/{job_id}, GET /jobs.

This module is the thin app-assembly hub. Responsibility-focused sub-modules hold
the actual logic:

* ``_paths`` — sys.path bootstrap (run first via ``api/__init__``).
* ``models`` — Pydantic request/response schemas.
* ``state`` — run-thread registry, timing constants, answer/progress helpers.
* ``lifecycle`` — ASGI startup probe.
* ``orchestration`` — orchestrator-thread lifecycle and auto-resume.
* ``pr_review`` — PR-review execution machinery.
* ``git_ops`` — git/branch machinery and the github-hook run flow.
* ``routes/*`` — APIRouter modules grouped by concern.

Every symbol the original single-module app exposed is re-imported here so
``from coding_team.api.main import X`` and ``monkeypatch.setattr(main, "X", …)``
keep working unchanged: this module stays the single owning namespace, and the
sub-modules dereference the patched collaborators through ``main`` at call time.
"""

from __future__ import annotations

import fcntl  # noqa: F401  (patched via main.fcntl.flock in tests)
import logging
import shutil  # noqa: F401  (patched via main.shutil.rmtree in tests)
import subprocess  # noqa: F401  (patched via main.subprocess.run in tests)
import threading  # noqa: F401  (patched via main.threading.Thread/Timer in tests)

# --- External collaborators (re-exported: read/patched on ``main`` by tests and
# imported from ``main`` by the Temporal worker + service composition root) ---
from coding_team import hitl  # noqa: F401
from coding_team.activity import ActivityBridge  # noqa: F401
from coding_team.agent_status import build_agent_statuses  # noqa: F401
from coding_team.api.git_ops import (  # noqa: F401
    ACTIVE_ISSUE_CONFIG_KEY,
    RESCUE_BRANCH_PREFIX,
    _cleanup_issue_checkout,
    _clear_active_issue,
    _clear_active_issue_if_matches,
    _defer_terminal_success,
    _ephemeral_checkout_target,
    _failed_tasks,
    _fast_forward,
    _format_failed_tasks,
    _git,
    _git_auth_env,
    _has_merged_tasks,
    _is_ahead,
    _is_ephemeral_checkout_path,
    _latest_issue_rescue_ref,
    _prepare_issue_branch,
    _preserve_if_would_orphan,
    _push_branch,
    _reachable_from,
    _read_active_issue,
    _record_failure,
    _recover_dirty_tree,
    _rescue_branch_name,
    _run_with_github_hooks,
    _scrub_auth_header_values,
    _truncate_title,
    _utc_timestamp,
    _working_tree_dirty,
    _write_active_issue,
)

# --- Moved sub-module symbols (re-exported for the public/import + patch surface) ---
from coding_team.api.lifecycle import _warn_if_no_engine_provider  # noqa: F401
from coding_team.api.models import (  # noqa: F401
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
from coding_team.api.orchestration import (  # noqa: F401
    _RESUME_RECHECK_DELAY_S,
    _running_job_for_issue,
    _schedule_resume_recheck,
    _start_github_resume_thread,
    _start_hook_thread,
    _start_orchestrator_thread,
    _try_auto_resume,
    plan_from_input,
    run_orchestrator_wired,
)
from coding_team.api.pr_review import (  # noqa: F401
    _REVIEW_ADMISSION_LOCK,
    _REVIEW_GUARD_HEARTBEAT_STALE_S,
    _REVIEW_HEARTBEAT_INTERVAL_S,
    ReviewCode,
    _bisect_submit,
    _build_review_code,
    _format_questions_comment,
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
    _submit_review,
)
from coding_team.api.state import (  # noqa: F401
    _ANSWER_WAIT_HEARTBEAT_STALE_S,
    _BISECT_CONTINUATION_BODY,
    _HEARTBEAT_CLOCK_SKEW_TOLERANCE_S,
    _HTTP_UNPROCESSABLE,
    _active_run_threads,
    _answer_wait_heartbeat_fresh,
    _claim_run_thread,
    _clear_run_thread,
    _coerce_progress,
    _is_run_thread_alive,
    _register_run_thread,
    _run_thread_lock,
    _starting_run_jobs,
    _validate_answers,
)
from coding_team.clone_workspace import (  # noqa: F401
    clone_lock_path,
    is_per_issue_dir,
    is_within_ephemeral_workspace,
)
from coding_team.engine_provider import get_engine_provider  # noqa: F401
from coding_team.github_source import (  # noqa: F401
    GitHubAPIError,
    GitHubClient,
    Issue,
    NotAnIssueError,
    anchor_to_first_file,
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
from coding_team.github_source.client import _is_safe_ref  # noqa: F401
from coding_team.job_store import (  # noqa: F401
    DEFAULT_CACHE_DIR,
    RESUME_CLAIM_TTL_S,
    claim_resume,
    create_job,
    get_job,
    heartbeat_job,
    list_jobs,
    release_resume_claim,
    update_job,
)
from coding_team.job_store import submit_answers as store_submit_answers  # noqa: F401
from coding_team.models import AgentStatusEntry, CodingTeamPlanInput  # noqa: F401
from coding_team.orchestrator import run_coding_team_orchestrator  # noqa: F401
from coding_team.postgres import SCHEMA as CODE_REVIEW_SCHEMA
from coding_team.review_history_store import (  # noqa: F401
    list_reviews,
    record_review_start,
    update_review,
)
from coding_team.token_crypto import decrypt_token, encrypt_token  # noqa: F401
from shared_app import create_team_app
from shared_git.git_utils import (  # noqa: F401
    DEVELOPMENT_BRANCH,
    commit_working_tree,
    git_identity_env,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

app = create_team_app(
    service_name="coding-team",
    team_key="coding_team",
    title="Coding Team API",
    description="Tech Lead with frontend_v2/backend_v2 implementation teams and Task Graph. "
    "POST /run to start a job; poll GET /status/{job_id}.",
    version="0.1.0",
    postgres_schema=CODE_REVIEW_SCHEMA,
    on_startup=_warn_if_no_engine_provider,
)

# Mount the concern-grouped routers. Imported last so the route modules'
# ``from coding_team.api import main as _main`` binds a fully-populated hub.
from coding_team.api.routes import github, jobs, reviews  # noqa: E402
from coding_team.api.routes import hitl as hitl_routes  # noqa: E402

app.include_router(jobs.router)
app.include_router(hitl_routes.router)
app.include_router(github.router)
app.include_router(reviews.router)

# A few route handlers are also invoked directly (not only over HTTP) by sibling
# modules — e.g. the answers route returns get_status(job_id). Expose them on the
# hub so those in-process calls (``_main.get_status``) resolve.
get_status = jobs.get_status  # noqa: F811
post_run = jobs.post_run  # noqa: F811
resume_job = hitl_routes.resume_job  # noqa: F811
submit_pending_answers = hitl_routes.submit_pending_answers  # noqa: F811
