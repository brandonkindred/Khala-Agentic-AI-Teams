"""Durable, per-step Temporal workflow for the user_agent_founder team.

``UserAgentFounderWorkflow`` reproduces the founder lifecycle — begin → spec
generation → product analysis (start + durable poll loop + autonomous answers) →
target-team build (same shape) → finalize — as a graph of individually
retryable, UI-visible Temporal activities, replacing the previous single
monolithic activity that ran the whole orchestrator with blocking ``time.sleep``
poll loops buried inside. A worker restart now re-runs only the unfinished
activity, and the multi-hour analysis/build polling survives restarts because the
inter-poll waits are durable ``workflow.wait_condition`` timers, not in-activity
sleeps.

Determinism: the workflow body performs no I/O, clock, or randomness — only
``execute_activity`` scheduling, ``workflow.wait_condition`` timers, and pure
dict/string control flow (a faithful port of ``orchestrator._run_phase``'s
start→poll→answer state machine). All job-store/DB writes live in the activities.

Cancellation/observability: a ``cancel`` signal sets a flag the poll loop's
``wait_condition`` wakes on immediately (rather than waiting out the full
``poll_interval``, the way a plain ``workflow.sleep`` would), and checks
again at each remaining decision point (so a cancel short-circuits before the
next expensive activity); a ``progress`` query exposes the current
phase/attempt. This mirrors ``sales_team`` (fine-grained activities, catch-all
``mark_failed`` re-raise contract, ``start_to_close`` timeouts) and
``branding_team`` (signal + query).

Sandbox note: this module and the package ``__init__`` stay free of import-time
side effects (no ``os.getenv``, no worker boot) — the temporalio sandbox replays
them during workflow registration (guarded by ``tests/test_temporal_bootstrap``).
Poll intervals and attempt ceilings are env-derived, so they are read once by the
``begin_run`` activity and carried in its snapshot rather than read here.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from agent_team_studio.user_agent_founder.temporal import activities as _act

# LLM/agent steps (spec gen, answer batches): a single call is cheap to re-run and
# llm_service already fails over on transient provider errors, so a small bounded
# retry gives crash/transient durability without re-running the whole phase.
LLM_RETRY = RetryPolicy(
    maximum_attempts=3,
    initial_interval=timedelta(seconds=30),
    maximum_interval=timedelta(minutes=2),
    backoff_coefficient=2.0,
)
# Cheap, idempotent job-store / HTTP-poll / bookkeeping steps: retry aggressively
# so a transient store/target blip never fails the whole workflow.
IO_RETRY = RetryPolicy(
    maximum_attempts=3,
    initial_interval=timedelta(seconds=5),
    maximum_interval=timedelta(seconds=30),
    backoff_coefficient=2.0,
)
# Answering is NOT idempotent — it records decisions/chat and submits answers to
# the target, and owns its own submit retry/backoff internally. A single attempt
# (no Temporal retry) ensures a mid-batch crash fails the run instead of
# re-answering + re-submitting duplicate answers, matching the thread path.
ANSWER_RETRY = RetryPolicy(maximum_attempts=1)

# Per-attempt execution timeouts (queue wait excluded — start_to_close, never
# schedule_to_close, so a saturated worker never starves a queued bookkeeping write).
_IO_TIMEOUT = timedelta(minutes=2)
# Both spec generation and answering are long, self-heartbeating LLM calls (see
# activities._beating()); their heartbeat_timeout derives from the SAME
# activities.HEARTBEAT_TIMEOUT_S the beat interval derives from, so the two ends
# of the heartbeat contract can never drift apart across files.
_SPEC_TIMEOUT = timedelta(minutes=20)
_SPEC_HEARTBEAT_TIMEOUT = timedelta(seconds=_act.HEARTBEAT_TIMEOUT_S)
# Answering runs single-attempt (not idempotent); a generous start_to_close plus a
# heartbeat lets a large/slow question batch run to completion while a genuine hang
# still fails fast at the heartbeat_timeout instead of holding a slot for the ceiling.
_ANSWER_TIMEOUT = timedelta(minutes=30)
_ANSWER_HEARTBEAT_TIMEOUT = timedelta(seconds=_act.HEARTBEAT_TIMEOUT_S)
_FINALIZE_TIMEOUT = timedelta(minutes=5)


class _PhaseFailed(Exception):
    """A phase reached a terminal failure/cancel/timeout reported by the target.

    Routed to the workflow catch-all, which records FAILED and re-raises.
    """


class _CancelledSignal(Exception):
    """The workflow observed a cooperative ``cancel`` signal between poll ticks.

    Caught in :meth:`UserAgentFounderWorkflow.run` to end cleanly (no FAILED
    write) — the API cancel route already wrote the terminal cancel state (job
    CANCELLED; run row "failed").
    """


@workflow.defn(name="UserAgentFounderWorkflow")
class UserAgentFounderWorkflow:
    """Run one founder job as a durable sequence of per-step activities.

    Invariants:
        - Job-store status ownership lives entirely in the activities: RUNNING at
          begin, COMPLETED only in ``finalize``, FAILED only via the catch-all's
          ``mark_failed``.
        - The workflow body is deterministic — activity scheduling, timers, and
          pure control flow only; queryable state (``_phase``/``_attempt``/
          ``_cancel_requested``) is derived from replayed history.
    """

    def __init__(self) -> None:
        self._phase: str = "starting"
        self._attempt: int = 0
        self._cancel_requested: bool = False
        self._max_attempts: int = 0
        self._max_answer_retries: int = 0

    # ── Signals / queries ────────────────────────────────────────────────

    @workflow.signal
    def cancel(self) -> None:
        """Request cooperative cancellation of the run.

        Preconditions:
            - None (a Temporal signal handler takes no caller-supplied state).
        Postconditions:
            - Sets ``_cancel_requested``; the poll loops check it between ticks so
              a cancel short-circuits before the next expensive activity. The API
              cancel route also writes the terminal cancel state (job CANCELLED;
              run row "failed"), so the workflow just needs to stop. Idempotent.
        """
        self._cancel_requested = True

    @workflow.query
    def progress(self) -> dict[str, Any]:
        """Return the current progress snapshot.

        Preconditions:
            - None (read-only query; must not mutate workflow state).
        Postconditions:
            - Returns ``{phase, attempt, cancel_requested}`` reflecting the last
              phase transition and the cancel flag; no side effects.
        """
        return {
            "phase": self._phase,
            "attempt": self._attempt,
            "cancel_requested": self._cancel_requested,
        }

    # ── Entrypoint ───────────────────────────────────────────────────────

    @workflow.run
    async def run(self, run_id: str) -> dict[str, Any]:
        """Durable entrypoint: run the founder lifecycle for ``run_id``.

        Preconditions:
            - ``run_id`` refers to a founder run + central job already created by
              ``/start`` (or re-dispatched by ``/resume``/``/restart``).
        Postconditions:
            - On success the run is COMPLETED (via ``finalize``) and
              ``{"run_id": run_id}`` is returned.
            - A ``cancel`` signal ends the workflow cleanly with
              ``{"run_id", "cancelled": True}`` and no FAILED write.
            - Any fatal error records FAILED (best-effort, via ``mark_failed``)
              and re-raises, so a failed run is never reported as a succeeded
              workflow. Phases whose checkpoint columns are already set are
              short-circuited (resume parity with the thread path).
        """
        try:
            snap = await self._exec(
                _act.begin_run_activity, run_id, retry=IO_RETRY, timeout=_IO_TIMEOUT
            )
            self._max_attempts = snap["max_poll_attempts"]
            self._max_answer_retries = snap["max_answer_retries"]
            self._raise_if_cancelled()

            if not snap["skip_spec"]:
                self._phase = "generating_spec"
                await self._exec(
                    _act.generate_spec_activity,
                    run_id,
                    retry=LLM_RETRY,
                    timeout=_SPEC_TIMEOUT,
                    heartbeat=_SPEC_HEARTBEAT_TIMEOUT,
                )
            self._raise_if_cancelled()

            if not snap["skip_analysis"]:
                await self._run_phase(
                    run_id,
                    "analysis",
                    snap["analysis_job_id"],
                    snap["analysis_poll_interval"],
                    "Product analysis",
                )
            # Between phases: a cancel that lands right as analysis completes must
            # stop the build phase from submitting a fresh (expensive) target job
            # — _run_phase's own cancel checks only run inside ITS poll loop, after
            # enter_phase_activity has already been scheduled.
            self._raise_if_cancelled()

            await self._run_phase(
                run_id,
                "build",
                snap["build_job_id"],
                snap["build_poll_interval"],
                f"{snap['adapter_display_name']} build",
            )
            # Same reasoning as above: a cancel landing right as the build phase
            # completes must stop finalize_run_activity from writing COMPLETED
            # over the terminal cancel state the API route already recorded.
            self._raise_if_cancelled()

            self._phase = "finalizing"
            await self._exec(
                _act.finalize_run_activity, run_id, retry=IO_RETRY, timeout=_FINALIZE_TIMEOUT
            )
            self._phase = "completed"
            return {"run_id": run_id}
        except _CancelledSignal:
            self._phase = "cancelled"
            workflow.logger.info("UserAgentFounderWorkflow %s cancelled via signal", run_id)
            return {"run_id": run_id, "cancelled": True}
        except Exception as exc:
            await self._safe_mark_failed(run_id, str(exc))
            raise

    # ── Phase state machine (port of orchestrator._run_phase) ────────────

    async def _run_phase(
        self, run_id: str, phase: str, existing_job_id: str | None, poll_interval: int, label: str
    ) -> Any:
        """Start (or resume) one phase and poll it to a terminal state.

        Preconditions:
            - ``phase`` is ``"analysis"`` or ``"build"``; ``existing_job_id`` is
              the persisted job id for the resume path (else ``None``);
              ``poll_interval`` (seconds) and the instance ``_max_attempts`` /
              ``_max_answer_retries`` come from the ``begin_run`` snapshot.
        Postconditions:
            - Returns the phase's ``repo_path`` on ``completed`` (``None`` for
              build / for a repo-less target).
            - A target ``failed``/``cancelled``, an exhausted answer-retry budget,
              or a poll-attempt timeout raises ``_PhaseFailed`` (→ catch-all
              FAILED). A pending ``cancel`` signal raises ``_CancelledSignal``.
            - Between polls the workflow durably waits up to ``poll_interval``
              seconds, woken early the instant a ``cancel`` signal arrives (see
              the ``wait_condition`` below) rather than always waiting out the
              full interval; each pending-question batch is answered by an
              activity and, on a handled submission failure, the
              per-question-set failure counter is advanced (abort once it
              exceeds ``_max_answer_retries``) — exactly mirroring
              ``orchestrator._run_phase``.
        """
        self._phase = f"polling_{phase}"
        # Reset the queryable attempt counter so the progress query reports a
        # per-phase attempt number: without this, entering the build phase would
        # briefly surface analysis's final attempt count until build's first poll
        # tick overwrites it.
        self._attempt = 0
        # enter_phase starts a fresh phase or, on resume, transitions the run to
        # polling_<phase> (clearing stale error + syncing the job phase) — either
        # way returning the job id to poll.
        started = await self._exec(
            _act.enter_phase_activity,
            run_id,
            phase,
            existing_job_id,
            retry=IO_RETRY,
            timeout=_IO_TIMEOUT,
        )
        job_id = started["job_id"]

        # Loop-local, and correctly so under Temporal replay: the workflow method
        # is re-run deterministically against the cached activity results, so this
        # counter is rebuilt from the same sequence of answer-activity outcomes and
        # reaches the identical state — no divergence. Only state read OUTSIDE the
        # linear run (the cancel signal / progress query) needs to be an instance
        # attribute; this per-phase counter does not.
        failed_question_sets: dict[frozenset[str], int] = {}
        for attempt in range(self._max_attempts):
            self._raise_if_cancelled()
            self._attempt = attempt
            # A durable timer (same as workflow.sleep), but woken immediately on
            # cancel instead of always waiting out the full poll_interval — a
            # cancel arriving early in a long-configured interval would otherwise
            # not be observed until the timer fires.
            with contextlib.suppress(asyncio.TimeoutError):
                await workflow.wait_condition(
                    lambda: self._cancel_requested, timeout=timedelta(seconds=poll_interval)
                )
            self._raise_if_cancelled()

            r = await self._exec(
                _act.poll_phase_activity, run_id, phase, job_id, retry=IO_RETRY, timeout=_IO_TIMEOUT
            )
            if r.get("poll_error"):
                continue

            status = r.get("status", "")
            if r.get("waiting"):
                # A cancel landing right after this poll must stop the (expensive,
                # LLM-driven) autonomous-answer round from running against a
                # target the user already told the system to stop working on.
                self._raise_if_cancelled()
                pending = r.get("pending_questions") or []
                qset = frozenset(q.get("id", "") for q in pending)
                prior = failed_question_sets.get(qset, 0)
                if prior > self._max_answer_retries:
                    raise _PhaseFailed(
                        f"Answer submission failed {prior} times for {phase} questions. Aborting."
                    )
                ans = await self._exec(
                    _act.answer_questions_activity,
                    run_id,
                    phase,
                    job_id,
                    pending,
                    retry=ANSWER_RETRY,
                    timeout=_ANSWER_TIMEOUT,
                    heartbeat=_ANSWER_HEARTBEAT_TIMEOUT,
                )
                if not ans.get("ok"):
                    failed_question_sets[qset] = prior + 1
                continue

            if status == "completed":
                return r.get("repo_path")
            if status == "failed":
                # poll_phase_activity's dict always carries the "error" key (None
                # when the target omitted it), so r.get('error', 'unknown') would
                # never fall back — `or` handles both "key absent" and "present
                # but falsy" the same way dict.get's default alone cannot.
                raise _PhaseFailed(f"{label} failed: {r.get('error') or 'unknown'}")
            if status == "cancelled":
                raise _PhaseFailed(f"{label} was cancelled")

        raise _PhaseFailed(f"{label} timed out")

    # ── Helpers ──────────────────────────────────────────────────────────

    def _raise_if_cancelled(self) -> None:
        """Raise ``_CancelledSignal`` when a cancel has been signalled.

        Preconditions:
            - None (reads ``self._cancel_requested``, set only by the ``cancel``
              signal handler).
        Postconditions:
            - Raises ``_CancelledSignal`` iff a cancel was signalled; otherwise
              returns ``None`` with no state change.
        """
        if self._cancel_requested:
            raise _CancelledSignal()

    async def _exec(
        self,
        fn: Any,
        *args: Any,
        retry: RetryPolicy,
        timeout: timedelta,
        heartbeat: timedelta | None = None,
    ) -> Any:
        """Schedule ``fn`` as an activity with ``args``, timeout, and retry policy.

        Preconditions:
            - ``fn`` is one of this team's registered ``@activity.defn`` callables;
              ``timeout`` is a per-attempt ``start_to_close`` bound; ``retry`` is a
              ``RetryPolicy``; ``heartbeat`` (when set) is the activity's
              ``heartbeat_timeout`` for long, self-heartbeating activities.
        Postconditions:
            - Returns the activity's JSON result once it completes, or raises the
              activity's terminal error after its retry policy is exhausted (no
              side effects in the workflow body itself).
        """
        kwargs: dict[str, Any] = {
            "args": list(args),
            "start_to_close_timeout": timeout,
            "retry_policy": retry,
        }
        if heartbeat is not None:
            kwargs["heartbeat_timeout"] = heartbeat
        return await workflow.execute_activity(fn, **kwargs)

    async def _safe_mark_failed(self, run_id: str, error: str) -> None:
        """Best-effort FAILED write; its own failure must not mask the cause.

        Preconditions:
            - ``run_id`` refers to the run being failed; ``error`` is the
              stringified fatal cause.
        Postconditions:
            - Schedules ``mark_failed_activity`` (which is a no-op if the run was
              already cancelled); a failure of that activity is swallowed and
              logged (with its own details, so the job-store/Temporal-state
              divergence this leaves behind is diagnosable from the worker log)
              so the original pipeline error is the one that propagates.
        """
        try:
            await self._exec(
                _act.mark_failed_activity, run_id, error, retry=IO_RETRY, timeout=_IO_TIMEOUT
            )
        except Exception as mark_exc:  # noqa: BLE001 — never mask the original pipeline error
            workflow.logger.warning(
                "UserAgentFounderWorkflow %s: failed to record FAILED after error: %s",
                run_id,
                mark_exc,
            )
