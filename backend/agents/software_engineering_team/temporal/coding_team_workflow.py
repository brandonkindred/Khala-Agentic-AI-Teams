"""Temporal workflow + activity wrapping the coding team orchestrator."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from temporalio import activity, workflow
from temporalio.common import RetryPolicy

from software_engineering_team.temporal.coding_team_github_activities import (
    github_branch_prep_activity,
    github_failure_notice_activity,
    github_publish_activity,
)

# Bounded retry for short GitHub-hook activities (prep/publish/notice). Pipeline
# keeps its own long start_to_close timeout without a shared retry policy here.
_GITHUB_ACTIVITY_RETRY = RetryPolicy(
    maximum_attempts=3,
    initial_interval=timedelta(seconds=5),
    maximum_interval=timedelta(seconds=30),
    backoff_coefficient=2.0,
)


@activity.defn(name="coding_team_run_pipeline")
def run_pipeline_activity(request: dict[str, Any]) -> dict[str, Any]:
    """Run the coding-team pipeline as a Temporal activity.

    Preconditions:
        - A ``CodeEngineProvider`` is installed in THIS worker process (SE's
          ``_se_startup()`` installs it before starting this worker).
        - ``request`` is a dict whose ``repo_path``/``plan_input``/
          ``acknowledged_resume_token`` keys form a serialized ``RunRequest``
          (``coding_team_models.RunRequest`` defines exactly those three
          fields); ``plan_input`` must be non-null (the workflow executes a
          plan; a job-only request with no plan has nothing to run).
          ``request`` may also carry a top-level ``job_id`` (the row the API
          already created for the client to poll) — this is read directly via
          ``request.get("job_id")``, not a ``RunRequest`` field, so it passes
          through Pydantic's default ignore-extra-keys behavior unvalidated.
    Postconditions:
        - Runs the orchestrator wired to the job store against the request's
          ``job_id`` when supplied (the API created the row; do not create it
          again), or mints one and creates the row when absent, forwarding
          ``req.acknowledged_resume_token`` and always requesting
          ``pause_strategy="return"`` (contract doc §1) — this activity never
          blocks through a HITL pause.
        - When the orchestrator returns a non-``None`` result (a HITL gate
          paused, or a pre-work activity retry re-emitting an already-persisted
          pause — see ``run_coding_team_orchestrator``'s contract), that
          ``{"outcome": "paused", ...}`` dict is returned unchanged and
          promptly — no further job-store read, no blocking call past the
          point of pause.
        - Otherwise (the orchestrator returned ``None``, i.e. the pipeline
          reached a terminal state) returns a small fixed-shape summary --
          ``{"outcome": "completed" | "failed", "job_id": ..., "status": ...,
          "error": <optional>, "summary": <optional>}`` -- rather than the
          full job record: this activity's result becomes
          ``CodingTeamWorkflow.run``'s own return value, so an unbounded
          job-record payload (e.g. a large ``task_graph_snapshot``) risks
          exceeding Temporal's activity-result payload limit, and a retry
          triggered by an oversized payload would re-run completion logic
          against state the first attempt already changed. ``outcome`` is
          ``"completed"`` for every terminal SUCCESS status
          (``hitl.TERMINAL_SUCCESS_STATUSES``: completed, completed with
          failures, already-complete) and ``"failed"`` otherwise (failed,
          cancelled). ``error``/``summary`` are included only when the job
          record actually carries a value for them. Returns
          ``{"outcome": "unknown", "job_id": ..., "status": "unknown"}`` when
          the job row is missing or unreadable after the orchestrator run --
          the full job record remains the source of truth in the job store,
          and callers already poll ``GET /status/{job_id}`` for complete
          state. Raises with an actionable message when the worker is
          mis-wired (no provider) or the request carries no plan — instead of
          failing later, mid-run, with a generic error.
        - Safe for POST ``/run/{job_id}/answers`` (see that route) to signal a
          resume without this activity ever having blocked: the route reads
          the job's persisted ``resume_token`` and signals
          ``CodingTeamWorkflow`` directly rather than relying on a live,
          blocked thread — this activity has nothing to unblock.
    """
    import uuid

    from software_engineering_team import hitl
    from software_engineering_team.api.coding_team_main import (
        RunRequest,
        create_job,
        get_job,
        plan_from_input,
        run_orchestrator_wired,
    )
    from software_engineering_team.engine_provider import get_engine_provider

    if get_engine_provider() is None:
        raise RuntimeError(
            "coding_team Temporal worker has no CodeEngineProvider installed: this worker "
            "process never ran SE's _se_startup() hook. Call "
            "software_engineering_team.engine_provider.set_engine_provider(...) "
            "in the worker bootstrap before executing CodingTeamWorkflow."
        )
    req = RunRequest(**request)
    if not req.plan_input:
        raise ValueError(
            "CodingTeamWorkflow requires a plan_input to execute; received a job-only request "
            "with no plan."
        )
    # Reuse the API-created job row when the dispatcher supplied its id, so the
    # id the client polls is the id the orchestrator writes to. Only mint + create
    # a row when dispatched without one (self-contained callers/tests). Either way
    # run through the shared orchestrator wiring — the same path POST /run uses —
    # against the real (job_id, repo_path, plan) signature.
    supplied_job_id = request.get("job_id")
    job_id = supplied_job_id or str(uuid.uuid4())
    if not supplied_job_id:
        create_job(job_id=job_id, repo_path=req.repo_path, plan_input=req.plan_input)
    plan = plan_from_input(req.plan_input, req.repo_path)
    paused = run_orchestrator_wired(
        job_id,
        req.repo_path,
        plan,
        pause_strategy="return",
        acknowledged_resume_token=req.acknowledged_resume_token,
    )
    if paused is not None:
        return paused
    job = get_job(job_id)
    if job is None:
        return {"outcome": "unknown", "job_id": job_id, "status": "unknown"}
    status = job.get("status")
    result: dict[str, Any] = {
        "outcome": "completed" if status in hitl.TERMINAL_SUCCESS_STATUSES else "failed",
        "job_id": job_id,
        "status": status,
    }
    error = job.get("error")
    if error:
        result["error"] = error
    summary = job.get("status_text")
    if summary:
        result["summary"] = summary
    return result


@workflow.defn(name="CodingTeamWorkflow")
class CodingTeamWorkflow:
    """Durable driver for a coding-team pipeline run.

    Invariants:
        - ``self._active_resume_token`` is non-None only while this workflow
          is waiting on a pause it has detected (between noting a
          ``"paused"`` activity result and consuming the matching
          ``submit_answers`` signal for that same pause) — so
          ``submit_answers`` can tell a fresh submission for the CURRENT
          pause apart from a stale one for an already-resolved pause.
        - ``self._submitted_answers`` is non-None only in the narrow window
          between a validated ``submit_answers`` signal being delivered and
          the top of the next loop iteration in ``run``, which resets it to
          ``None`` before re-arming ``wait_condition`` — so a stale answer
          batch from one pause round can never be mistaken for a fresh one
          in the next.
        - ``self._buffered_signals`` holds at most one early-arrived answer
          batch per not-yet-activated ``resume_token`` — an entry is
          inserted only when no pause is active yet (``submit_answers``
          received a signal ahead of ``run``'s loop arming that token) and is
          always removed, whether or not it was ever consumed, the moment
          ``run`` arms that same token — so a buffered entry never outlives
          the pause round it was minted for.
    """

    def __init__(self) -> None:
        self._active_resume_token: str | None = None
        self._submitted_answers: list[dict[str, Any]] | None = None
        self._buffered_signals: dict[str, list[dict[str, Any]]] = {}

    @workflow.signal(name="submit_answers")
    def submit_answers(self, payload: dict[str, Any]) -> None:
        """Deliver a human answer batch for the current (or next) pause.

        Preconditions:
            - None enforced — ``payload`` arrives from outside the workflow
              (ultimately from an HTTP caller via ``signal_workflow_sync``), so
              this handler validates its shape defensively rather than trusting
              a precondition an external, unvalidated signal cannot guarantee.
              A well-formed payload is a dict shaped ``{"resume_token": str,
              "answers": list}`` — the wire shape fixed by
              ``system_design/hitl_pause_resume_contract.md`` §2/§3 — but a
              malformed one must not raise: an unhandled exception here fails
              the workflow task and, since Temporal replays history, would
              fail identically on every future replay, permanently stranding
              the workflow.
        Postconditions:
            - Any payload that is not a dict, or a dict without a list
              ``"answers"`` value, is ignored (returns without side effects).
            - When no pause is currently active
              (``self._active_resume_token is None``), a well-formed payload
              is treated as an early arrival for a pause ``run``'s loop has
              not armed yet (contract §2 rule 1) — the exact race
              ``run_pipeline_activity`` opened by returning
              ``{"outcome": "paused", ...}`` promptly instead of blocking: a
              client can read the persisted ``resume_token`` from the job
              record and signal before this workflow has processed the
              paused activity result. A non-empty string ``resume_token`` is
              buffered in ``self._buffered_signals``, keyed by that token, so
              ``run`` can apply it the instant it arms the matching pause;
              first submission per token wins (an already-buffered token is
              left alone, not overwritten). A payload with no usable
              ``resume_token`` while no pause is active has nothing to key a
              buffer entry on and is dropped. A mismatched-but-active token
              is never buffered (only the "no pause active yet" case is) —
              since each pause round mints a fresh unique token and the
              workflow only ever awaits one pause at a time, a token that
              does not match the currently active one can only belong to an
              already-resolved earlier round, not a legitimate future one.
            - Otherwise, validates ``payload.get("resume_token")`` against
              ``self._active_resume_token`` per the contract's §2 match rules
              2 and 3: a mismatch is ignored, not applied; and once a batch
              is accepted for the current token, a second matching-token
              signal (a double-submit, or two clients racing to answer the
              same pause) is ignored too — first submission per token wins,
              an unconditional overwrite would make which human answer
              "wins" depend on delivery order. Only a token-matching first
              submission with a list ``"answers"`` sets
              ``self._submitted_answers`` to that list, satisfying a
              ``wait_condition`` predicate of
              ``self._submitted_answers is not None``.
        """
        if not isinstance(payload, dict):
            return
        answers = payload.get("answers")
        if not isinstance(answers, list):
            return
        resume_token = payload.get("resume_token")
        if self._active_resume_token is None:
            if isinstance(resume_token, str) and resume_token:
                self._buffered_signals.setdefault(resume_token, answers)
            return
        if resume_token != self._active_resume_token:
            return
        if self._submitted_answers is not None:
            return
        self._submitted_answers = answers

    @workflow.run
    async def run(self, request: dict[str, Any]) -> dict[str, Any]:
        """Run the coding-team pipeline, looping while the activity reports a pause.

        Preconditions:
            - ``request`` is a dict whose ``repo_path``/``plan_input`` keys
              form a serialized ``RunRequest`` (see
              ``run_pipeline_activity``'s contract).
            - When supplied, ``request["github"]`` is a dict carrying the
              GitHub branch/PR coordinates needed by the GitHub activities:
              ``owner``, ``repo``, ``issue_number``, ``issue_title``,
              ``base``, and ``integration_branch``. ``remote`` defaults to
              ``"origin"`` when blank or absent.
            - Any GitHub token or credential stays outside workflow activity
              arguments. This workflow passes only repository coordinates and
              issue metadata to GitHub activities; a ``"token"`` key must
              never appear in activity args.

        Postconditions:
            - Without GitHub metadata, returns the activity's result dict.
              With GitHub metadata, prepares the integration branch before
              running the pipeline, skips the pipeline and posts a failure
              notice when branch prep reports ``ok=False``, posts a failure
              notice and re-raises when the pipeline activity raises, returns
              failed/cancelled/waiting-for-user pipeline results unchanged,
              and publishes the integration branch after successful terminal
              pipeline results.
            - ``run_pipeline_activity`` now always requests
              ``pause_strategy="return"`` and emits
              ``{"outcome": "paused", ...}`` whenever a HITL gate pauses, or
              a pre-work activity retry re-emits an already-persisted pause,
              so this method loops (executing the activity more than once)
              until a non-``"paused"`` outcome is returned; production
              behavior is no longer "call the activity once."
            - When an activity result's ``"outcome"`` key IS
              ``"paused"``, this validates ``result["resume_token"]`` is a
              non-empty string, raising ``ValueError`` (failing this workflow
              task deterministically) if not — the contract guarantees a
              paused result always carries one, so a missing/malformed value
              means the activity-side contract broke, and assigning ``None``
              would instead make ``submit_answers``'s guard silently drop
              every future signal, stranding this workflow in
              ``wait_condition`` forever with no diagnostic. Otherwise
              records it as ``self._active_resume_token``, then immediately
              applies any early signal already buffered for that exact token
              (``self._buffered_signals.pop(resume_token, None)`` — ``None``
              when nothing was buffered, otherwise the buffered ``answers``
              list, either way assigned to ``self._submitted_answers``)
              before arming ``workflow.wait_condition`` for a token-matching
              ``submit_answers`` signal to set it (see ``submit_answers``'s
              contract) — a buffered entry already satisfies that predicate,
              so the wait resolves immediately without an extra signal round
              trip. Once resolved, it sets
              ``request["acknowledged_resume_token"]`` to that same token —
              telling ``run_coding_team_orchestrator``'s re-entry check
              (``_check_pending_pause_reentry``) which persisted pause this
              invocation resolves (contract doc §1/§3) — clears both
              signal-tracking fields, re-invokes the SAME activity with the
              mutated ``request``, and pops
              ``request["acknowledged_resume_token"]`` once that call
              returns (its job is done whether or not that call consumed
              it). Repeats until a non-``"paused"`` outcome.
            - Deliberately NOT implemented here: although the activity-side
              pause payload (``pending_questions``, ``pause_kind``,
              ``pause_context``) now exists on every paused result, this
              method does not yet APPLY resolved answers into
              ``request["plan_input"]["resolved_questions"]`` /
              ``task_decision_overrides`` — resume for the entry/tech-lead
              gates instead round-trips through the job record (the
              orchestrator's ``_hydrate_resolved_from_record`` picks up
              ``submitted_answers`` on its own re-entry). Also not
              implemented: reconciling against the job record's terminal
              status while waiting (so a job cancelled out-of-band while
              paused doesn't strand this workflow in ``wait_condition``
              forever).
        """
        github = request.get("github")
        activity_timeout = timedelta(hours=4)
        github_timeout = timedelta(minutes=30)

        if isinstance(github, dict) and github:
            prep = await workflow.execute_activity(
                github_branch_prep_activity,
                {
                    "job_id": request["job_id"],
                    "repo_path": request["repo_path"],
                    "remote": github.get("remote") or "origin",
                    "default_branch": github["base"],
                    "integration_branch": github["integration_branch"],
                    "issue_number": github.get("issue_number"),
                },
                start_to_close_timeout=github_timeout,
                retry_policy=_GITHUB_ACTIVITY_RETRY,
            )
            if not prep.get("ok"):
                return await workflow.execute_activity(
                    github_failure_notice_activity,
                    {
                        "job_id": request["job_id"],
                        "owner": github["owner"],
                        "repo": github["repo"],
                        "number": github["issue_number"],
                        "message": f"branch prep failed: {prep.get('error')}",
                        "kind": "failure",
                    },
                    start_to_close_timeout=github_timeout,
                    retry_policy=_GITHUB_ACTIVITY_RETRY,
                )

        try:
            result = await workflow.execute_activity(
                run_pipeline_activity,
                request,
                start_to_close_timeout=activity_timeout,
            )
            while result.get("outcome") == "paused":
                resume_token = result.get("resume_token")
                if not isinstance(resume_token, str) or not resume_token:
                    # The contract guarantees a paused result always carries a resume_token; a
                    # missing/malformed one means the activity-side contract broke. Fail fast and
                    # deterministically here rather than assign None and let wait_condition's
                    # predicate become permanently unsatisfiable (submit_answers drops every signal
                    # while self._active_resume_token is None) -- an unresolvable hang is a much
                    # worse failure mode than an immediate, diagnosable workflow-task error.
                    raise ValueError(f"Paused activity result missing a valid resume_token: {result!r}")
                self._active_resume_token = resume_token
                self._submitted_answers = self._buffered_signals.pop(resume_token, None)
                await workflow.wait_condition(lambda: self._submitted_answers is not None)
                request["acknowledged_resume_token"] = self._active_resume_token
                self._submitted_answers = None
                self._active_resume_token = None
                result = await workflow.execute_activity(
                    run_pipeline_activity,
                    request,
                    start_to_close_timeout=activity_timeout,
                )
                request.pop("acknowledged_resume_token", None)
        except Exception as exc:
            # Contract-violation ValueError for a missing resume_token must fail the
            # workflow task without a GitHub failure notice — that path is not an
            # orchestrator/activity failure worth posting to the issue.
            if isinstance(exc, ValueError) and "missing a valid resume_token" in str(exc):
                raise
            if isinstance(github, dict) and github:
                notice_message = f"pipeline failed: {exc}" if str(exc) else "pipeline failed"
                try:
                    await workflow.execute_activity(
                        github_failure_notice_activity,
                        {
                            "job_id": request["job_id"],
                            "owner": github["owner"],
                            "repo": github["repo"],
                            "number": github["issue_number"],
                            "message": notice_message,
                            "kind": "failure",
                        },
                        start_to_close_timeout=github_timeout,
                        retry_policy=_GITHUB_ACTIVITY_RETRY,
                    )
                except Exception:
                    # Never let a notice failure mask the original pipeline exception
                    # or hang the workflow via unbounded activity retries. Log best-effort:
                    # workflow.logger requires the Temporal runtime (unit tests call
                    # ``run()`` directly), so fall back to stdlib logging.
                    try:
                        workflow.logger.exception(
                            "github_failure_notice_activity failed after pipeline error; "
                            "re-raising original"
                        )
                    except Exception:
                        logging.getLogger(__name__).exception(
                            "github_failure_notice_activity failed after pipeline error; "
                            "re-raising original"
                        )
            raise

        if isinstance(github, dict) and github:
            status = result.get("status")
            if status in ("failed", "cancelled", "waiting_for_user"):
                return result
            return await workflow.execute_activity(
                github_publish_activity,
                {
                    "job_id": request["job_id"],
                    "owner": github["owner"],
                    "repo": github["repo"],
                    "repo_path": request["repo_path"],
                    "issue_number": github["issue_number"],
                    "issue_title": github["issue_title"],
                    "base": github["base"],
                    "integration_branch": github["integration_branch"],
                    "remote": github.get("remote") or "origin",
                    "cleanup_checkout_on_success": bool(github.get("cleanup_checkout_on_success")),
                },
                start_to_close_timeout=github_timeout,
                retry_policy=_GITHUB_ACTIVITY_RETRY,
            )
        return result


WORKFLOWS = [CodingTeamWorkflow]
ACTIVITIES = [
    run_pipeline_activity,
    github_branch_prep_activity,
    github_publish_activity,
    github_failure_notice_activity,
]

# NB: no worker self-boot at import time. This module DEFINES CodingTeamWorkflow,
# so the temporalio sandbox re-imports it during workflow registration; a top-level
# ``is_temporal_enabled()`` call (os.getenv) would trip the sandbox, and an
# import-time ``start_team_worker`` races the first dispatch. Boot lives in
# ``software_engineering_team.temporal.coding_team_worker`` (invoked from SE's
# ``_se_startup()`` hook).
