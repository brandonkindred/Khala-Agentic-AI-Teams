"""Temporal activities for the user_agent_founder (Testing Personas) team.

The fine-grained :class:`UserAgentFounderWorkflow` drives the founder lifecycle
as a graph of durable, individually-retryable Temporal activities instead of the
old single monolithic activity that ran the whole orchestrator with blocking
``time.sleep`` poll loops buried inside. Each specialist step — begin, spec
generation, phase start, one poll tick, one answer batch, finalize, mark-failed —
is its own ``@activity.defn`` so a worker restart re-runs only the unfinished
step and every step is visible in the Temporal UI.

Reuse: the genuinely intricate logic (autonomous question answering with
retry/backoff and the 409-terminal guard, and heartbeated spec generation) is
delegated verbatim to the orchestrator helpers ``_answer_pending_questions`` /
``_generate_spec_with_heartbeat`` — the exact same code the thread path runs — so
Temporal mode and thread mode stay behavior-equivalent. Only the *loop control*
moves out of the activity and into the deterministic workflow body.

Import hygiene: top-level imports stay light (``temporalio``, typing); every
heavy import (``orchestrator``, ``store``, ``agent``, ``targets``, ``httpx``) and
any ``os.getenv`` read is lazy inside function bodies, so the temporalio workflow
sandbox that re-imports sibling modules during workflow registration is never
tripped (guarded by ``tests/test_temporal_bootstrap.py``).

Serialization boundary: every activity takes/returns JSON-native values keyed on
``run_id`` — the store, agent, and adapter are non-serializable and are rebuilt
inside each activity from the run row (the same rule the pre-decomposition
activity already followed).

Job-store status ownership (retry-safe contract, mirroring ``sales_team``):
    - RUNNING is written by ``begin_run`` (and refreshed at each phase boundary).
    - COMPLETED is written only by ``finalize_run``.
    - FAILED is written only by ``mark_failed``, which the WORKFLOW invokes from
      its catch-all after a fatal error has exhausted the activity's retries — so
      an activity never marks FAILED mid-retry (which would defeat the retry
      policy) and a user cancel (which already wrote CANCELLED) is never
      clobbered.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from temporalio import activity
from temporalio.exceptions import ApplicationError

if TYPE_CHECKING:
    import httpx

# Heartbeat timeout every long, self-heartbeating activity (spec generation,
# answer batches) is scheduled with, and the single source the beat interval
# below derives from — see :func:`_beating`. Owned here (not in workflows.py) so
# the "interval must stay well under timeout" safety margin can't drift across
# files: workflows.py imports this constant for its heartbeat_timeout kwargs
# instead of hardcoding its own value.
HEARTBEAT_TIMEOUT_S = 180.0
# Beat cadence: comfortably inside HEARTBEAT_TIMEOUT_S (clamped to at most a
# third of it) so a progressing activity always outpaces the timeout regardless
# of how HEARTBEAT_TIMEOUT_S is tuned.
_HEARTBEAT_INTERVAL_S = min(30.0, HEARTBEAT_TIMEOUT_S / 3.0)
# In-activity retry budget for persisting a just-started target job id (see
# _record_started_job_id) — absorbs a transient store blip so the activity does
# not fail and re-submit a duplicate target job on Temporal retry. 5 attempts at
# 0.5s apart (~2s total) rather than a tighter budget: the window has to survive
# a brief store hiccup (e.g. a Postgres failover), since exhausting it converts
# to a non-retryable failure that requires a human-gated /resume.
_PERSIST_RETRIES = 5
_PERSIST_BACKOFF_S = 0.5


def _http_client() -> "httpx.Client":
    """Return the process-wide pooled ``httpx.Client`` for target-team calls.

    Preconditions:
        - None.
    Postconditions:
        - Returns the shared, connection-pooled client from ``shared.http``
          (already thread-safe, env-tunable keepalive, atexit-registered
          teardown) — never constructs a private client for this team.
    """
    from shared.http import get_pooled_client

    return get_pooled_client()


def _beating(extra_beat: Callable[[], None] | None = None) -> Any:
    """Background heartbeat keeping a long, LLM-calling activity alive.

    Preconditions:
        - Called from inside a running activity body (the constructor snapshots
          the calling thread's context so the beater can reach the Temporal
          activity handle; beat errors outside an activity context — e.g. unit
          tests — are swallowed by the beater).
        - ``extra_beat``, if given, is a zero-arg callable invoked on the same
          beat alongside the Temporal activity heartbeat — folds a second
          liveness signal (e.g. the job-service heartbeat) into this one
          background thread instead of nesting a second independent beater.
    Postconditions:
        - Returns an unstarted ``BackgroundHeartbeat`` context manager; entering
          it starts the daemon beater, exiting stops and joins it.
    """
    from shared.concurrency import BackgroundHeartbeat

    def _beat() -> None:
        activity.heartbeat()
        if extra_beat is not None:
            extra_beat()

    return BackgroundHeartbeat(
        _beat,
        _HEARTBEAT_INTERVAL_S,
        name="user-agent-founder-heartbeat",
        copy_context=True,
        join_timeout=5.0,
    )


def _is_first_attempt() -> bool:
    """Whether the current activity execution is Temporal's first attempt.

    Preconditions:
        - Called from inside a running activity body.
    Postconditions:
        - Returns ``True`` iff this is the first attempt of this specific
          scheduled activity task (``activity.info().attempt == 1``) — the
          Temporal-native signal for "am I being retried", unaffected by
          domain state persisted by this or any other activity. Prefer this
          over comparing persisted run/job status for retry-dedup: status can
          already be in the "consumed" shape for reasons unrelated to a
          Temporal retry (a sibling activity's unconditional write, or a
          genuinely new workflow execution after ``/resume``), which is a
          real footgun a status comparison does not protect against.
    """
    return activity.info().attempt == 1


def _record_started_job_id(store: Any, run_id: str, phase: str, job_id: str) -> None:
    """Persist a just-started target job id to its checkpoint column, with retry.

    Preconditions:
        - ``job_id`` is the id returned by a successful target submit; ``phase``
          is ``"analysis"`` or ``"build"``.
    Postconditions:
        - The matching column (``analysis_job_id``/``se_job_id``) holds ``job_id``.
        - Retries a transient store failure up to ``_PERSIST_RETRIES`` times so the
          just-created target job id is not lost — losing it would fail the
          activity and cause a Temporal retry to submit a *second* target job.
          Re-raises the last error only if every attempt fails.
    """
    column = "analysis_job_id" if phase == "analysis" else "se_job_id"
    last_exc: Exception | None = None
    for attempt in range(_PERSIST_RETRIES):
        try:
            store.update_run(run_id, **{column: job_id})
            return
        except Exception as exc:  # noqa: BLE001 — retried below; re-raised if exhausted
            last_exc = exc
            activity.logger.warning(
                "Persist %s=%s for run %s failed (attempt %d/%d): %s",
                column,
                job_id,
                run_id,
                attempt + 1,
                _PERSIST_RETRIES,
                exc,
            )
            if attempt < _PERSIST_RETRIES - 1:
                time.sleep(_PERSIST_BACKOFF_S)
    if last_exc is None:  # pragma: no cover — unreachable given _PERSIST_RETRIES >= 1
        # Every loop exit above either returns on success or sets last_exc in the
        # except block, so this never actually fires; an explicit raise here
        # instead of `assert` so it stays correct even under `-O` (which strips
        # asserts) rather than silently trying to raise None.
        raise RuntimeError(
            f"_record_started_job_id: exhausted retries for run {run_id} with no exception captured"
        )
    raise last_exc


def _job_terminal_status(run_id: str) -> str | None:
    """Return the central job's status if it is already terminal, else ``None``.

    The single source of "is this run already done" for every activity that
    must never clobber a terminal state (a user cancel, an unrelated failure,
    or its own prior successful write) — replaces three independently
    hand-rolled ``job_store.get_job(...)`` + status-tuple comparisons with one
    call and one canonical terminal set.

    Preconditions:
        - ``run_id`` is a founder-run id (the central job row may or may not
          exist).
    Postconditions:
        - Returns the job's status string when it is CANCELLED, COMPLETED, or
          FAILED; returns ``None`` otherwise (including when the job row is
          missing — a missing job is not itself a terminal-state guard, callers
          needing that distinction check separately).
    """
    from agent_team_studio.user_agent_founder.shared import job_store

    job = job_store.get_job(run_id)
    status = job.get("status") if job is not None else None
    terminal = (
        job_store.JOB_STATUS_CANCELLED,
        job_store.JOB_STATUS_COMPLETED,
        job_store.JOB_STATUS_FAILED,
    )
    return status if status in terminal else None


# ---------------------------------------------------------------------------
# Dependency reconstruction (lazy — nothing heavy at module import)
# ---------------------------------------------------------------------------


def _store_and_run(run_id: str):
    """Return ``(store, run)`` for ``run_id`` (``run`` is ``None`` if absent).

    Preconditions:
        - ``run_id`` is a founder-run id (the row may or may not exist).
    Postconditions:
        - Returns the shared ``FounderRunStore`` and the ``StoredRun`` (or
          ``None``); no side effects.
    """
    from agent_team_studio.user_agent_founder.store import get_founder_store

    store = get_founder_store()
    return store, store.get_run(run_id)


def _agent_for(run):
    """Build a ``FounderAgent`` for ``run`` (persona-customized when set).

    Preconditions:
        - ``run`` is a non-``None`` ``StoredRun``.
    Postconditions:
        - Returns a ``FounderAgent`` seeded with the run's persona prompts when a
          persona is attached, else the default persona.
    """
    from agent_team_studio.user_agent_founder.agent import FounderAgent
    from agent_team_studio.user_agent_founder.store import get_persona_store

    persona = get_persona_store().get_persona(run.persona_id) if run.persona_id else None
    if persona is not None:
        return FounderAgent(
            system_prompt=persona.system_prompt,
            spec_generation_prompt=persona.spec_generation_prompt,
        )
    return FounderAgent()


def _adapter_for(run):
    """Resolve the target-team adapter for ``run``.

    Preconditions:
        - ``run`` is a non-``None`` ``StoredRun`` with a resolvable
          ``target_team_key``.
    Postconditions:
        - Returns a ``TargetTeamAdapter``; ``process_id``/``spec`` are threaded
          through for agentic-team targets and ignored for the static targets.
    """
    from agent_team_studio.user_agent_founder.targets import get_adapter

    return get_adapter(run.target_team_key, process_id=run.process_id, spec=run.spec_content)


def _require_run(run_id: str, where: str):
    """Load ``(store, run)`` and raise non-retryably when the run row is missing.

    Preconditions:
        - ``run_id`` is a founder-run id; ``where`` names the call site for the
          error message.
    Postconditions:
        - Returns ``(store, run)`` with a non-``None`` ``run``; a genuinely
          absent row raises a non-retryable ``ApplicationError`` (retrying a
          missing row can never succeed and must surface as a failed workflow,
          not silently no-op every downstream store write).
    """
    store, run = _store_and_run(run_id)
    if run is None:
        raise ApplicationError(f"Founder run {run_id} not found at {where}", non_retryable=True)
    return store, run


# ---------------------------------------------------------------------------
# Activities
# ---------------------------------------------------------------------------


@activity.defn(name="user_agent_founder_begin_run")
def begin_run_activity(run_id: str) -> dict[str, Any]:
    """Open the run: mark it RUNNING and return the resume/config snapshot.

    Preconditions:
        - ``run_id`` refers to a run row already created by ``/start``.
    Postconditions:
        - Missing run row → non-retryable ``ApplicationError`` (see
          :func:`_require_run`).
        - Otherwise the central job row is marked RUNNING and a JSON snapshot is
          returned carrying the deterministic decisions the workflow body needs:
          which phases to short-circuit (``skip_spec``/``skip_analysis`` from the
          checkpoint columns), the existing per-phase job ids for resume, the
          persisted ``repo_path``, display/label metadata, and the env-derived
          poll intervals + attempt/answer-retry ceilings (read here so the
          deterministic workflow never touches the environment). ``max_poll_attempts``
          is floored at 1 so a misconfigured non-positive env value can't make the
          workflow's poll loop skip every tick and immediately time out.
        - Resume breadcrumbs (``spec_content``/``repo_path`` already set) are
          gated on ``activity.info().attempt == 1`` (see :func:`_is_first_attempt`)
          so a Temporal retry of this activity (it runs under ``IO_RETRY``) does
          not duplicate them. Deliberately NOT gated on the central job's status:
          both ``/start`` and ``/resume`` set the job to RUNNING *before*
          dispatching the workflow, so on every real invocation — not just
          retries — the job would already read RUNNING, permanently suppressing
          the breadcrumbs; ``attempt`` is scoped to this specific scheduled
          activity task and is unaffected by that ordering.
    """
    from agent_team_studio.user_agent_founder import orchestrator

    store, run = _require_run(run_id, "begin_run")
    adapter = _adapter_for(run)

    first_attempt = _is_first_attempt()
    orchestrator._sync_job_status(run_id, "running", phase="starting")

    # Preserve the thread path's resume chat breadcrumbs — gated so a Temporal
    # retry of this exact activity task doesn't duplicate them.
    if first_attempt:
        if run.spec_content:
            store.add_chat_message(
                run_id, "system", "Resuming with existing spec.", "status_update"
            )
        if run.repo_path:
            store.add_chat_message(
                run_id, "system", "Resuming with existing analysis output.", "status_update"
            )

    return {
        "skip_spec": bool(run.spec_content),
        "skip_analysis": bool(run.repo_path),
        "analysis_job_id": run.analysis_job_id,
        "build_job_id": run.se_job_id,
        "repo_path": run.repo_path,
        "project_name": run.project_name or f"user-agent-founder-{run_id}",
        "target_team_key": run.target_team_key,
        "adapter_display_name": adapter.display_name,
        "analysis_poll_interval": orchestrator.ANALYSIS_POLL_INTERVAL,
        "build_poll_interval": orchestrator.EXECUTION_POLL_INTERVAL,
        # Floor at 1 so a misconfigured FOUNDER_MAX_POLL_ATTEMPTS<=0 can't make the
        # workflow's `for _ in range(max_poll_attempts)` loop skip polling entirely
        # and immediately raise "timed out" without a single poll. (The env var is
        # otherwise an un-clamped int() in orchestrator.py; the same floor could be
        # applied there for thread-mode parity, but this snapshot is where the
        # deterministic workflow consumes the value.)
        "max_poll_attempts": max(1, orchestrator.MAX_POLL_ATTEMPTS),
        "max_answer_retries": orchestrator.MAX_ANSWER_RETRIES,
    }


@activity.defn(name="user_agent_founder_generate_spec")
def generate_spec_activity(run_id: str) -> dict[str, Any]:
    """Phase 1 — generate the product spec (heartbeated) and persist it.

    Preconditions:
        - ``run_id`` refers to an existing run; the workflow only schedules this
          when ``skip_spec`` is false, but the activity is idempotent either way.
    Postconditions:
        - Missing run → non-retryable ``ApplicationError``.
        - Spec already present → returns ``{"chars", "skipped": True}`` without a
          second LLM call (crash/retry idempotency).
        - Otherwise the spec is generated via ``agent.generate_spec()`` under a
          single ``_beating()`` thread that beats BOTH the Temporal activity
          heartbeat and the job-service heartbeat (so the stale-job monitor
          doesn't reap a long generation) — one background thread servicing both
          liveness signals, rather than nesting this activity's Temporal
          heartbeat around ``orchestrator._generate_spec_with_heartbeat``'s own
          independent job-heartbeat thread (which stays thread-mode-only; the
          core ``agent.generate_spec()`` LLM call is identical either way, so
          this doesn't affect Temporal/thread-mode behavior-equivalence).
          Persisted to ``spec_content``, and a chat breadcrumb is recorded.
    """
    from agent_team_studio.user_agent_founder import orchestrator

    store, run = _require_run(run_id, "generate_spec")
    if run.spec_content:
        return {"chars": len(run.spec_content), "skipped": True}

    agent = _agent_for(run)
    adapter_name = _adapter_for(run).display_name

    store.update_run(run_id, status="generating_spec")
    orchestrator._sync_job_status(run_id, "running", phase="generating_spec")
    store.add_chat_message(run_id, "system", "Generating product specification...", "status_update")

    with _beating(extra_beat=lambda: orchestrator._heartbeat(run_id)):
        spec_content = agent.generate_spec()
    store.update_run(run_id, spec_content=spec_content)
    store.add_chat_message(
        run_id,
        "assistant",
        f"Product spec generated ({len(spec_content)} chars). "
        f"Submitting to {adapter_name} for analysis.",
        "status_update",
    )
    return {"chars": len(spec_content), "skipped": False}


@activity.defn(name="user_agent_founder_enter_phase")
def enter_phase_activity(run_id: str, phase: str, existing_job_id: str | None) -> dict[str, Any]:
    """Start one target-team phase — or resume its poll — and return its job id.

    Mirrors the start-or-resume block of ``orchestrator._run_phase``: a fresh
    phase submits to the target and records the job id; a resumed phase (a job id
    passed in, or already persisted on the checkpoint column) skips the submit but
    still transitions the run to ``polling_<phase>``, clears any stale error, and
    syncs the central job phase — so a resumed run reports the live phase instead
    of the stale ``pending``/``starting`` it was left in.

    Idempotency: the "resume" branch also covers a Temporal retry of a *fresh*
    submit. Because the checkpoint column (``analysis_job_id``/``se_job_id``) is
    re-read at entry, a retry after a prior attempt already submitted + persisted
    the job id resumes that job instead of submitting a second target job.

    Preconditions:
        - ``run_id`` refers to an existing run; ``phase`` is ``"analysis"`` or
          ``"build"``. For a fresh ``analysis`` the run's ``spec_content`` is set;
          for a fresh ``build`` the run's ``repo_path`` is the analysis→build
          handoff (a real path for the SE target, ``None`` for the agentic target
          whose ``start_build`` ignores it).
    Postconditions:
        - Unknown ``phase`` → non-retryable ``ApplicationError``.
        - Resume path (``existing_job_id`` set OR the checkpoint column already
          holds a job id): transitions the run to ``polling_<phase>`` with error
          cleared + the job phase synced, records a resume breadcrumb, and returns
          ``{"job_id": <that id>}`` without contacting the target.
        - Fresh path: ``StartFailed`` (a definite target rejection) *or* a
          non-connection ``httpx.HTTPError`` (an inconclusive transport failure
          — the target may have already accepted the submit) → non-retryable
          ``ApplicationError``. Both are treated as terminal rather than
          Temporal-retried: unlike ``StartFailed``, an ambiguous transport
          failure gives no proof the submit didn't land, so letting Temporal's
          automatic retry re-run this activity could submit a second target job
          with no way to detect or reconcile the duplicate. Failing the run
          instead requires a human-gated ``/resume``. ``httpx.ConnectError``/
          ``httpx.ConnectTimeout`` are the one exception — the TCP/TLS
          connection itself never completed, so no bytes reached the target —
          and are left to propagate as an ordinary (Temporal-retryable) error
          instead. Otherwise the target job is started, its id is persisted to the
          matching checkpoint column (``analysis_job_id``/``se_job_id``) via
          :func:`_record_started_job_id` (itself non-retryable on exhaustion, for
          the identical reason), the run transitions to ``polling_<phase>``, and
          ``{"job_id": ...}`` is returned.
    """
    import httpx

    from agent_team_studio.user_agent_founder import orchestrator
    from agent_team_studio.user_agent_founder.targets import StartFailed

    if phase not in ("analysis", "build"):
        raise ApplicationError(f"Unknown phase {phase!r}", non_retryable=True)

    store, run = _require_run(run_id, "enter_phase")
    adapter = _adapter_for(run)
    label = "Product analysis" if phase == "analysis" else f"{adapter.display_name} build"

    # Unify the workflow-passed id with the persisted checkpoint column so a
    # retry of a fresh submit (which already persisted the id) resumes rather
    # than double-submits to the target.
    persisted = run.analysis_job_id if phase == "analysis" else run.se_job_id
    resume_job_id = existing_job_id or persisted

    if resume_job_id:
        # Resume: no submit, but transition to polling + clear error so the run
        # row and central job reflect the live phase during the resumed poll. The
        # breadcrumb is gated on the status transition actually happening so a
        # Temporal retry (which re-reads status already == polling_<phase>) does
        # not add a duplicate 'Resuming …' row.
        already_polling = run.status == f"polling_{phase}"
        store.update_run(run_id, status=f"polling_{phase}", error=None)
        orchestrator._sync_job_status(run_id, "running", phase=f"polling_{phase}")
        if not already_polling:
            store.add_chat_message(
                run_id, "system", f"Resuming {label} poll (job: {resume_job_id})", "status_update"
            )
        return {"job_id": resume_job_id}

    store.update_run(run_id, status=f"submitting_{phase}")
    orchestrator._sync_job_status(run_id, "running", phase=f"submitting_{phase}")
    project_name = run.project_name or f"user-agent-founder-{run_id}"

    client = _http_client()
    try:
        if phase == "analysis":
            job_id = adapter.start_from_spec(client, project_name, run.spec_content or "")
        else:
            job_id = adapter.start_build(client, run.repo_path)
    except StartFailed as exc:
        raise ApplicationError(f"Failed to start {phase}: {exc}", non_retryable=True) from exc
    except (httpx.ConnectError, httpx.ConnectTimeout):
        # Unlike the other httpx.HTTPError subclasses below, these fail before
        # any bytes reach the target (the TCP/TLS handshake itself didn't
        # complete) — there is no ambiguity about whether the submit landed. Caught
        # only to re-raise unchanged (rather than converting to a non-retryable
        # ApplicationError like the general HTTPError case does), so Temporal wraps
        # it as an ordinary retryable failure under IO_RETRY instead of forcing a
        # non-retryable, human-gated /resume for what may be a pure network blip.
        # This except must stay ABOVE the httpx.HTTPError clause (both are HTTPError
        # subclasses; first match wins).
        raise
    except httpx.HTTPError as exc:
        # Ambiguous: the request may have reached the target before the
        # transport failed. Non-retryable so Temporal doesn't auto-resubmit;
        # see the Postconditions above. This deliberately includes
        # httpx.RemoteProtocolError / ReadTimeout / etc.: unlike the connection
        # errors above (handshake never completed → no bytes sent), a protocol
        # violation or GOAWAY can arrive *after* the request stream was already
        # transmitted, so we have no proof the submit didn't land and must
        # default to the safe, non-retryable side to avoid a duplicate job.
        raise ApplicationError(
            f"Transport error starting {phase}: {exc}", non_retryable=True
        ) from exc

    # Persist the just-started id immediately (with retry): if this were lost to a
    # transient store error the activity would fail and a Temporal retry would
    # submit a second target job. If even the retry budget is exhausted,
    # _record_started_job_id's own re-raised exception is converted to
    # non-retryable below for the same reason as the httpx.HTTPError case above.
    try:
        _record_started_job_id(store, run_id, phase, job_id)
    except Exception as exc:
        raise ApplicationError(
            f"Failed to persist {phase} job id {job_id!r} after target submit: {exc}",
            non_retryable=True,
        ) from exc
    store.update_run(run_id, status=f"polling_{phase}", error=None)
    orchestrator._sync_job_status(run_id, "running", phase=f"polling_{phase}")
    store.add_chat_message(run_id, "system", f"{label} started (job: {job_id})", "status_update")
    return {"job_id": job_id}


@activity.defn(name="user_agent_founder_poll_phase")
def poll_phase_activity(run_id: str, phase: str, job_id: str) -> dict[str, Any]:
    """Poll one target-team phase once and normalize the verdict for the workflow.

    Preconditions:
        - ``run_id`` refers to an existing run; ``phase`` is ``"analysis"`` or
          ``"build"``; ``job_id`` is the target job started for that phase.
    Postconditions:
        - Missing run / unknown phase → non-retryable ``ApplicationError``.
        - Touches the job heartbeat (so the stale-job monitor doesn't reap a
          long-polling run) and returns the normalized single-poll verdict:
          ``{"status", "poll_error", "waiting", "pending_questions", "repo_path",
          "error"}``. ``error`` is always present in the dict (``None`` when the
          target response omitted it) — callers must use ``r.get("error") or
          "unknown"``, not ``r.get("error", "unknown")``, to get the same
          "unknown" fallback the pre-decomposition orchestrator produced.
          ``pending_questions`` is shape-guarded at this HTTP boundary: a
          non-list value, or a list containing a non-dict entry, is normalized
          to ``[]`` (and ``waiting`` to ``False``) rather than reaching
          ``answer_questions_activity``, which assumes each entry is a dict. On a
          terminal ``completed`` analysis poll the analysis→build handoff
          ``repo_path`` is persisted (idempotent) and the run's success
          breadcrumb is written exactly once, gated on
          ``activity.info().attempt == 1`` (see :func:`_is_first_attempt`) —
          NOT on ``run.status``: a repo-less target's ``repo_path`` is ``None``
          both before and after (so equality against it is unusable — see
          ``AgenticTeamAdapter``, whose ``poll_analysis`` is a no-op
          pass-through), and ``run.status`` can already have moved on from
          ``polling_<phase>`` for a reason unrelated to THIS poll's own retry
          (e.g. an intervening question-answering round leaves it at
          ``answering_<phase>_questions``, which would wrongly suppress a
          genuinely-first completion breadcrumb).
    """
    from agent_team_studio.user_agent_founder import orchestrator

    store, run = _require_run(run_id, "poll_phase")
    adapter = _adapter_for(run)

    orchestrator._heartbeat(run_id)
    client = _http_client()
    if phase == "analysis":
        status_data = adapter.poll_analysis(client, job_id)
    elif phase == "build":
        status_data = adapter.poll_build(client, job_id)
    else:
        raise ApplicationError(f"Unknown phase {phase!r}", non_retryable=True)

    poll_error = status_data.get("_poll_error")
    status = status_data.get("status", "")
    repo_path = status_data.get("repo_path")

    # Shape-guard the target's pending_questions before trusting it: this activity
    # is the single normalization point between the target-team HTTP boundary and
    # answer_questions_activity, which assumes each entry is a dict (q.get("id")).
    # A malformed/non-dict entry here becomes an unhandled crash one activity
    # downstream instead of a clean poll_error; treat it as no questions instead.
    raw_questions = status_data.get("pending_questions")
    if isinstance(raw_questions, list) and all(isinstance(q, dict) for q in raw_questions):
        pending_questions = raw_questions
    else:
        pending_questions = []
        if raw_questions is not None:
            # Loud, not silent: the target sent *something* for pending_questions
            # that isn't a list-of-dicts. Discarding it (rather than crashing the
            # answer activity) is deliberate, but an operator needs to see this —
            # it usually means the target-team API contract drifted.
            activity.logger.warning(
                "Target returned malformed pending_questions (%s) for run %s %s; "
                "treating as no questions",
                type(raw_questions).__name__,
                run_id,
                phase,
            )
    waiting = bool(status_data.get("waiting_for_answers") and pending_questions)

    if phase == "analysis" and status == "completed" and not poll_error:
        store.update_run(run_id, repo_path=repo_path)
        if _is_first_attempt():
            store.add_chat_message(
                run_id, "system", "Analysis complete. Starting target-team build.", "status_update"
            )

    return {
        "status": status,
        "poll_error": poll_error,
        "waiting": waiting,
        "pending_questions": pending_questions,
        "repo_path": repo_path,
        "error": status_data.get("error"),
    }


@activity.defn(name="user_agent_founder_answer_questions")
def answer_questions_activity(
    run_id: str, phase: str, job_id: str, questions: list[dict[str, Any]]
) -> dict[str, Any]:
    """Autonomously answer + submit one batch of target-team questions.

    Preconditions:
        - ``run_id`` refers to an existing run; ``phase`` is ``"analysis"`` or
          ``"build"``; ``questions`` is the target's pending-questions batch.
    Postconditions:
        - Missing run / unknown phase → non-retryable ``ApplicationError``.
        - Delegates verbatim to ``orchestrator._answer_pending_questions`` (the
          same persona-driven answering, decision/chat recording, submit
          retry/backoff, and 409-terminal guard the thread path uses) and returns
          ``{"ok": bool}``. ``ok=False`` (a handled submission failure) is a
          normal return, not an exception, so Temporal does not retry the batch —
          the workflow tracks the failed-set counter and re-surfaces on the next
          poll, exactly like the thread path.
        - Not idempotent (it records decisions/chat and submits answers), so the
          workflow schedules it with a single attempt (no Temporal retry): a
          mid-batch crash fails the run rather than re-answering + re-submitting,
          matching the thread path where such a crash fails ``run_workflow``.
    """
    from agent_team_studio.user_agent_founder import orchestrator

    store, run = _require_run(run_id, "answer_questions")
    if phase not in ("analysis", "build"):
        raise ApplicationError(f"Unknown phase {phase!r}", non_retryable=True)
    agent = _agent_for(run)
    adapter = _adapter_for(run)

    store.update_run(run_id, status=f"answering_{phase}_questions")
    store.add_chat_message(
        run_id,
        "system",
        f"Target team has {len(questions)} question(s) during {phase}.",
        "question_received",
        metadata={"question_ids": [q.get("id", "") for q in questions]},
    )

    client = _http_client()
    if phase == "analysis":

        def _submit(answers: list[dict[str, Any]]) -> None:
            adapter.submit_analysis_answers(client, job_id, answers)
    else:

        def _submit(answers: list[dict[str, Any]]) -> None:
            adapter.submit_build_answers(client, job_id, answers)

    # Heartbeat across the per-question LLM calls so a long-but-progressing batch
    # isn't killed by the activity's start_to_close timeout (the workflow schedules
    # this with a heartbeat_timeout and no retry — a genuine hang fails fast, but
    # a batch that keeps beating runs to completion).
    with _beating():
        ok = orchestrator._answer_pending_questions(
            agent, store, run_id, job_id, questions, _submit
        )
    return {"ok": bool(ok)}


@activity.defn(name="user_agent_founder_finalize_run")
def finalize_run_activity(run_id: str) -> dict[str, Any]:
    """Record the terminal COMPLETED state — the single writer of COMPLETED.

    Preconditions:
        - ``run_id`` refers to an existing run whose build phase completed.
    Postconditions:
        - Missing run row → non-retryable ``ApplicationError`` (via
          :func:`_require_run`).
        - Central job already terminal (CANCELLED, COMPLETED, or FAILED, via
          :func:`_job_terminal_status`) → no-op, returns ``{"run_id": run_id}``
          without writing COMPLETED again. CANCELLED/FAILED covers a cancel (or
          an unrelated failure) landing between the build phase completing and
          this activity running — mirrors ``mark_failed_activity``'s "never
          clobber a terminal state" guard. COMPLETED covers a Temporal retry of
          this activity's own prior successful write — a clean no-op instead of
          a harmless-but-redundant re-write. A *missing* central job row (as
          opposed to a terminal one) is treated as not-terminal — the guard
          falls through and COMPLETED is written normally.
        - Otherwise the run row + central job are marked COMPLETED and a success
          breadcrumb is recorded exactly once; returns ``{"run_id": run_id}``.
    """
    from agent_team_studio.user_agent_founder import orchestrator

    store, _run = _require_run(run_id, "finalize")

    terminal = _job_terminal_status(run_id)
    if terminal is not None:
        activity.logger.info(
            "Founder run %s already terminal (%s) at finalize; not overwriting with COMPLETED",
            run_id,
            terminal,
        )
        return {"run_id": run_id}

    store.add_chat_message(run_id, "system", "Build completed successfully.", "status_update")
    store.update_run(run_id, status="completed")
    orchestrator._sync_job_status(run_id, "completed", phase="completed")
    return {"run_id": run_id}


@activity.defn(name="user_agent_founder_mark_failed")
def mark_failed_activity(run_id: str, error: str) -> dict[str, Any]:
    """Record the terminal FAILED state — the single writer of FAILED.

    Invoked only by the workflow's catch-all after a fatal error exhausted its
    retries, so failure marking can never defeat an activity's own retry policy.

    Preconditions:
        - ``error`` is the stringified fatal error.
    Postconditions:
        - Central job already in ANY terminal state (via :func:`_job_terminal_status`)
          → no-op, returns ``{"marked": False}``. This both preserves a user
          CANCELLED (never clobbered) and makes the activity idempotent: on a
          Temporal retry after the first attempt already wrote FAILED, the job
          is terminal so it does not re-write the status or add a duplicate
          failure breadcrumb.
        - Otherwise the run row + central job end FAILED with ``error`` recorded
          verbatim as both the run's ``error`` column and the chat breadcrumb —
          unprefixed, matching ``orchestrator._run_phase``'s original phase-
          failure messages exactly (the pre-decomposition thread path's own
          outer catch-all does prefix with "Workflow failed: ", but that path is
          reserved for genuine crashes outside any phase; ``_PhaseFailed``
          messages routed through here are already fully descriptive on their
          own, e.g. "Product analysis failed: unknown", and prefixing them again
          would only make the audit log diverge from thread mode's wording for
          the common case). Returns ``{"marked": True}``.
    """
    from agent_team_studio.user_agent_founder import orchestrator
    from agent_team_studio.user_agent_founder.store import get_founder_store

    terminal = _job_terminal_status(run_id)
    if terminal is not None:
        activity.logger.info(
            "Founder run %s already terminal (%s) at mark-failed; leaving status untouched",
            run_id,
            terminal,
        )
        return {"marked": False}

    store = get_founder_store()
    store.update_run(run_id, status="failed", error=error)
    orchestrator._sync_job_status(run_id, "failed", error=error)
    store.add_chat_message(run_id, "system", error, "status_update")
    return {"marked": True}
