"""coding_team API — github-issue-driven run route."""

from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Any, Dict, NoReturn

from fastapi import APIRouter, HTTPException, Query

from software_engineering_team.api import coding_team_main as _main
from software_engineering_team.api.coding_team_models import (
    CheckoutRunningResponse,
    RunFromGitHubRequest,
    RunFromGitHubResponse,
)
from software_engineering_team.api.git_ops import integration_branch_for
from software_engineering_team.api.routes._common import (
    raise_if_checkout_occupied,
    resolve_github_token,
)
from software_engineering_team.github_source import (
    GitHubAPIError,
    NotAnIssueError,
    is_ready,
    issue_to_plan_input,
    pick_ready_issue,
)
from software_engineering_team.models import JobStatus
from software_engineering_team.temporal.coding_team_start_workflow import (
    start_coding_team_workflow,
)
from software_engineering_team.token_crypto import encrypt_token

logger = logging.getLogger(__name__)
router = APIRouter()


def _fail_new_job(
    job_id: str,
    http_status: int,
    error: str,
    *,
    detail: str | None = None,
    cause: Exception | None = None,
) -> NoReturn:
    """Terminalize a freshly-created job as failed and raise the matching HTTPException.

    Shared by ``post_run_from_github``'s admission block for the three ways a
    job that was already created (and so already counts as active for
    ``_running_job_for_issue``/checkout-admission purposes) can fail before a
    workflow is actually dispatched: an unresolvable base branch, an
    unresolvable base-branch head SHA, and a Temporal dispatch failure.
    Leaving the row 'pending' in any of these cases would make every retry
    409 forever with nothing left to terminalize it, and could wedge the
    checkout admission lock the caller holds. Callers are expected to log
    their own context (``logger.error``/``logger.exception``) BEFORE calling
    this, since the right log level and message differ per site.

    Preconditions:
        - ``job_id`` names a job row this request already created.
        - ``error`` is safe to disclose: it is stored on the job row, which the
          generic ``GET /api/jobs/{team}`` route echoes verbatim to any caller
          that can read the job. Pass a sanitized summary — never raw
          ``str(exception)`` text — and keep the full diagnostic in ``cause``
          and the caller's own ``logger.exception``.
        - ``detail``, when given, is the (possibly friendlier/less internal)
          HTTP response detail to use instead of ``error`` — e.g. to avoid
          echoing an internal exception's ``str()`` straight into the
          response body.
        - ``cause``, when given, is chained onto the raised ``HTTPException``
          via ``raise ... from cause``, matching the original site's own
          exception-chaining.
    Postconditions:
        - The job is marked ``FAILED`` with ``error`` and ``current_activity``
          cleared. Always raises ``HTTPException(status_code=http_status,
          detail=detail or error)`` — never returns normally.
    """
    _main.update_job(
        job_id,
        status=JobStatus.FAILED.value,
        error=error,
        current_activity=None,
    )
    raise HTTPException(status_code=http_status, detail=detail or error) from cause


