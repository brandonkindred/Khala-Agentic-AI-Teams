"""Temporal activities for GitHub-issue-driven coding-team hooks (branch prep,
plus the sibling publish/failure-notice activities joining this module next).

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

_REQUIRED_FIELDS = ("repo_path", "remote", "default_branch", "integration_branch")


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
        - ``request`` carries non-empty string values for ``repo_path``,
          ``remote``, ``default_branch``, and ``integration_branch``;
          missing/falsy values raise ``ValueError`` before any git operation
          runs. May also carry ``token`` (Optional[str] -- a plain-text
          GitHub token; #3992 will replace this with activity-side
          resolution from the encrypted job record, out of scope here) and
          ``issue_number`` (Optional[int]).
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
          request payload itself (which may carry ``token``, and an activity
          exception message is recorded in Temporal history).
        - Does NOT catch exceptions ``_prepare_issue_branch`` itself raises
          (e.g. a bug in a helper) -- they propagate uncaught, exactly as
          they do today through the thread-mode call site in
          ``orchestration.py``'s ``_run_with_github_hooks`` (no surrounding
          try/except there either), so Temporal's own activity
          failure/retry semantics apply instead of a silently swallowed bug.
    """
    missing = [f for f in _REQUIRED_FIELDS if not request.get(f)]
    if missing:
        raise ValueError(f"github_branch_prep_activity missing required fields: {missing!r}")
    from software_engineering_team.api.coding_team_main import _prepare_issue_branch

    ok, err, notes = _prepare_issue_branch(
        request["repo_path"],
        request["remote"],
        request["default_branch"],
        request["integration_branch"],
        request.get("token"),
        issue_number=request.get("issue_number"),
    )
    return {"ok": ok, "error": err, "notes": notes}
