"""Temporal activities for GitHub-issue-driven coding-team hooks (branch
prep, publish, and failure/outage-notice reporting).

Deliberately its own module rather than appended to ``coding_team_workflow.py``:
the co-location note in ``system_design/hitl_pause_resume_contract.md`` is
scoped to ``run_pipeline_activity`` specifically -- that activity is
long-running, has no Temporal heartbeat coordination, and its paused/terminal
return shape is interpreted directly by ``CodingTeamWorkflow.run``'s control
flow, which is why it is co-located with the workflow it drives. The
activities in this module are short-lived and self-contained, dispatched by
their registered Temporal name regardless of which module defines them, so
they follow the repository's general activities-module convention instead
(matching ``code_review_agent``'s ``activities.py``/``workflows.py`` split)
rather than growing ``coding_team_workflow.py`` with unrelated GitHub-hook
logic.

NB: this module is imported at the top of ``coding_team_workflow.py``, which
DEFINES ``CodingTeamWorkflow`` -- the temporalio workflow sandbox re-imports
that module (and everything it imports at top level) during workflow
registration. A top-level side-effecting import here would trip the sandbox
exactly as it would in ``coding_team_workflow.py`` itself; keep every
non-trivial import inside each activity function body.
"""

from __future__ import annotations

from typing import Any

from temporalio import activity

_REQUIRED_FIELDS = ("job_id", "repo_path", "remote", "default_branch", "integration_branch")


def _require_activity_github_token(request: dict[str, Any]) -> str:
    """Resolve a GitHub token for a Temporal GitHub-hook activity.

    Preconditions:
        - ``request`` is a dict (the activity request payload).
    Postconditions:
        - Raises ``ValueError`` if ``\"token\"`` is present in ``request`` (plain-text
          tokens must not appear in Temporal activity arguments).
        - Raises ``ValueError`` if ``job_id`` is missing/falsy, the job cannot be
          loaded, or neither ``github_token_encrypted`` nor ``GITHUB_TOKEN`` yields
          a usable token. Messages name field names / reasons only — never the
          request payload, ciphertext, or plaintext secrets.
        - Returns the plaintext token for in-activity use only (never place it in
          the activity return value).
    """
    if "token" in request:
        raise ValueError(
            "github activity request must not include 'token'; "
            "resolve the token activity-side from the job record or GITHUB_TOKEN"
        )
    job_id = request.get("job_id")
    if not job_id:
        raise ValueError("github activity missing required fields: ['job_id']")

    import os

    from software_engineering_team.api import coding_team_main as _main
    from software_engineering_team.token_crypto import decrypt_token

    job = _main.get_job(job_id)
    if job is None:
        raise ValueError("github activity job_id not found")

    token = decrypt_token(job.get("github_token_encrypted")) or os.environ.get("GITHUB_TOKEN")
    if not token:
        raise ValueError("github activity has no usable GitHub token")
    return token


@activity.defn(name="coding_team_github_branch_prep")
def github_branch_prep_activity(request: dict[str, Any]) -> dict[str, Any]:
    """Prepare development + integration branches for a GitHub-issue-driven run.

    Wraps ``_prepare_issue_branch`` (``api/git_ops.py``, re-exported by
    ``api/coding_team_main.py``) as a Temporal activity, so a GitHub-issue
    workflow run can execute the same branch-recovery/continuation logic the
    thread-mode ``_run_with_github_hooks`` orchestrator calls today, on a
    worker rather than a caller thread. Not yet called by ``CodingTeamWorkflow``
    (workflow wiring is a separate, later follow-up) and does not publish
    comments (that is the sibling publish/failure-notice activity work).

    Preconditions:
        - ``request`` carries non-empty string values for ``job_id``, ``repo_path``,
          ``remote``, ``default_branch``, and ``integration_branch``. Must NOT
          include a ``token`` field -- the activity resolves the GitHub token
          from the job's ``github_token_encrypted`` or ``GITHUB_TOKEN``.
        - May also carry ``issue_number`` (Optional[int]).
        - ``repo_path`` names a git checkout the calling process can write
          to; ``remote``/``default_branch``/``integration_branch`` may be
          untrusted ref-shaped strings -- ``_prepare_issue_branch`` rejects
          unsafe refs itself, fails closed, before any git call.
    Postconditions:
        - Returns ``{"ok": bool, "error": Optional[str], "notes": list[str]}``
          -- the exact ``(ok, err, notes)`` tuple translated to dict form for
          the Temporal payload boundary. ``ok=True`` means
          ``integration_branch`` is checked out with a clean working tree;
          ``notes`` describes any recovery/continuation actions taken.
          ``ok=False`` means no uncommitted work was deleted and no
          previously-reachable commit became unreachable; ``error`` says why.
        - Raises ``ValueError`` (not a discriminated return) when ``request``
          is missing a required field -- a caller-wiring bug, not a git
          failure, so it must not be conflated with the ``ok=False`` outcome.
          The error message names only the missing field NAMES, never the
          request payload itself. Activity exception messages are recorded in
          Temporal history, so they must never include secrets.
        - Does NOT catch exceptions ``_prepare_issue_branch`` itself raises
          (e.g. a bug in a helper) -- they propagate uncaught, exactly as
          they do today through the thread-mode call site in
          ``orchestration.py``'s ``_run_with_github_hooks`` (no surrounding
          try/except there either), so Temporal's own activity
          failure/retry semantics apply instead of a silently swallowed bug.
    """
    token = _require_activity_github_token(request)
    missing = [f for f in _REQUIRED_FIELDS if not request.get(f)]
    if missing:
        raise ValueError(f"github_branch_prep_activity missing required fields: {missing!r}")
    from software_engineering_team.api.coding_team_main import _prepare_issue_branch

    ok, err, notes = _prepare_issue_branch(
        request["repo_path"],
        request["remote"],
        request["default_branch"],
        request["integration_branch"],
        token,
        issue_number=request.get("issue_number"),
    )
    return {"ok": ok, "error": err, "notes": notes}


