"""Start the coding team Temporal workflow from synchronous API code.

Thin wrapper over ``shared.temporal.start_workflow_sync`` (the shared sync→async
bridge, which polls for the worker's Temporal client to become ready before
dispatching). We deliberately do NOT use ``shared.temporal.run_team_job`` here:
it creates its own job row and sets ``status=running`` itself, which would
collide with the API's ``create_job`` and the activity-owned status bookkeeping.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from shared.temporal import execute_workflow_sync, start_workflow_sync
from software_engineering_team.temporal.coding_team_constants import TASK_QUEUE, WORKFLOW_ID_PREFIX
from software_engineering_team.temporal.coding_team_workflow import CodingTeamWorkflow

logger = logging.getLogger(__name__)

_COMMENT_WORKFLOW_TIMEOUT_S = 4 * 60 * 60


def _contains_token_key(value: Any) -> bool:
    """True iff ``value`` (recursively) contains a dict with a ``"token"`` key.

    A plain top-level ``"token" in github`` check only catches a token stored
    directly on the ``github`` dict — one nested under a sub-dict (e.g.
    ``github["auth"]["token"]``) would pass that check and get serialized into
    the Temporal workflow's durable event history, exactly the leakage the
    no-token contract exists to prevent.

    Postconditions:
        - Returns True iff any dict reachable from ``value`` (through nested
          dicts, lists, or tuples) has a key literally equal to ``"token"``.
    """
    if isinstance(value, dict):
        return any(k == "token" or _contains_token_key(v) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return any(_contains_token_key(v) for v in value)
    return False


def _workflow_id(job_id: str) -> str:
    """The Temporal workflow id for ``job_id`` — one shared spelling for both dispatchers."""
    return f"{WORKFLOW_ID_PREFIX}{job_id}"


def _build_workflow_payload(
    job_id: str, repo_path: str, plan_input: Optional[Dict[str, Any]], github: Optional[Dict[str, Any]]
) -> Dict[str, Any]:
    """Build the ``CodingTeamWorkflow.run`` argument dict shared by both dispatchers.

    Postconditions:
        - Returns ``{"job_id", "repo_path", "plan_input"}`` plus ``"github"``
          when ``github`` is truthy (omitted entirely when falsy/``None``, so a
          caller with no GitHub context doesn't send a spurious empty dict).
    """
    payload: Dict[str, Any] = {
        "job_id": job_id,
        "repo_path": repo_path,
        "plan_input": plan_input,
    }
    if github:
        payload["github"] = github
    return payload


def start_coding_team_workflow(
    job_id: str,
    repo_path: str,
    plan_input: Optional[Dict[str, Any]],
    github: Optional[Dict[str, Any]] = None,
) -> None:
    """Start ``CodingTeamWorkflow`` for a coding-team job.

    Preconditions:
        - ``job_id`` is a non-empty str whose job row already exists (the API
          called ``create_job`` before dispatching).
        - ``repo_path`` is a non-empty str; ``plan_input`` is a JSON-serializable
          plan dict (a run with no plan has nothing to execute).
        - ``github``, when provided, is a dict of GitHub-issue run metadata for
          the workflow (owner/repo/issue/base/integration_branch/...). It must
          not contain a plaintext token — activities resolve tokens activity-side.
    Postconditions:
        - A workflow with id ``coding_team-<job_id>`` is started on the coding
          team task queue (fire-and-forget; the caller polls
          ``GET /status/{job_id}``). When ``github`` is a non-empty dict it is
          included on the payload under ``"github"``; otherwise that key is
          omitted. Raises ``RuntimeError`` if the worker's Temporal client never
          becomes available within the wait window.
    """
    assert job_id, "start_coding_team_workflow requires a non-empty job_id"
    assert repo_path, "start_coding_team_workflow requires a non-empty repo_path"
    if github:
        assert not _contains_token_key(github), "github workflow payload must not include a token"
    payload = _build_workflow_payload(job_id, repo_path, plan_input, github)
    workflow_id = _workflow_id(job_id)
    start_workflow_sync(
        CodingTeamWorkflow.run,
        payload,
        workflow_id=workflow_id,
        task_queue=TASK_QUEUE,
    )
    logger.info("Started CodingTeamWorkflow id=%s", workflow_id)


def execute_coding_team_workflow(
    job_id: str,
    repo_path: str,
    plan_input: Optional[Dict[str, Any]],
    github: Dict[str, Any],
) -> Dict[str, Any]:
    """Run one coding-team workflow and wait for its terminal result.

    This is the completion-aware counterpart to :func:`start_coding_team_workflow`.
    It is used by review-comment remediation because a thread must not be replied
    to or resolved merely because Temporal accepted a workflow start.

    Preconditions:
        - ``job_id`` is a non-empty str, unique per review comment, naming an
          existing child job.
        - ``repo_path`` is a non-empty str.
        - ``github`` is a REQUIRED ``dict`` of GitHub PR/comment metadata and
          must not contain a plaintext token; activities resolve credentials
          from the child job's encrypted token or ``GITHUB_TOKEN`` instead.
        - ``plan_input``, when not ``None``, is a JSON-serializable plan dict
          (it is sent verbatim as a Temporal workflow argument).
    Postconditions:
        - Blocks until ``CodingTeamWorkflow`` reaches a terminal result and returns
          that result. A pause remains durable in Temporal and is resumed through
          the normal answer-signal path.
        - Blocking past ``_COMMENT_WORKFLOW_TIMEOUT_S`` (the client-side wait
          window) does NOT surface as failure: this caller reattaches to the
          same still-running workflow (``reattach_on_timeout=True``) and keeps
          waiting rather than reporting a terminal failure while the durable
          workflow may later succeed and push code with nobody watching. The
          caller is a per-comment background-thread worker (see
          ``address_comments._dispatch_implementation``), which can afford to
          keep blocking — there is no request deadline to respect here.
    Raises:
        ValueError: ``job_id``/``repo_path`` are empty, ``github`` is not a
            dict, or ``github`` contains a ``"token"`` key at any nesting
            depth (see :func:`_contains_token_key`).
        RuntimeError: ``CodingTeamWorkflow.run`` returned a non-dict result.
        Exception: Any other exception ``execute_workflow_sync`` itself raises
            (a Temporal RPC error, the workflow's own failure exception, a
            cancellation, etc.) propagates unchanged — this function does not
            catch or wrap it. Callers such as the per-comment background
            worker must be prepared for these, not just the two explicit
            raises above.
    """
    if not job_id:
        raise ValueError("execute_coding_team_workflow requires a non-empty job_id")
    if not repo_path:
        raise ValueError("execute_coding_team_workflow requires a non-empty repo_path")
    if not isinstance(github, dict):
        raise ValueError("execute_coding_team_workflow requires github to be a dict")
    if _contains_token_key(github):
        raise ValueError("github workflow payload must not include a token")
    payload = _build_workflow_payload(job_id, repo_path, plan_input, github)
    workflow_id = _workflow_id(job_id)
    logger.info(
        "Executing CodingTeamWorkflow id=%s (timeout_s=%s)", workflow_id, _COMMENT_WORKFLOW_TIMEOUT_S
    )
    result = execute_workflow_sync(
        CodingTeamWorkflow.run,
        payload,
        workflow_id=workflow_id,
        task_queue=TASK_QUEUE,
        execute_timeout_s=_COMMENT_WORKFLOW_TIMEOUT_S,
        reattach_on_timeout=True,
    )
    logger.info("CodingTeamWorkflow id=%s reached terminal result", workflow_id)
    if not isinstance(result, dict):
        raise RuntimeError("CodingTeamWorkflow returned a non-object result")
    return result