@router.post("/run-from-github", response_model=RunFromGitHubResponse)
def post_run_from_github(request: RunFromGitHubRequest) -> RunFromGitHubResponse:
    """Discover (or verify) a ready GitHub issue and start a coding job for it.

    Preconditions:
        - ``request`` names an existing local checkout and provides a GitHub token
          either directly or through the configured environment.
    Postconditions:
        - A job record is created and tagged with GitHub context for the selected
          ready issue. When a token encryption key is configured, the encrypted
          token is stored on the job record too, so a later resume can re-drive
          the GitHub publish flow without the caller supplying it again.
        - The CodingTeamWorkflow is started with a GitHub payload that contains
          branch metadata (including the base branch's HEAD SHA at this
          moment, so branch prep can detect the base moving before it runs)
          but never the plaintext token.
        - A 409 is raised when a job is already running for this issue, OR when
          another active job (any issue/PR — including an address-comments
          remediation) is already using the SAME ``request.repo_path``: an
          operator-pinned checkout is shared (unnamespaced) across every
          issue/PR of that repo, so two different jobs would otherwise race on
          the same working tree for each job's entire run, not just at
          admission.
    """
    token = resolve_github_token(request)
    if not Path(request.repo_path).is_dir():
        raise HTTPException(status_code=400, detail=f"repo_path not found: {request.repo_path}")

    with _main.GitHubClient(token=token) as client:
        try:
            if request.issue_number is not None:
                issue = client.get_issue(request.owner, request.repo, request.issue_number)
                ready = is_ready(client, request.owner, request.repo, issue)
                if not ready.ready:
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            f"issue #{issue.number} blocked by sub-issues {list(ready.blocking)}"
                        ),
                    )
            else:
                picked = pick_ready_issue(client, request.owner, request.repo, label=request.label)
                if picked is None:
                    raise HTTPException(status_code=404, detail="no ready issues")
                issue, ready = picked
            default_branch = (
                None
                if request.base_branch
                else client.get_repo(request.owner, request.repo).default_branch
            )
        except NotAnIssueError as e:
            # Operator passed a PR number — that's a 400, not an upstream error.
            raise HTTPException(status_code=400, detail=str(e)) from e
        except GitHubAPIError as e:
            raise HTTPException(status_code=502, detail=f"github api error: {e}") from e

    running = _main._running_job_for_issue(request.owner, request.repo, issue.number)
    if running:
        raise HTTPException(
            status_code=409,
            detail=(
                f"job {running} already running for {request.owner}/{request.repo}#{issue.number}"
            ),
        )

    plan = issue_to_plan_input(
        issue,
        request.repo_path,
        list(ready.sub_issues),
        request.owner,
        request.repo,
    )

    # An operator-pinned repo_path is shared (unnamespaced) across every issue/PR
    # of that repo, so the issue-scoped check above cannot see a running job for
    # a DIFFERENT issue or PR (e.g. an address-comments remediation) on the SAME
    # checkout. `_checkout_admission`, keyed by the checkout path itself, closes
    # that gap — nested here, around job creation, exactly like
    # `address_github_pr_comments`'s own admission section — so this route and
    # that one serialize against each other regardless of which one is admitted
    # first.
    with _main._checkout_admission(request.repo_path):
        raise_if_checkout_occupied(request.repo_path)

        job_id = str(uuid.uuid4())
        _main.create_job(job_id=job_id, repo_path=request.repo_path, plan_input=plan.model_dump())
        job_fields: Dict[str, Any] = {
            "github_context": {
                "owner": request.owner,
                "repo": request.repo,
                "issue_number": issue.number,
                "issue_url": issue.html_url,
                "base_branch": request.base_branch,
                "remote": request.remote,
                # Persisted so a resume reconstructs the SAME cleanup decision the
                # fresh run made; without it a resumed job would default to False and
                # leak its ephemeral per-issue checkout on clean completion.
                "cleanup_checkout_on_success": request.cleanup_checkout_on_success,
            },
        }
        # Persist the token (encrypted) so a resume after the orchestrator thread dies (server restart,
        # different worker process) can re-drive the GitHub publish flow. In the standard deployment the
        # token is a per-request PAT from the credential store and the coding-team container has no
        # GITHUB_TOKEN env, so without this the job could never resume. Only OPAQUE CIPHERTEXT is stored
        # — never a usable PAT — because the raw job record is echoed verbatim by the generic
        # GET /api/jobs/{team} route. When no encryption key is configured the token is not persisted
        # and resume falls back to GITHUB_TOKEN env (or refuses); we never store plaintext.
        # Wrapped so a failure here cannot orphan the row created just above.
        # ``encrypt_token`` is contractually non-raising (it returns None on any
        # crypto/key problem), but ``update_job`` reaches the central job service
        # and CAN raise (transport error, service unavailable, store rejection).
        # An exception escaping here would leave the row 'pending' — which
        # ``_running_job_for_issue`` treats as active — so every retry for this
        # issue would 409 forever with nothing left to terminalize it, exactly
        # the failure mode the three handlers below already guard against.
        try:
            encrypted = encrypt_token(token)
            if encrypted:
                job_fields["github_token_encrypted"] = encrypted
            _main.update_job(job_id, **job_fields)
        except Exception as e:
            # Sanitized per ``_fail_new_job``'s safe-to-disclose contract: the
            # stored ``error`` is echoed verbatim by GET /api/jobs/{team}, and a
            # job-service exception's ``str()`` can carry internal host names or
            # connection strings. The full diagnostic stays in the log and the
            # chained cause.
            logger.exception("Failed to persist GitHub context for job_id=%s: %s", job_id, e)
            _fail_new_job(
                job_id,
                500,
                "failed to persist GitHub job context",
                cause=e,
            )

        base = request.base_branch or default_branch
        if not base:
            # The job row already exists (created above) and _running_job_for_issue
            # treats a pending job as active, so leaving it pending here would make
            # every retry for this issue 409 forever with nothing left to
            # terminalize it, and would also wedge the checkout admission lock
            # this route just took above. Mark it failed, same as the base-sha
            # and Temporal-dispatch failure handlers below.
            logger.error("Unable to resolve base branch for GitHub-issue run job_id=%s", job_id)
            _fail_new_job(job_id, 500, "unable to resolve base branch for GitHub-issue run")
        # Pin branch prep to the exact commit the plan above was grounded on: if
        # `base` moves between now and when the Temporal branch-prep activity
        # actually runs (queueing, retries, worker restarts), that activity must
        # detect the drift instead of silently seeding work from a different HEAD.
        sha_ok, base_sha_or_err = _main.resolve_remote_branch_sha(
            request.repo_path, request.remote, base, token
        )
        if not sha_ok:
            # The job row already exists (created above) and _running_job_for_issue
            # treats a pending job as active, so leaving it pending here would make
            # every retry for this issue 409 forever with nothing left to
            # terminalize it. Mark it failed, same as a Temporal dispatch failure.
            # The raw git output stays in the log only. ``resolve_remote_branch_sha``
            # already scrubs URL-embedded credentials and the transient auth header,
            # but that scrubbing is best-effort pattern matching over arbitrary git
            # stderr; the stored ``error`` is echoed verbatim by
            # GET /api/jobs/{team}, so it carries a fixed summary instead of
            # remote-supplied text, per ``_fail_new_job``'s safe-to-disclose contract.
            logger.error("Unable to resolve base branch head sha: %s", base_sha_or_err)
            _fail_new_job(job_id, 502, "unable to resolve base branch head sha")
        integration_branch = integration_branch_for(issue.number)
        try:
            start_coding_team_workflow(
                job_id,
                request.repo_path,
                plan.model_dump(),
                github={
                    "owner": request.owner,
                    "repo": request.repo,
                    "issue_number": issue.number,
                    "issue_title": issue.title,
                    "remote": request.remote,
                    "base": base,
                    "integration_branch": integration_branch,
                    "expected_base_sha": base_sha_or_err,
                    "cleanup_checkout_on_success": request.cleanup_checkout_on_success,
                },
            )
        except Exception as e:
            # Dispatch failed (worker not ready, start timeout, bad config). Mark
            # the freshly-created row failed so it is not orphaned in 'pending',
            # and surface a retryable error instead of an opaque 500.
            # The stored `error` is deliberately sanitized, not `str(e)`: the
            # generic `GET /api/jobs/{team}` route echoes a job's error verbatim,
            # so raw dispatch-exception text (host names, connection strings,
            # anything a Temporal client puts in its message) would reach any
            # caller that can read the job. The full diagnostic still reaches the
            # log through the logger.exception above and the chained `cause`.
            logger.exception("Coding team Temporal dispatch failed: %s", e)
            _fail_new_job(
                job_id,
                503,
                "Temporal dispatch failed (worker unavailable)",
                detail="Temporal dispatch failed (worker unavailable); job marked failed. Retry.",
                cause=e,
            )
    return RunFromGitHubResponse(job_id=job_id, issue_number=issue.number, issue_url=issue.html_url)