_PUBLISH_REQUIRED_FIELDS = ("job_id", "owner", "repo", "repo_path", "issue_number")
_PUBLISH_MERGED_WORK_FIELDS = ("base", "integration_branch", "issue_title")


@activity.defn(name="coding_team_github_publish")
def github_publish_activity(request: dict[str, Any]) -> dict[str, Any]:
    """Publish a GitHub-issue-driven run's result as a Temporal activity.

    Wraps ``_finish_already_complete``/``_publish_merged_work``
    (``api/orchestration.py``) unchanged, dispatching on the job's own
    ``already_complete`` flag exactly as the thread-mode
    ``_run_with_github_hooks`` orchestrator does immediately before calling
    them: an already-complete run gets a close-recommendation comment and no
    PR; every other run publishes the merged work (fast-forward, push, draft
    PR create/reuse, comments), annotating the PR and job status when some
    tasks failed to merge. Not yet called by ``CodingTeamWorkflow`` (workflow
    wiring is a separate, later follow-up); does not itself decide whether
    publishing is warranted for a given job -- skipping publish entirely when
    nothing merged stays the caller's responsibility, exactly as it is today
    in ``_run_with_github_hooks``.

    Preconditions:
        - ``request`` carries non-empty ``job_id``, ``owner``, ``repo``,
          ``repo_path``, and ``issue_number``. Must NOT include a ``token``
          field -- the activity resolves the GitHub token from the job's
          ``github_token_encrypted`` or ``GITHUB_TOKEN``. Missing/falsy values
          raise ``ValueError`` naming only the missing field NAMES before any
          GitHub side effect runs; activity exception messages are recorded in
          Temporal history, so they must never include secrets.
        - When the job identified by ``job_id`` is not already-complete (per
          the job store's own ``already_complete`` flag), ``request`` must
          additionally carry non-empty ``base``, ``integration_branch``, and
          ``issue_title``, checked only once that branch is known and before
          any GitHub call runs; missing/falsy values raise a second, separate
          ``ValueError`` naming only those missing fields.
        - May also carry ``remote`` (defaults to ``"origin"``, matching
          ``RunFromGitHubRequest``'s own default) and
          ``cleanup_checkout_on_success`` (defaults to ``False``).
    Postconditions:
        - Delegates to ``_finish_already_complete`` or ``_publish_merged_work``
          exactly as thread mode does, then returns the job's resulting
          record (``get_job(job_id)``), or ``{"job_id": job_id, "status":
          "unknown"}`` when the job store has nothing for it -- deliberately
          the FULL record here, not the small fixed-shape summary
          ``run_pipeline_activity`` returns on a terminal state: a future
          workflow caller needs ``github_pr_url`` (and other publish-specific
          fields) that only the full record carries.
        - Does NOT catch exceptions the wrapped functions raise -- they
          propagate uncaught, exactly as they do today through
          ``_run_with_github_hooks``'s call sites (no surrounding try/except
          there either), so Temporal's own activity failure/retry semantics
          apply.
    """
    token = _require_activity_github_token(request)
    missing = [f for f in _PUBLISH_REQUIRED_FIELDS if not request.get(f)]
    if missing:
        raise ValueError(f"github_publish_activity missing required fields: {missing!r}")

    from software_engineering_team.api import coding_team_main as _main
    from software_engineering_team.api.orchestration import (
        _finish_already_complete,
        _publish_merged_work,
    )

    job_id = request["job_id"]
    num = request["issue_number"]
    job_after = _main.get_job(job_id) or {}
    already_complete = bool(job_after.get("already_complete"))

    if not already_complete:
        missing_publish = [f for f in _PUBLISH_MERGED_WORK_FIELDS if not request.get(f)]
        if missing_publish:
            raise ValueError(
                "github_publish_activity missing required fields for merged-work "
                f"publish: {missing_publish!r}"
            )

    req_obj = _main.RunFromGitHubRequest(
        owner=request["owner"],
        repo=request["repo"],
        repo_path=request["repo_path"],
        remote=request.get("remote") or "origin",
        cleanup_checkout_on_success=bool(request.get("cleanup_checkout_on_success")),
    )

    with _main.GitHubClient(token=token) as client:
        if already_complete:
            issue_obj = _main.Issue(
                number=num, title="", body="", state="open", html_url="", labels=(), id=num
            )
            _finish_already_complete(client, job_id, req_obj, issue_obj, job_after)
        else:
            issue_obj = _main.Issue(
                number=num,
                title=request["issue_title"],
                body="",
                state="open",
                html_url="",
                labels=(),
                id=num,
            )
            _publish_merged_work(
                client,
                job_id,
                req_obj,
                issue_obj,
                request["base"],
                request["integration_branch"],
                token,
            )

    return _main.get_job(job_id) or {"job_id": job_id, "status": "unknown"}


