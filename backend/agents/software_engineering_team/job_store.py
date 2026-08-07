"""
Job store for coding_team: persists job status and task graph snapshot via the job service.
Used for status API and resume; task graph snapshot and agent_task_map are stored on the job.

Invariants:
    - Job lifecycle: a job starts ``pending``, moves to ``running``, and ends in exactly one
      terminal status: ``completed``, ``completed_with_failures``, ``already_complete``,
      ``failed``, or ``cancelled``. ``waiting_for_user`` is a substate of ``running`` — a paused
      job still owns its checkout and issue, so it counts as active (see
      ``NON_TERMINAL_STATUSES``) even though no worker thread is currently driving it.
    - ``create_job`` is idempotent: the job service upserts on ``(team, job_id)``, so calling it
      again for the same ``job_id`` overwrites the existing record rather than erroring.
    - ``update_job`` (and the narrower wrappers built on it, e.g. ``update_job_task_graph``,
      ``heartbeat_job``) require the job to already exist; they patch fields on the job service's
      record and do not create one.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import job_service_client as _jsc
from job_service_client import JobServiceClient, get_job_service_client
from software_engineering_team.models import JobStatus
from user_profile import ArtifactType, record_association_safe

logger = logging.getLogger(__name__)

DEFAULT_CACHE_DIR: Path = Path(os.getenv("AGENT_CACHE", ".agent_cache"))

# Every status under which a job may still resume on its own. This is the single definition of
# "active": a paused job (waiting_for_user) is still in flight — its checkout is owned, its issue
# is being worked — and every liveness/admission consumer must see it.
NON_TERMINAL_STATUSES: tuple[str, ...] = (
    JobStatus.PENDING.value,
    JobStatus.RUNNING.value,
    JobStatus.WAITING_FOR_USER.value,
)


def _client(cache_dir: str | Path = DEFAULT_CACHE_DIR) -> JobServiceClient:
    # cache_dir is accepted for API compatibility with callers that were written when this module
    # was file-backed. JobServiceClient uses HTTP (configured via JOB_SERVICE_URL) and does not
    # use a local filesystem cache, so cache_dir is intentionally not forwarded.
    # Reuse one pooled client per process instead of a fresh client (and TCP
    # connection) on every job operation.
    return get_job_service_client(team="coding_team")


def create_job(
    job_id: str,
    repo_path: str,
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
    plan_input: Optional[Dict[str, Any]] = None,
) -> None:
    """Create a new coding_team job with pending status."""
    data: Dict[str, Any] = {
        "repo_path": repo_path,
        "phase": "task_graph",
        "status_text": "",
        "progress": 0,
        "task_graph_snapshot": [],
        "agent_task_map": {},
        "stack_specs": [],
        "error": None,
        "plan_input": plan_input or {},
        "events": [],
        # Human-in-the-loop gate state (mirrors the SE job-record contract so the same answer
        # protocol resumes a pause on either path).
        "pending_questions": [],
        "waiting_for_answers": False,
        "submitted_answers": [],
        # Set only by a pause_strategy="return" pause (pause_cycle._run_pause_cycle) -- never by a
        # block-mode (thread/GitHub-hook) pause. Presence of resume_token is exactly what
        # POST /run/{job_id}/answers uses to decide whether a submission must signal the Temporal
        # workflow instead of relying on a blocked thread (see coding_team_hitl.py).
        "resume_token": None,
        "pause_kind": None,
        "pause_context": None,
    }
    _client(cache_dir).create_job(job_id, status="pending", **data)
    # Best-effort: link the project to the default profile. record_association_safe
    # never raises, so a link failure can't break job creation.
    record_association_safe(ArtifactType.PROJECT, "coding_team", job_id, label=repo_path or job_id)


def get_job(
    job_id: str,
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
) -> Optional[Dict[str, Any]]:
    """Get job data. Returns None if not found."""
    return _client(cache_dir).get_job(job_id)


def update_job(
    job_id: str,
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
    heartbeat: bool = True,
    **fields: Any,
) -> None:
    """Update job with given fields (e.g. status, phase, status_text, task_graph_snapshot, agent_task_map)."""
    _client(cache_dir).update_job(job_id, heartbeat=heartbeat, **fields)


def heartbeat_job(
    job_id: str,
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
) -> None:
    """Touch the job's ``last_heartbeat_at`` without changing any other field.

    Preconditions: ``job_id`` names an existing job.
    Postconditions: the job service stamps ``last_heartbeat_at`` to now. Liveness
        consumers (e.g. the PR-review admission guard's staleness cutoff) read this
        stamp to distinguish a live worker from one that died mid-job. Raises only if
        the job-service call fails — callers beating from a background thread should
        wrap it (``BackgroundHeartbeat`` takes an ``on_error``).
    """
    _client(cache_dir).heartbeat(job_id)


def update_job_task_graph(
    job_id: str,
    task_graph_snapshot: Dict[str, Any],
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
) -> None:
    """Persist task graph snapshot and agent_task_map to the job (for status API and resume)."""
    update_job(
        job_id,
        cache_dir=cache_dir,
        heartbeat=True,
        task_graph_snapshot=task_graph_snapshot.get("tasks", []),
        agent_task_map=task_graph_snapshot.get("agent_task_map", {}),
    )


def list_jobs(
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
    active_only: bool = False,
) -> List[Dict[str, Any]]:
    """List coding_team jobs.

    Postconditions:
        - With ``active_only`` True, returns only jobs in a NON_TERMINAL_STATUSES status —
          including ``waiting_for_user``: a paused job still owns its checkout and issue, so
          admission guards and list consumers must not treat it as gone.
    """
    statuses = list(NON_TERMINAL_STATUSES) if active_only else None
    return _client(cache_dir).list_jobs(statuses=statuses)


# ---------------------------------------------------------------------------
# Human-in-the-loop pause / answer operations
#
# The four cache_dir-keyed wrappers are generated by the shared factory so this
# team and software_engineering_team share one contract for the same job-record
# fields (waiting_for_answers / pending_questions / submitted_answers). Writes are
# atomic via the job service so a concurrent answer submission and a status update
# cannot clobber each other.
# ---------------------------------------------------------------------------

# ``lambda cd: _client(cd)`` (not ``_client`` directly) keeps the client factory
# resolved at call time against this module's ``_client`` — so tests can still
# monkeypatch ``job_store._client`` and the wrappers observe it.
(
    add_pending_questions,
    submit_answers,
    is_waiting_for_answers,
    get_submitted_answers,
) = _jsc.make_cachedir_hitl(lambda cd: _client(cd), DEFAULT_CACHE_DIR)


def append_submitted_answers(
    job_id: str,
    answers: List[Dict[str, Any]],
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
) -> None:
    """cache_dir-bound wrapper for a Temporal-native pause's append-only answer store.

    Not part of the shared ``make_cachedir_hitl`` factory above (that factory's four functions
    are shared with ``software_engineering_team``'s own job-record contract) — this is
    coding_team-only, narrower behavior. See ``job_service_client.append_submitted_answers``
    for the full contract.
    """
    _jsc.append_submitted_answers(_client(cache_dir), job_id, answers)


# ---------------------------------------------------------------------------
# Cross-worker resume ownership (a recoverable TTL lease)
#
# The process-local run-thread registry cannot stop two *different worker processes* from each
# spawning an orchestrator for the same paused job. ``claim_resume`` grants ownership in the SHARED
# job store as a TTL lease, so it is both mutually exclusive AND recoverable if the winning worker
# dies:
#
#   * Mutual exclusion: an atomic, row-locked ``apply`` increments a monotonic ``resume_claim_seq``
#     and stamps ``resume_claim_at``; the caller wins iff its increment took the seq from the value
#     it read to exactly that value + 1 (optimistic compare-and-set). Concurrent callers in any
#     process read the same prior seq and only one increment can produce ``prior + 1``.
#   * Recoverability: ``resume_claim_at`` is a lease timestamp, not renewed during the resumed run.
#     A claim whose stamp is older than ``RESUME_CLAIM_TTL_S`` is treated as abandoned (its winner
#     died before the run progressed), so a later claim reclaims it instead of wedging. A still-fresh
#     stamp means a live worker is mid-resume → decline. The brief acquire→status=running window is
#     far shorter than the TTL, and no claim is attempted once the job leaves ``waiting_for_user``.
# ---------------------------------------------------------------------------

_RESUME_CLAIM_SEQ_FIELD = "resume_claim_seq"
_RESUME_CLAIM_AT_FIELD = "resume_claim_at"
RESUME_CLAIM_TTL_S = 60.0

# Tolerated clock skew between worker hosts: a claim stamped up to this many seconds in the
# future (relative to the checking worker) is still treated as fresh. Covers NTP drift without
# wedging the lease on a far-future/corrupt stamp (those still expire naturally).
_CLAIM_CLOCK_SKEW_TOLERANCE_S = 10.0


def _claim_lease_fresh(stamp: Any, now: datetime) -> bool:
    """True when ``stamp`` is a parseable timestamp whose age is within the lease TTL.

    A stamp more than ``_CLAIM_CLOCK_SKEW_TOLERANCE_S`` seconds in the future is NOT fresh:
    implausible skew or corruption must not wedge the lease until a far-future time passes.
    Stamps within the bounded skew window are accepted as fresh to tolerate NTP drift in
    multi-host deployments.
    """
    if not stamp:
        return False
    try:
        ts = datetime.fromisoformat(str(stamp))
    except ValueError:
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    age = (now - ts).total_seconds()
    return age > -_CLAIM_CLOCK_SKEW_TOLERANCE_S and age < RESUME_CLAIM_TTL_S


def claim_resume(job_id: str, cache_dir: str | Path = DEFAULT_CACHE_DIR) -> bool:
    """Atomically and recoverably claim cross-worker ownership of a resume.

    Preconditions:
        - Callers must confirm the job is in ``waiting_for_user`` state before calling; this
          function only enforces the claim stamp, not the job status. Invoking on a running or
          terminal job is a logic error in the caller — it may acquire a lease for a job that
          no longer needs resuming.
    Postconditions:
        - Returns True iff this caller acquired the lease: the prior claim was absent or expired
          (its stamp older than ``RESUME_CLAIM_TTL_S``) AND this caller's atomic seq increment was
          the unique one that produced ``prior_seq + 1``. Returns False when a live worker holds a
          fresh lease, another concurrent caller won, or the job is gone. Never raises beyond the
          underlying transport.
    """
    client = _client(cache_dir)
    job = client.get_job(job_id)
    if not job:
        return False
    now = datetime.now(timezone.utc)
    if _claim_lease_fresh(job.get(_RESUME_CLAIM_AT_FIELD), now):
        return False  # a live worker is mid-resume
    prior_seq = job.get(_RESUME_CLAIM_SEQ_FIELD) or 0
    updated = client.apply_and_get(
        job_id,
        increment={_RESUME_CLAIM_SEQ_FIELD: 1},
        merge_fields={_RESUME_CLAIM_AT_FIELD: now.isoformat()},
    )
    return bool(updated) and updated.get(_RESUME_CLAIM_SEQ_FIELD) == prior_seq + 1


def release_resume_claim(job_id: str, cache_dir: str | Path = DEFAULT_CACHE_DIR) -> None:
    """Release the lease (clear its stamp) so a failed/abandoned spawn doesn't block the TTL window.

    Leaves ``resume_claim_seq`` untouched — it is monotonic; clearing only the stamp makes the job
    immediately re-claimable without resetting the compare-and-set version.

    Best-effort and never raises: a job-store transport error while clearing the stamp is logged and
    swallowed, not propagated. Release is cleanup, not a result — if it fails, the lease simply
    self-heals when its TTL expires. Crucially, callers rely on this: resume paths document
    "never raises", and ``resume_job`` calls this inside an ``except`` block that re-raises the
    original error — a release failure must never mask either.
    """
    try:
        _client(cache_dir).update_job(job_id, heartbeat=False, **{_RESUME_CLAIM_AT_FIELD: None})
    except Exception:
        logger.exception(
            "release_resume_claim failed for job %s; the lease will expire via its TTL.", job_id
        )
