"""Temporal workflow + activity wrapping the coding team orchestrator."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from temporalio import activity, workflow


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
          again), or mints one and creates the row when absent. Returns the
          final job snapshot as a dict, or a minimal synthetic
          ``{"job_id": ..., "status": "unknown"}`` dict when the job row is
          missing or unreadable after the orchestrator run. Raises with an
          actionable message when the worker is mis-wired (no provider) or
          the request carries no plan — instead of failing later, mid-run,
          with a generic error.
        - ``req.acknowledged_resume_token`` is NOT yet forwarded into
          ``run_orchestrator_wired`` or consulted by any re-entry check — the
          orchestrator does not yet mint or persist a ``resume_token`` per
          pause for it to match against, so there is nothing to check it
          against today. Declaring the field on ``RunRequest`` only stops it
          from being silently dropped by Pydantic before that consumer
          exists; wiring it through is separate, not-yet-implemented
          activity-side work (contract §1, sibling issues #3987/#3988).
    """
    import uuid

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
    run_orchestrator_wired(job_id, req.repo_path, plan)
    return get_job(job_id) or {"job_id": job_id, "status": "unknown"}


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
    """

    def __init__(self) -> None:
        self._active_resume_token: str | None = None
        self._submitted_answers: list[dict[str, Any]] | None = None

    @workflow.signal(name="submit_answers")
    def submit_answers(self, payload: dict[str, Any]) -> None:
        """Deliver a human answer batch for the current pause (wakes ``wait_condition``).

        Preconditions:
            - ``payload`` is a dict shaped ``{"resume_token": str, "answers":
              list}`` — the wire shape fixed by
              ``system_design/hitl_pause_resume_contract.md`` §2/§3.
        Postconditions:
            - Validates ``payload["resume_token"]`` against
              ``self._active_resume_token`` per the contract's §2 match rules
              2 and 3: a mismatch — including no pause being active yet
              (``self._active_resume_token is None``) — is ignored, not
              applied; and once a batch is accepted for the current token, a
              second matching-token signal (a double-submit, or two clients
              racing to answer the same pause) is ignored too — first
              submission per token wins, an unconditional overwrite would
              make which human answer "wins" depend on delivery order. Only
              a token-matching first submission sets
              ``self._submitted_answers`` to ``payload["answers"]``,
              satisfying a ``wait_condition`` predicate of
              ``self._submitted_answers is not None``.
            - Deliberately NOT implemented here: buffering a signal that
              arrives before ``self._active_resume_token`` is set (the
              contract's §2 rule 1, ``self._buffered_signals``) — such a
              signal is simply dropped by this skeleton. Today the pause
              loop in ``run`` is dead code (no activity ever returns
              ``"outcome": "paused"``), so this gap cannot manifest yet; it
              is deferred to the sibling reconciliation-loop issue (#3988),
              which has the real activity-side pause payload to buffer
              against.
        """
        if self._active_resume_token is None:
            return
        if payload.get("resume_token") != self._active_resume_token:
            return
        if self._submitted_answers is not None:
            return
        self._submitted_answers = payload["answers"]

    @workflow.run
    async def run(self, request: dict[str, Any]) -> dict[str, Any]:
        """Run the coding-team pipeline, looping while the activity reports a pause.

        Preconditions:
            - ``request`` is a dict whose ``repo_path``/``plan_input`` keys
              form a serialized ``RunRequest`` (see
              ``run_pipeline_activity``'s contract).

        Postconditions:
            - Returns the activity's result dict. ``run_pipeline_activity``
              does not emit ``{"outcome": "paused", ...}`` today (activity-side
              pause detection is separate, not-yet-implemented work), so in
              current production behavior this executes the activity exactly
              once and returns its result immediately — identical to before
              this change.
            - When a future activity result's ``"outcome"`` key IS
              ``"paused"``, this instead records the pause's
              ``resume_token`` as ``self._active_resume_token``, resets
              ``self._submitted_answers`` to ``None``, and waits on
              ``workflow.wait_condition`` for a token-matching
              ``submit_answers`` signal to set it (see ``submit_answers``'s
              contract). Once resolved, it sets
              ``request["acknowledged_resume_token"]`` to that same token —
              telling a future activity-side consumer which persisted pause
              this invocation resolves (contract doc §1/§3) — clears both
              signal-tracking fields, re-invokes the SAME activity with the
              mutated ``request``, and pops
              ``request["acknowledged_resume_token"]`` once that call
              returns (its job is done whether or not that call consumed
              it). Repeats until a non-``"paused"`` outcome.
            - Deliberately NOT implemented here (deferred to #3988, the
              sibling reconciliation-loop issue, where the real
              activity-side pause payload — ``pending_questions``, a
              ``pause_kind``, etc. — will exist to apply them against):
              applying resolved answers into
              ``request["plan_input"]["resolved_questions"]`` /
              ``task_decision_overrides``, and reconciling against the job
              record's terminal status while waiting (so a job cancelled
              out-of-band while paused doesn't strand this workflow in
              ``wait_condition`` forever).
        """
        result = await workflow.execute_activity(
            run_pipeline_activity,
            request,
            start_to_close_timeout=timedelta(hours=4),
        )
        while result.get("outcome") == "paused":
            self._active_resume_token = result.get("resume_token")
            self._submitted_answers = None
            await workflow.wait_condition(lambda: self._submitted_answers is not None)
            request["acknowledged_resume_token"] = self._active_resume_token
            self._submitted_answers = None
            self._active_resume_token = None
            result = await workflow.execute_activity(
                run_pipeline_activity,
                request,
                start_to_close_timeout=timedelta(hours=4),
            )
            request.pop("acknowledged_resume_token", None)
        return result


WORKFLOWS = [CodingTeamWorkflow]
ACTIVITIES = [run_pipeline_activity]

# NB: no worker self-boot at import time. This module DEFINES CodingTeamWorkflow,
# so the temporalio sandbox re-imports it during workflow registration; a top-level
# ``is_temporal_enabled()`` call (os.getenv) would trip the sandbox, and an
# import-time ``start_team_worker`` races the first dispatch. Boot lives in
# ``software_engineering_team.temporal.coding_team_worker`` (invoked from SE's
# ``_se_startup()`` hook).
