"""Temporal workflow + per-step activities for the job matching team.

The scan pipeline is decomposed into individually annotated activities —
prepare-run, build-queries, scan, rank, finalize (plus a terminal failure
bookkeeper) — orchestrated by :class:`JobMatchingWorkflow`. Each phase is a
durable, independently-retryable Temporal activity, so a worker crash resumes
from the last completed phase instead of re-running the whole scan. Every phase
maps onto the same specialist agent / store call the thread-mode
``JobMatchingOrchestrator`` makes, so both execution modes stay behaviourally
identical.

Kept in its own module (separate from the package ``__init__``) so the
temporalio workflow sandbox can re-import the workflow class without executing
any non-deterministic top-level code (e.g. ``os.getenv``, worker bootstrap).
Every heavy/team import therefore lives inside an activity function body, and
only JSON-native values cross the activity boundary — the shared data converter
has no pydantic support, so each activity reconstructs its models
(``Model.model_validate``) and returns ``.model_dump(mode="json")``.

The legacy monolithic :func:`run_scan_activity` is retained because workflows
started before this decomposition still schedule it: the workflow gates the new
phase sequence behind ``workflow.patched`` and replays those older histories on
the single-activity path so their replay stays deterministic until they drain.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from temporalio import activity, workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

# Bounded so a non-idempotent phase can't retry-storm on a crash. A phase that
# raises a deterministic (business) failure exhausts these attempts and then
# bubbles to the workflow, which records the run + job FAILED via
# ``fail_scan_activity`` — the same terminal state the monolith recorded inline.
# Interval/backoff are set explicitly rather than inherited from the SDK default
# so the retry cadence is version-independent.
#
# Trade-off: this means a deterministic failure (e.g. a malformed profile that
# always breaks a phase) now costs up to 3 attempts instead of the monolith's
# single try-and-record. The 1s initial interval (vs. the 30s used by sibling
# Temporal teams that did the same monolith-to-phases split) is a deliberate
# choice to bound that cost — 3 attempts at 1s/2s/4s total ~7s of extra delay
# and LLM/network calls before ``fail_scan`` runs, instead of the tens-of-
# seconds a 30s-initial policy would add on every deterministic failure. A
# ``maximum_interval`` is set for defensive completeness even though 3
# attempts at this backoff never approach it.
DEFAULT_RETRY_POLICY = RetryPolicy(
    maximum_attempts=3,
    initial_interval=timedelta(seconds=1),
    maximum_interval=timedelta(seconds=30),
    backoff_coefficient=2.0,
)

# Per-phase start_to_close budgets. ``scan`` does the bulk of the network/LLM
# work (search + fetch + per-posting extraction), so it keeps the original
# 30-minute budget; the surrounding phases are comfortably shorter.
_PREPARE_TIMEOUT = timedelta(minutes=2)
_BUILD_QUERIES_TIMEOUT = timedelta(minutes=5)
_SCAN_TIMEOUT = timedelta(minutes=30)
# Ranking issues one LLM judge call per posting sequentially (up to max_roles,
# default 40), so it gets the same 30-minute budget as scan rather than a
# tighter one that could time out a slow-but-valid run and force a full re-rank.
_RANK_TIMEOUT = timedelta(minutes=30)
_FINALIZE_TIMEOUT = timedelta(minutes=2)
_FAIL_TIMEOUT = timedelta(minutes=2)

# Temporal patch id gating the decomposed phase sequence. Workflows started
# before this change recorded a single ``job_matching_run_scan`` command first;
# they must keep replaying that monolithic path (``workflow.patched`` returns
# False for them) or the new phase commands would trip Temporal's determinism
# check. New executions record the marker and take the decomposed path.
#
# Naming note: sibling Temporal teams that did this same monolith-to-phases
# split name their patch id "<team>-per-phase-activities"; this one predates
# that convention and is intentionally NOT renamed to match — any workflow
# execution that already recorded this exact marker in its history would fail
# ``workflow.patched`` on replay against a renamed id, incorrectly falling back
# to the legacy branch. Only rename alongside a full history audit.
#
# Removal criterion: once every workflow execution started before this patch
# has drained (no history can still record only ``job_matching_run_scan`` as
# its first command — check via Temporal's visibility API for open/closed
# executions of this workflow type older than the deploy date), the
# ``workflow.patched`` check, this constant, and ``run_scan_activity`` /
# ``JobMatchingOrchestrator`` (its only remaining caller) can all be deleted.
_DECOMPOSED_PHASES_PATCH = "job-matching-decomposed-phases"


def _best_effort(logger_: Any, fn, message: str, *args: Any) -> bool:
    """Call ``fn()``; on any exception, log ``message`` (%-formatted with
    ``args``) and return False instead of raising.

    Shared by prepare/finalize/fail_scan for their "attempt a store write,
    never let it abort the phase, just note the failure" calls — the same
    pattern the thread-mode ``JobMatchingOrchestrator`` repeats inline.

    Preconditions:
        * ``fn`` takes no arguments.
    Postconditions:
        * Returns True if ``fn()`` completed without raising, False otherwise
          (the exception is logged via ``logger_.warning``, never re-raised).
    """
    try:
        fn()
        return True
    except Exception:  # noqa: BLE001 - best-effort persistence; caller decides how to proceed
        logger_.warning(message, *args, exc_info=True)
        return False


def _activity_error_message(exc: ActivityError) -> str:
    """Best-effort real failure text for a caught :class:`ActivityError`.

    ``ActivityError``'s own message is the Temporal SDK's generic wrapper text
    (e.g. "Activity task failed") — the same for every activity type and every
    failure — while the underlying business exception (what a phase actually
    raised) is chained on ``__cause__``. Prefer that; fall back to ``str(exc)``
    when there is no cause (e.g. a raw timeout/cancellation) or the cause itself
    has no message (e.g. a bare ``raise SomeError()`` or an unmessaged
    ``assert``, whose ``str()`` is ``""``) — an empty diagnostic would be worse
    than the SDK's generic-but-non-empty fallback text.

    Preconditions:
        * ``exc`` is a caught ``ActivityError``; it need not have a chained
          ``__cause__`` (a raw timeout/cancellation may not).
    Postconditions:
        * Returns ``str(exc.__cause__)`` when a cause is chained and that text
          is non-empty, else ``str(exc)``.
    """
    cause = exc.__cause__
    cause_text = str(cause) if cause is not None else ""
    return cause_text or str(exc)


@activity.defn(name="job_matching_prepare_scan")
def prepare_scan_activity(job_id: str, request: dict[str, Any], run_id: str) -> dict[str, Any]:
    """Open a scan: idempotent-replay guard, RUNNING transition, profile + run row.

    Reconstructs the request, resolves the effective profile, creates the
    Postgres run row under the workflow-supplied ``run_id``, and (when
    ``exclude_seen``) loads the seen fingerprints — all the setup the
    orchestrator does before the specialist phases run. Returns a JSON-native
    dict the workflow threads into the downstream activities.

    ``run_id`` is generated by the workflow (``workflow.uuid4()``, deterministic
    across replay) and passed in rather than minted here, so an activity retry
    after a crash re-uses the same id; combined with ``create_run``'s
    ``ON CONFLICT DO UPDATE`` this makes prepare idempotent — a retry neither
    duplicates the run row nor orphans a half-created one, and always persists
    the profile/request this attempt actually used rather than a stale snapshot
    from an earlier, lost attempt.

    ``load_job_seeker_profile`` reads the seeker's stored profile and need not be
    deterministic across attempts: Temporal records an activity's result and does
    not re-invoke it on workflow replay, so a successful attempt's profile is
    captured exactly once. A retry that precedes the first success may observe a
    different profile than an earlier, lost attempt (e.g. the user edited it
    between attempts) — ``create_run``'s upsert-on-conflict ensures the
    persisted ``profile_snapshot`` always reflects the attempt that actually
    returned, matching what the workflow threads into every downstream phase.

    Preconditions:
        * ``job_id`` refers to a job row already created by ``POST /scan``.
        * ``request`` is the JSON dump of a :class:`JobMatchRequest`.
        * ``run_id`` is the workflow-owned run identifier.
    Postconditions:
        * ``{"status": "already_completed", "result": <stored>}`` when the job is
          already COMPLETED (idempotent replay — no RUNNING flap, no run row).
        * ``{"status": "cancelled"}`` when the job was CANCELLED before start.
        * Otherwise ``{"status": "ready", ...}`` carrying the serialised effective
          ``profile``, the ``skip`` fingerprint list, ``store_ok`` (False when the
          run row could not be created — persistence is best-effort and never
          aborts the scan), and the request's ``max_queries``/``max_roles``/
          ``top_n``; the job row is set RUNNING and the ``run_id`` run row exists.
    """
    from job_matching_team.models import JobMatchRequest
    from job_matching_team.profile.loader import load_job_seeker_profile
    from job_matching_team.shared.job_store import (
        JOB_STATUS_CANCELLED,
        JOB_STATUS_COMPLETED,
        JOB_STATUS_RUNNING,
        get_job,
        update_job,
    )
    from job_matching_team.store import get_store

    # Idempotent replay + pre-run cancellation, ported from the monolith: a retry
    # that lands on an already-COMPLETED job returns the stored result without
    # re-running; a job cancelled before start never flips to RUNNING.
    existing = get_job(job_id)
    if existing is not None:
        status = existing.get("status")
        if status == JOB_STATUS_COMPLETED:
            return {"status": "already_completed", "result": existing.get("result") or {}}
        if status == JOB_STATUS_CANCELLED:
            return {"status": "cancelled"}

    req = JobMatchRequest(**request)
    update_job(job_id, status=JOB_STATUS_RUNNING)

    effective = load_job_seeker_profile().merged_with(req.profile_overrides)

    store = get_store()
    store_ok = _best_effort(
        activity.logger,
        lambda: store.create_run(run_id, effective, req),
        "Failed to create run row %s",
        run_id,
    )

    skip: list[str] = []
    if req.exclude_seen and store_ok:
        try:
            skip = sorted(store.seen_fingerprints())
        except Exception:  # noqa: BLE001 - seen-set lookup is best-effort
            activity.logger.warning("seen_fingerprints lookup failed", exc_info=True)

    return {
        "status": "ready",
        "profile": effective.model_dump(mode="json"),
        "skip": skip,
        "store_ok": store_ok,
        "max_queries": req.max_queries,
        "max_roles": req.max_roles,
        "top_n": req.top_n,
    }


@activity.defn(name="job_matching_build_queries")
def build_queries_activity(profile: dict[str, Any], max_queries: int, job_id: str) -> list[str]:
    """Build the search queries for a scan (LLM with deterministic fallback).

    Preconditions:
        * ``profile`` is the JSON dump of a :class:`JobSeekerProfile`.
        * ``max_queries >= 1``.
    Postconditions:
        * Returns up to ``max_queries`` unique, non-empty query strings. Every
          LLM call is stamped with the job_matching team + ``job_id`` for
          telemetry attribution.
    """
    from job_matching_team.agents.query_builder import QueryBuilderAgent
    from job_matching_team.profile.model import JobSeekerProfile
    from llm_service import llm_attribution

    prof = JobSeekerProfile.model_validate(profile)
    with llm_attribution(team="job_matching", job_id=job_id):
        return QueryBuilderAgent().build(prof, max_queries=max_queries)


@activity.defn(name="job_matching_scan")
def scan_activity(
    queries: list[str], max_roles: int, skip: list[str], job_id: str
) -> list[dict[str, Any]]:
    """Search, fetch, and extract up to ``max_roles`` unique postings.

    Preconditions:
        * ``queries`` is the list from :func:`build_queries_activity`.
        * ``max_roles >= 1``; ``skip`` is a list of fingerprints to exclude
          (rebuilt to a set here — the boundary carries JSON, not Python sets).
    Postconditions:
        * Returns the serialised (JSON-native) postings — a fingerprint-deduped
          list of length ``<= max_roles`` excluding any fingerprint in ``skip``.
    """
    from job_matching_team.agents.scanner import JobScannerAgent
    from llm_service import llm_attribution

    with llm_attribution(team="job_matching", job_id=job_id):
        postings = JobScannerAgent().scan(queries, max_roles=max_roles, skip_fingerprints=set(skip))
    return [p.model_dump(mode="json") for p in postings]


@activity.defn(name="job_matching_rank")
def rank_activity(
    postings: list[dict[str, Any]], profile: dict[str, Any], top_n: int, job_id: str
) -> dict[str, Any]:
    """Score every posting and return the top-N plus scan totals.

    Preconditions:
        * ``postings`` is the list from :func:`scan_activity`; ``profile`` is a
          serialised :class:`JobSeekerProfile`; ``top_n >= 1``.
    Postconditions:
        * ``{"top": [...], "total_found": len(postings),
          "scanned_fingerprints": [...]}`` — ``top`` is the ranked best-first
          top-N (serialised :class:`RankedJob`); ``total_found`` counts every
          scanned posting; ``scanned_fingerprints`` lists all their fingerprints
          (so ``exclude_seen`` can suppress them on later runs).
    """
    from job_matching_team.agents.ranker import JobRankerAgent
    from job_matching_team.models import JobPosting
    from job_matching_team.profile.model import JobSeekerProfile
    from llm_service import llm_attribution

    prof = JobSeekerProfile.model_validate(profile)
    parsed = [JobPosting.model_validate(p).ensure_fingerprint() for p in postings]
    with llm_attribution(team="job_matching", job_id=job_id):
        ranked = JobRankerAgent().rank(parsed, prof)
    top = ranked[:top_n]
    return {
        "top": [r.model_dump(mode="json") for r in top],
        "total_found": len(parsed),
        "scanned_fingerprints": [p.fingerprint for p in parsed],
    }


@activity.defn(name="job_matching_finalize_scan")
def finalize_scan_activity(
    job_id: str,
    run_id: str,
    top: list[dict[str, Any]],
    total_found: int,
    scanned_fingerprints: list[str],
    profile: dict[str, Any],
    store_ok: bool,
) -> dict[str, Any]:
    """Persist results, build the response, and drive the job store to COMPLETED.

    Preconditions:
        * ``run_id`` was returned by :func:`prepare_scan_activity`.
        * ``top`` / ``profile`` are serialised :class:`RankedJob` /
          :class:`JobSeekerProfile`.
    Postconditions:
        * When ``store_ok``, the run row is saved (completed) with ``top`` and
          ``total_found``; a save failure marks the run failed instead (results
          persistence never blocks the response). The save is idempotent on
          ``run_id`` — a retry after a crash replaces, not duplicates, its rows.
        * Returns the serialised :class:`JobMatchResponse`, and sets the job
          COMPLETED with it — unless the job was cancelled mid-run, in which case
          it returns ``{}`` and leaves the job untouched.
        * If the cancellation check itself fails, raises so Temporal retries
          finalize (idempotent on ``run_id``) with a fresh read rather than
          completing a possibly-cancelled job; the run row stays COMPLETED across
          retries, and if they exhaust into ``fail_scan``, that activity
          self-heals the job to COMPLETED from the run's persisted results
          instead of marking it FAILED (or leaving it stuck RUNNING) over a
          scan that actually succeeded.
    """
    from job_matching_team.models import JobMatchResponse, RankedJob
    from job_matching_team.profile.model import JobSeekerProfile
    from job_matching_team.shared.job_store import (
        JOB_STATUS_COMPLETED,
        is_job_cancelled,
        update_job,
    )

    ranked = [RankedJob.model_validate(r) for r in top]
    if store_ok:
        from job_matching_team.store import get_store

        store = get_store()
        # INTENTIONAL run-FAILED + job-COMPLETED split on a save failure (do not
        # "fix"): the run row is FAILED because its results couldn't be
        # persisted, yet the job still COMPLETEs below with the in-memory
        # payload. This mirrors the thread-mode orchestrator — "persistence
        # failure must not lose the response" — so a transient DB write error
        # still returns the ranked jobs the caller computed. The two rows track
        # different things (run persistence vs. API job outcome), so the
        # mismatch is expected. If mark_failed ALSO fails (run store entirely
        # unreachable, not just the save), the run row stays in its prior
        # (RUNNING) state while the job still COMPLETEs below — the same
        # trade-off taken to its limit, with response preservation still
        # winning over run-row accuracy; the stale RUNNING run is a recoverable
        # secondary record (a re-run or the run's own lifecycle supersedes it),
        # never lost data.
        saved = _best_effort(
            activity.logger,
            lambda: store.save_results(
                run_id,
                ranked,
                total_found=total_found,
                scanned_fingerprints=scanned_fingerprints,
                is_retry=activity.info().attempt > 1,
            ),
            "Failed to save results for run %s",
            run_id,
        )
        if not saved:
            _best_effort(
                activity.logger,
                lambda: store.mark_failed(run_id, "persisting results failed"),
                "Failed to mark run %s failed after save error",
                run_id,
            )

    payload = JobMatchResponse(
        run_id=run_id,
        ranked_jobs=ranked,
        total_found=total_found,
        total_ranked=len(ranked),
        profile_snapshot=JobSeekerProfile.model_validate(profile),
    ).model_dump(mode="json")

    # Results are already persisted (run COMPLETED). Only COMPLETE the job when
    # we can confirm it wasn't cancelled: if the cancel read fails, let it raise
    # so Temporal retries finalize with a fresh read (save_results is idempotent
    # on run_id, so re-running is safe). This avoids completing a job the user
    # cancelled; and because store.mark_failed won't overwrite a COMPLETED run,
    # exhausting the retries into fail_scan can't flip the saved run to FAILED.
    if is_job_cancelled(job_id):
        return {}
    update_job(job_id, status=JOB_STATUS_COMPLETED, result=payload)
    return payload


@activity.defn(name="job_matching_fail_scan")
def fail_scan_activity(job_id: str, run_id: str, error: str, store_ok: bool) -> None:
    """Record a failed scan, or self-heal the job if it actually succeeded.

    Invoked by the workflow when a pipeline phase exhausts its retries. Marks the
    run FAILED (when persisted) and the job FAILED, unless the job was cancelled
    or — the case this activity exists to catch — a later phase already
    completed the run before this failure was recorded, in which case the job is
    completed instead of failed (see Postconditions).

    Re-running this activity only re-runs status flips — never the scan — so,
    unlike the monolith, every job-store write here is allowed to raise on
    failure: that lets the bounded ``DEFAULT_RETRY_POLICY`` actually record the
    right terminal status once a transient outage clears, instead of silently
    leaving the job RUNNING or guessing. The run-completion rebuild
    (``store.get_run_response``) and both cancellation reads are likewise
    unguarded: a failure in any of them propagates the same way rather than
    guessing "not completed" / "not cancelled" and risking an incorrect FAILED
    write over a scan that actually succeeded or a job the user genuinely
    cancelled. A sustained outage on any of these exhausts the retries and is
    swallowed by the workflow's inner ``except ActivityError``, leaving job/run
    in their last-known state instead of a guessed, possibly wrong one. The
    run-row ``mark_failed`` write itself is secondary history and stays
    best-effort (also guarded by its own ``status <> completed`` check).

    Preconditions:
        * ``job_id`` refers to a job row already created by ``POST /scan``.
        * ``run_id`` is the workflow-owned run identifier from
          :func:`prepare_scan_activity`.
        * ``store_ok`` reflects whether the run row is known to exist (False on
          a prepare-phase failure, since prepare's only unguarded exception
          paths all precede ``create_run``).
    Postconditions:
        * If ``store_ok`` and rebuilding the run's response fails (run store
          unreachable, or the run is completed but its ``profile_snapshot`` is
          corrupt), this raises so Temporal retries rather than guessing the
          run isn't completed.
        * If ``store_ok`` and the run already reports ``completed`` (a later
          phase durably saved its results before this failure was recorded —
          e.g. finalize's post-save cancellation check exhausted its retries),
          the JOB is COMPLETED with the run's persisted results (unless
          cancelled, mirroring finalize's own check) instead of ever being
          marked FAILED or left stuck RUNNING — the whole point of this
          self-healing path is that a scan that actually succeeded is reported
          as such.
        * Otherwise the run row is FAILED when ``store_ok`` and not already
          completed; a mark_failed write failure is swallowed (best-effort
          history) — it does not fall under the raise above, since by that
          point the run is known not completed and only the write itself
          failed.
        * The job row is FAILED unless it was cancelled. If either the
          cancellation check or the job-status write can't reach the job store,
          this raises so Temporal retries; on retry exhaustion the workflow
          swallows it (bounded).
    """
    from job_matching_team.shared.job_store import (
        JOB_STATUS_COMPLETED,
        JOB_STATUS_FAILED,
        is_job_cancelled,
        update_job,
    )

    if store_ok:
        from job_matching_team.store import get_store

        store = get_store()
        completed_payload = store.get_run_response(run_id)
        if completed_payload is not None:
            # A later phase already saved results and completed the run before
            # this failure was recorded (see the finalize->fail_scan note in
            # finalize_scan_activity's docstring). Self-heal instead of
            # reporting FAILED (or leaving the job stuck RUNNING) over a scan
            # that actually succeeded: complete the job with the run's
            # persisted results. Mirrors finalize's own cancellation check —
            # only complete when confirmed not cancelled; if that check itself
            # fails (the same ambiguity that likely routed here), let it
            # propagate so Temporal retries with a fresh read rather than
            # guessing.
            if not is_job_cancelled(job_id):
                update_job(
                    job_id,
                    status=JOB_STATUS_COMPLETED,
                    result=completed_payload.model_dump(mode="json"),
                )
            return
        _best_effort(
            activity.logger,
            lambda: store.mark_failed(run_id, error),
            "Failed to mark run %s failed",
            run_id,
        )

    # Unguarded: let a cancellation-check failure propagate rather than
    # guessing "not cancelled" and risking an incorrect FAILED write over a job
    # that was actually cancelled by the user. Temporal retries this (idempotent)
    # activity; if the outage clears, we get the real state instead of a guess.
    # update_job is likewise unguarded so a sustained outage drives the same
    # bounded retry rather than silently dropping the FAILED write.
    if not is_job_cancelled(job_id):
        update_job(job_id, status=JOB_STATUS_FAILED, error=error)


@activity.defn(name="job_matching_run_scan")
def run_scan_activity(job_id: str, request: dict[str, Any]) -> dict[str, Any]:
    """Legacy single-activity scan — the ``workflow.patched`` replay path.

    Superseded by the decomposed prepare -> build_queries -> scan -> rank ->
    finalize activities that :class:`JobMatchingWorkflow` schedules for new runs.
    Workflows started before the decomposition still schedule this activity (the
    workflow's ``workflow.patched`` legacy branch) so their recorded histories
    replay deterministically until they drain.

    Runs one scan on the Temporal worker, keeping the job store in sync.
    Reconstructs the request + orchestrator inside the activity because neither
    is serialisable across the Temporal boundary. Business exceptions are
    recorded on the job store as FAILED and **swallowed** (not re-raised), so a
    deterministic failure does not trigger Temporal's retry loop.

    Preconditions:
        * ``job_id`` refers to a job row already created by ``POST /scan``.
        * ``request`` is the JSON dump of a :class:`JobMatchRequest`.
    Postconditions:
        * The job row is COMPLETED (with the serialised response) on success,
          FAILED (with the error) on failure, and left untouched if the job was
          cancelled before or during the run.
        * Idempotent on retry: when the job is already COMPLETED, returns the
          stored result without re-running or mutating the job row.
        * The return value is the serialised :class:`JobMatchResponse` on
          success, else an empty dict.
    """
    from job_matching_team.models import JobMatchRequest
    from job_matching_team.orchestrator import JobMatchingOrchestrator
    from job_matching_team.shared.job_store import (
        JOB_STATUS_CANCELLED,
        JOB_STATUS_COMPLETED,
        JOB_STATUS_FAILED,
        JOB_STATUS_RUNNING,
        get_job,
        is_job_cancelled,
        update_job,
    )

    req = JobMatchRequest(**request)
    try:
        existing = get_job(job_id)
        if existing is not None:
            status = existing.get("status")
            if status == JOB_STATUS_COMPLETED:
                return existing.get("result") or {}
            if status == JOB_STATUS_CANCELLED:
                return {}
        update_job(job_id, status=JOB_STATUS_RUNNING)
        result = JobMatchingOrchestrator().run(req, job_id=job_id)
        if is_job_cancelled(job_id):
            return {}
        payload = result.model_dump(mode="json")
        update_job(job_id, status=JOB_STATUS_COMPLETED, result=payload)
        return payload
    except Exception as exc:  # noqa: BLE001 - recorded on the job store, not re-raised
        activity.logger.exception("Job matching scan %s failed", job_id)
        try:
            if not is_job_cancelled(job_id):
                update_job(job_id, status=JOB_STATUS_FAILED, error=str(exc))
        except Exception:  # noqa: BLE001 - job store unreachable; do not re-raise into Temporal
            activity.logger.warning(
                "Could not record FAILED status for scan %s (job store unreachable)",
                job_id,
                exc_info=True,
            )
        return {}


@workflow.defn(name="JobMatchingWorkflow")
class JobMatchingWorkflow:
    @workflow.run
    async def run(self, job_id: str, request: dict[str, Any]) -> dict[str, Any]:
        """Execute one scan as a durable workflow of per-phase activities.

        Sequences prepare -> build_queries -> scan -> rank -> finalize, each a
        separately-retryable Temporal activity, and records a terminal failure
        via ``fail_scan`` when any phase exhausts its retries.

        Preconditions:
            * ``job_id`` refers to a job row already created by ``POST /scan``.
            * ``request`` is the JSON dump of a :class:`JobMatchRequest`. The
              ``(job_id, request)`` argument order is part of the contract — a
              change would break deterministic replay of in-flight histories and
              the dispatch bridge (pinned by
              ``test_workflow_run_delegates_through_phases`` and
              ``test_start_job_matching_workflow_delegates_to_shared_bridge``).
        Postconditions:
            * Returns the serialised :class:`JobMatchResponse` on success, the
              stored result on idempotent replay of an already-COMPLETED job, or
              ``{}`` when the job was cancelled or a phase failed. The activities
              own every job-store / run-row write; this method performs none.

        Trade-offs:
            * Each phase is bounded to ``DEFAULT_RETRY_POLICY`` (3 attempts, see
              its own comment for the deterministic-failure-cost trade-off).
              ``prepare`` and ``finalize`` are idempotent on the workflow-owned
              ``run_id`` (``create_run`` / ``save_results`` replace rather than
              duplicate on retry), so a crash mid-phase re-runs that phase safely;
              a deterministic failure exhausts the attempts and is recorded FAILED
              via ``fail_scan``. End state matches the monolith (job + run FAILED,
              no retry storm); only the path differs.
            * The monolith bounded a whole scan to one 30-minute
              ``start_to_close_timeout``. The decomposed phases each have their
              own budget (``_PREPARE_TIMEOUT`` + ``_BUILD_QUERIES_TIMEOUT`` +
              ``_SCAN_TIMEOUT`` + ``_RANK_TIMEOUT`` + ``_FINALIZE_TIMEOUT`` =
              up to 69 minutes) with no single enforced aggregate deadline, so a
              run where every phase individually stays within budget can now
              legitimately take more than double the old ceiling. There is no
              Temporal primitive for wrapping a sequence of already-awaited
              activities in one overall timeout without also bounding (and
              risking prematurely cancelling) a legitimately slow individual
              phase; if an aggregate ceiling is needed, add it explicitly (e.g.
              a workflow-level timer race) rather than assuming one.
            * None of ``build_queries``/``scan``/``rank`` heartbeat, so — same as
              the monolith's single activity — an attempt that runs past its
              ``start_to_close_timeout`` is retried while its own worker thread
              keeps executing; a slow-but-eventually-successful attempt can
              execute more than once concurrently (bounded by
              ``maximum_attempts``). The decomposition doesn't introduce this
              risk, but does spread it across three independent phases instead
              of one.
            * Workflows started before the decomposition replay the single
              ``run_scan_activity`` path (gated by ``workflow.patched``) so their
              histories stay deterministic; new runs take the decomposed phases.
        """
        if not workflow.patched(_DECOMPOSED_PHASES_PATCH):
            # Pre-decomposition history: its first recorded command is a single
            # job_matching_run_scan. Replay that exact monolithic path (same
            # activity type, timeout, and retry policy as the original workflow)
            # so Temporal's determinism check passes while the history drains.
            return await workflow.execute_activity(
                run_scan_activity,
                args=[job_id, request],
                start_to_close_timeout=timedelta(minutes=30),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )

        # Own the run id here (deterministic across replay) and thread it through
        # prepare/finalize/fail so an activity retry re-uses the same id and its
        # idempotent store writes replace rather than duplicate/orphan rows.
        run_id = str(workflow.uuid4())
        # Default until prepare reports it, for a prepare-phase failure that
        # never returns store_ok. False is not a guess: every unguarded
        # exception path inside prepare_scan_activity (job lookup, request
        # validation, the RUNNING transition, profile load) runs before its
        # store.create_run call, so if prepare itself failed, no run row was
        # ever created and fail_scan must not attempt store.mark_failed against
        # a run_id with no backing row.
        store_ok = False
        try:
            prep = await workflow.execute_activity(
                prepare_scan_activity,
                args=[job_id, request, run_id],
                start_to_close_timeout=_PREPARE_TIMEOUT,
                retry_policy=DEFAULT_RETRY_POLICY,
            )
            status = prep.get("status")
            if status == "already_completed":
                return prep.get("result") or {}
            if status == "cancelled":
                return {}

            profile = prep["profile"]
            store_ok = prep["store_ok"]
            queries = await workflow.execute_activity(
                build_queries_activity,
                args=[profile, prep["max_queries"], job_id],
                start_to_close_timeout=_BUILD_QUERIES_TIMEOUT,
                retry_policy=DEFAULT_RETRY_POLICY,
            )
            postings = await workflow.execute_activity(
                scan_activity,
                args=[queries, prep["max_roles"], prep["skip"], job_id],
                start_to_close_timeout=_SCAN_TIMEOUT,
                retry_policy=DEFAULT_RETRY_POLICY,
            )
            ranked = await workflow.execute_activity(
                rank_activity,
                args=[postings, profile, prep["top_n"], job_id],
                start_to_close_timeout=_RANK_TIMEOUT,
                retry_policy=DEFAULT_RETRY_POLICY,
            )
            return await workflow.execute_activity(
                finalize_scan_activity,
                args=[
                    job_id,
                    run_id,
                    ranked["top"],
                    ranked["total_found"],
                    ranked["scanned_fingerprints"],
                    profile,
                    store_ok,
                ],
                start_to_close_timeout=_FINALIZE_TIMEOUT,
                retry_policy=DEFAULT_RETRY_POLICY,
            )
        except ActivityError as exc:
            # A phase exhausted its bounded retries (a genuine crash or a
            # deterministic failure). Record the run + job FAILED via an activity
            # (all store / job-store I/O must happen off the workflow thread),
            # then return {} — the failure is terminal, not retried by the caller.
            # fail_scan only flips statuses, so it is idempotent and safe to retry.
            # _activity_error_message (not str(exc)): ActivityError's own message
            # is the SDK's generic wrapper text, identical for every activity type
            # and every failure — the real cause is chained on __cause__.
            try:
                await workflow.execute_activity(
                    fail_scan_activity,
                    args=[job_id, run_id, _activity_error_message(exc), store_ok],
                    start_to_close_timeout=_FAIL_TIMEOUT,
                    retry_policy=DEFAULT_RETRY_POLICY,
                )
            except ActivityError:
                # Even the terminal bookkeeping couldn't be recorded (worker died
                # or the job store was down through every retry). Swallow it:
                # failing the workflow here would not record FAILED either and
                # would surface a Failed workflow instead of the {} a caller
                # expects. This is the bounded worst case (a crash through every
                # fail_scan retry); the run/job stay RUNNING until the job's own
                # lifecycle (cancellation/expiry) or a re-submission supersedes it.
                workflow.logger.warning("fail_scan could not record FAILED for %s", job_id)
            return {}