@router.get("/checkout/running", response_model=CheckoutRunningResponse)
def get_checkout_running(repo_path: str = Query(..., min_length=1)) -> CheckoutRunningResponse:
    """Lightweight pre-check: is ANY active job (issue run or PR remediation) already using this checkout?

    Generic counterpart to ``GET /pulls/{pr_number}/address-comments/running``'s
    ``repo_path`` half-check, usable by any caller that owns a shared, mutable
    checkout resource before touching it — e.g. the unified API's
    ``run_github_issue``, which (unlike ``address_github_pr_comments``) has no
    PR-scoped admission of its own to piggyback on.

    Preconditions:
        - ``repo_path`` is non-empty.
    Postconditions:
        - Returns the job_id of a live, non-terminal job (any kind) using this
          EXACT checkout (:func:`_main.get_running_job_on_checkout`, canonical-path
          compared), or ``running_job_id=None`` when none is running. Read-only:
          takes no lock, creates no job. Best-effort only — the same TOCTOU
          window every other admission pre-check in this codebase already lives
          with; a caller needing a stronger guarantee must still rely on the
          eventual admitting route's own checks as the authority.
    """
    sibling = _main.get_running_job_on_checkout(repo_path)
    return CheckoutRunningResponse(
        running_job_id=sibling.get("job_id") if sibling is not None else None
    )