_FAILURE_NOTICE_REQUIRED_FIELDS = ("job_id", "owner", "repo", "number", "message", "kind")
_FAILURE_NOTICE_KINDS = ("failure", "outage")


@activity.defn(name="coding_team_github_failure_notice")
def github_failure_notice_activity(request: dict[str, Any]) -> dict[str, Any]:
    """Report a job failure or review outage to GitHub as a Temporal activity.

    Wraps ``_record_failure``/``_record_review_outage`` (``api/orchestration.py``)
    unchanged, dispatching on the request's own ``kind`` field: ``"failure"``
    marks the job failed and posts the raw scrubbed error as a PR/issue
    comment; ``"outage"`` marks the job failed with the terminal
    ``phase="completed"`` and posts (only when ``PR_REVIEW_POST_OUTAGE_NOTICE``
    is enabled) a fixed neutral note instead of the raw error -- the
    graceful-degradation path for a reviewer-side outage. This activity does
    not itself evaluate that gate; it delegates to ``_record_review_outage``,
    which checks it internally, so the gate's behavior lives in exactly one
    place. Not yet called by ``CodingTeamWorkflow`` (workflow wiring is a
    separate, later follow-up).

    Preconditions:
        - ``request`` carries non-empty ``job_id``, ``owner``, ``repo``,
          ``number`` (an issue or PR number -- generic name matching
          ``_safe_comment``'s own parameter, since this activity serves
          both), ``message`` (the raw error text passed as the wrapped
          functions' ``error`` argument), and ``kind``. Must NOT include a
          ``token`` field -- the activity resolves the GitHub token from the
          job's ``github_token_encrypted`` or ``GITHUB_TOKEN``. Missing/falsy
          values raise ``ValueError`` naming only the missing field NAMES
          before any GitHub/job-store side effect runs; activity exception
          messages are recorded in Temporal history, so they must never include
          secrets.
        - ``kind`` must be exactly ``"failure"`` or ``"outage"``; any other
          value raises a second, separate ``ValueError`` naming the invalid
          value.
    Postconditions:
        - ``kind="failure"`` delegates to ``_record_failure`` unchanged: the
          job (and review row, when one exists) is marked ``failed`` with
          the scrubbed error captured, ``phase`` is left untouched, and a
          comment containing the job id and the scrubbed error is always
          posted.
        - ``kind="outage"`` delegates to ``_record_review_outage`` unchanged:
          the job (and review row) is marked ``failed`` with ``phase`` set
          to the terminal ``"completed"``, and a neutral fixed-text comment
          (never the raw error) is posted iff ``PR_REVIEW_POST_OUTAGE_NOTICE``
          is enabled -- this activity does not re-check that gate itself.
        - Returns the job's resulting record (``get_job(job_id)``), or
          ``{"job_id": job_id, "status": "unknown"}`` when the job store has
          nothing for it -- matching ``github_publish_activity``'s return
          contract (the full record, not ``run_pipeline_activity``'s small
          fixed-shape terminal summary).
        - Does NOT catch exceptions the wrapped functions raise -- they
          propagate uncaught, exactly as they do today through their
          thread-mode call sites (``_run_with_github_hooks``,
          ``_publish_merged_work``, ``api/pr_review.py``'s
          ``_run_reviewer``/``_run_pr_review``), so Temporal's own activity
          failure/retry semantics apply.
    """
    token = _require_activity_github_token(request)
    missing = [f for f in _FAILURE_NOTICE_REQUIRED_FIELDS if not request.get(f)]
    if missing:
        raise ValueError(f"github_failure_notice_activity missing required fields: {missing!r}")

    kind = request["kind"]
    if kind not in _FAILURE_NOTICE_KINDS:
        raise ValueError(
            f"github_failure_notice_activity: kind must be one of {_FAILURE_NOTICE_KINDS!r}, "
            f"got {kind!r}"
        )

    from software_engineering_team.api import coding_team_main as _main
    from software_engineering_team.api.orchestration import _record_failure, _record_review_outage

    job_id = request["job_id"]

    with _main.GitHubClient(token=token) as client:
        if kind == "failure":
            _record_failure(
                client,
                request["owner"],
                request["repo"],
                request["number"],
                job_id,
                request["message"],
            )
        else:
            _record_review_outage(
                client,
                request["owner"],
                request["repo"],
                request["number"],
                job_id,
                request["message"],
            )

    return _main.get_job(job_id) or {"job_id": job_id, "status": "unknown"}
