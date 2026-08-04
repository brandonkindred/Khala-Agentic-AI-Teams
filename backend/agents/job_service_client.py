"""HTTP client for the central job service.

``JobServiceClient`` is the single Python entry point every agent team uses to
read/write job state.  It speaks HTTP to the ``khala-job-service`` container
defined in ``docker/docker-compose.yml``.

``JOB_SERVICE_URL`` is **required**.  The client raises ``RuntimeError`` at
construction time if it is not configured.  For local dev start the service
with ``docker compose -f docker/docker-compose.yml up -d postgres job-service``
and export ``JOB_SERVICE_URL=http://localhost:8085``.  Pytest spins up an
in-process job service automatically via ``backend/conftest.py``.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

import httpx

from shared.concurrency import BackgroundHeartbeat
from shared.http import get_pooled_client

logger = logging.getLogger(__name__)

# Re-export status constants so teams can import from here
JOB_STATUS_PENDING = "pending"
JOB_STATUS_RUNNING = "running"
JOB_STATUS_COMPLETED = "completed"
JOB_STATUS_FAILED = "failed"
JOB_STATUS_CANCELLED = "cancelled"
JOB_STATUS_INTERRUPTED = "interrupted"


_MISSING_URL_MSG = (
    "JOB_SERVICE_URL is not set. Start the job service "
    "(`docker compose -f docker/docker-compose.yml up -d postgres job-service`) "
    "and export JOB_SERVICE_URL=http://localhost:8085, "
    "or run via pytest which provisions one in-process."
)

# Retry policy for transient transport errors is idempotency-aware — see
# JobServiceClient._request. HTTP methods whose replay cannot duplicate an effect.
_IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "PUT", "DELETE"})
# Transport errors where the request provably never reached the server (no
# connection was established / acquired from the pool), so a retry can never
# duplicate work — safe for ANY method.
# ``ConnectTimeout`` is a TimeoutException (not a ConnectError subclass): the TCP
# handshake never completed, so the request was never sent — same safety as
# ConnectError. Omitting it turns brief job-service startup races into hard 500s.
_RETRY_ANY_METHOD_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)
# Transport errors where the request may already have been sent before the
# failure (the server closed a stale keep-alive connection, or timed out
# mid-exchange), so they are retried ONLY for idempotent methods.
# ``RemoteProtocolError``/``ReadError``/``WriteError`` are all the same
# stale-keep-alive failure mode — the server (or a proxy/LB) reset a pooled idle
# socket and we discover it mid-exchange (ReadError is ECONNRESET while reading
# the response; WriteError its write-side analog). ``ReadError``/``WriteError``
# are ``httpx.NetworkError`` subclasses, distinct from the ``*Timeout`` errors
# above, so there is no overlap with the existing tuples.
_RETRY_IDEMPOTENT_ONLY_ERRORS = (
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.RemoteProtocolError,
    httpx.ReadError,
    httpx.WriteError,
)


class _RetryAmbiguityTracker:
    """Records whether a _RETRY_IDEMPOTENT_ONLY_ERRORS-class transport error was
    observed during a _request call -- i.e. whether an earlier attempt may
    already have reached the server before its response was lost.

    Preconditions:
        - None; construct a fresh instance per _request call to be tracked.
    Postconditions:
        - maybe_reached_server starts False.
    Invariants:
        - Monotonic: only ever set True by _request, never reset to False.
        - Only set from the _RETRY_IDEMPOTENT_ONLY_ERRORS except-branch, never
          for _RETRY_ANY_METHOD_ERRORS (those prove the request never reached
          the server at all, so there is no ambiguity to record).
        - Scoped to a single _request call -- not reused across calls/threads.
    """

    def __init__(self) -> None:
        self.maybe_reached_server: bool = False


def _default_base_url() -> str:
    return os.environ.get("JOB_SERVICE_URL", "")


# ---------------------------------------------------------------------------
# Cached per-team client factory
#
# Every team's job store independently re-implemented "get a JobServiceClient
# for my team" — some constructing a fresh client on every call, others
# hand-rolling a module-level lazy singleton.  This single factory replaces all
# of those: it returns one shared client per team for the life of the process.
# The client resolves JOB_SERVICE_URL per request (see ``_base_url``), so a
# cached instance still honors a URL set after construction.
# ---------------------------------------------------------------------------

_client_cache: dict[str, "JobServiceClient"] = {}
_client_cache_lock = threading.Lock()


def get_job_service_client(team: str) -> "JobServiceClient":
    """Return a process-wide cached :class:`JobServiceClient` for ``team``.

    Preconditions:
        - ``team`` is a non-empty string.
    Postconditions:
        - Returns the same instance for the same ``team`` across calls
          (one client per team), constructed lazily on first request.
    """
    assert team, "team must be a non-empty string"
    client = _client_cache.get(team)
    if client is not None:
        return client
    with _client_cache_lock:
        client = _client_cache.get(team)
        if client is None:
            client = JobServiceClient(team=team)
            _client_cache[team] = client
        return client


def _clear_job_client_cache_for_testing() -> None:
    """Drop all cached per-team clients.  Test-only seam for isolation."""
    with _client_cache_lock:
        _client_cache.clear()


class JobServiceClient:
    """HTTP client for the central job service."""

    def __init__(self, team: str, base_url: str | None = None) -> None:
        self.team = team
        # Explicit base_url wins; otherwise resolve from JOB_SERVICE_URL each
        # request via the ``_base_url`` property below.  This matters because
        # team modules build module-level ``JobServiceClient(team=…)`` at import
        # time — pinning the env value at construction would freeze whatever
        # placeholder pytest's conftest had set before the integration fixture
        # swapped in the real URL.
        self._explicit_base_url: str | None = base_url.rstrip("/") if base_url else None
        if not self._explicit_base_url and not _default_base_url():
            raise RuntimeError(_MISSING_URL_MSG)

    @property
    def _base_url(self) -> str:
        if self._explicit_base_url is not None:
            return self._explicit_base_url
        url = _default_base_url()
        if not url:
            raise RuntimeError(_MISSING_URL_MSG)
        return url.rstrip("/")

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def _url(self, path: str) -> str:
        return f"{self._base_url}{path}"

    def _request(
        self,
        method: str,
        url: str,
        *,
        timeout: float = 30.0,
        max_retries: int = 3,
        retry_tracker: _RetryAmbiguityTracker | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        """Execute an HTTP request with idempotency-aware retry on transient errors.

        Preconditions:
            - ``timeout`` is a positive, finite number of seconds (passed
              through to :func:`get_pooled_client`, which asserts this).
            - ``max_retries`` is non-negative.
        Postconditions:
            - Returns a successful ``httpx.Response`` (2xx) or raises the last
              transient error / HTTP status error after exhausting retries.
            - Retry is idempotency-aware so a retry can never duplicate a
              non-idempotent operation:
                * ``ConnectError`` / ``ConnectTimeout`` / ``PoolTimeout`` — the
                  request provably never reached the server (no connection was
                  established / acquired, or the TCP handshake timed out), so
                  they are retried for ANY method.
                * ``ReadTimeout`` / ``WriteTimeout`` / ``RemoteProtocolError`` /
                  ``ReadError`` / ``WriteError`` — the request may already have
                  been sent (e.g. a stale keep-alive connection the server reset,
                  surfacing as a disconnect or ECONNRESET mid-exchange, or a
                  timeout mid-exchange), so they are retried ONLY for idempotent
                  methods (GET/HEAD/OPTIONS/PUT/DELETE). For non-idempotent
                  methods (e.g. POST) they propagate immediately — replaying could
                  duplicate the operation, and the caller must decide how to
                  recover.
                * ``HTTPStatusError`` is never retried.
            - When ``retry_tracker`` is not None, its ``maybe_reached_server``
              attribute is set True iff at least one
              ``_RETRY_IDEMPOTENT_ONLY_ERRORS`` exception was observed during
              this call — regardless of whether that attempt was subsequently
              retried or the exception ultimately propagated. It is never set
              for ``_RETRY_ANY_METHOD_ERRORS``. When ``retry_tracker`` is None
              (every pre-existing call site), behavior is unchanged.
        """
        delays = [0.5, 1.0, 2.0]
        last_exc: Exception | None = None
        total_attempts = max_retries + 1
        idempotent = method.upper() in _IDEMPOTENT_METHODS

        def _backoff(attempt: int) -> None:
            time.sleep(delays[min(attempt, len(delays) - 1)])

        for attempt in range(total_attempts):
            try:
                # Reuse a process-wide pooled client (keep-alive connections)
                # instead of opening/closing one per request.
                client = get_pooled_client(timeout)
                resp = client.request(method, url, **kwargs)
                resp.raise_for_status()
                return resp
            except _RETRY_ANY_METHOD_ERRORS as exc:
                # Connection never established/acquired -> request not sent ->
                # always safe to retry, regardless of method idempotency.
                last_exc = exc
                if attempt < max_retries:
                    _backoff(attempt)
                    continue
                raise
            except _RETRY_IDEMPOTENT_ONLY_ERRORS as exc:
                # The request may already have reached the server; replaying a
                # non-idempotent method could duplicate the operation, so only
                # idempotent methods are retried.
                last_exc = exc
                if retry_tracker is not None:
                    retry_tracker.maybe_reached_server = True
                if idempotent and attempt < max_retries:
                    _backoff(attempt)
                    continue
                raise
            except httpx.HTTPStatusError:
                raise
        raise last_exc  # type: ignore[misc]

    # ------------------------------------------------------------------
    # Core CRUD
    # ------------------------------------------------------------------

    def create_job(self, job_id: str, *, status: str = JOB_STATUS_PENDING, **fields: Any) -> None:
        self._request(
            "POST",
            self._url(f"/jobs/{self.team}"),
            json={"job_id": job_id, "status": status, "fields": fields},
        )

    def replace_job(self, job_id: str, payload: Dict[str, Any]) -> None:
        self._request(
            "POST",
            self._url(f"/jobs/{self.team}/{job_id}/replace"),
            json={"payload": payload},
        )

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        resp = self._request("GET", self._url(f"/jobs/{self.team}/{job_id}"))
        return resp.json().get("job")

    def delete_job(self, job_id: str) -> bool:
        """Delete a job, tolerating a lost response to an earlier retried attempt.

        Preconditions:
            - ``job_id`` is non-empty (caller-validated; not enforced here).
        Postconditions:
            - Returns True when the server deleted a row on this call, OR when
              it reports ``deleted: false`` but an earlier attempt in this
              call hit a ``_RETRY_IDEMPOTENT_ONLY_ERRORS``-class error
              (``maybe_reached_server``) — that error class means an earlier
              DELETE may have already reached the server and removed the row
              before its own response was lost, which is exactly what
              produced this call's ``deleted: false``. Reported as success
              rather than a spurious not-found.
            - Returns False only when the server reports ``deleted: false``
              and no such ambiguous error occurred (the common
              genuinely-nonexistent-job case), including when only
              ``_RETRY_ANY_METHOD_ERRORS``-class errors occurred (those prove
              no earlier attempt ever reached the server).
            - Known, accepted limitation: if the job never existed AND the
              server's "not found" response is itself lost to the same error
              class (rather than a successful deletion's response), this
              cannot be distinguished from the case above and will also
              report True. The job service has no soft-delete/audit trail (a
              hard SQL DELETE), so once a retry-eligible transport error has
              occurred, "never existed" and "already deleted by an earlier
              attempt" are server-side indistinguishable — informationally
              unavoidable for a client-only fix.
        """
        tracker = _RetryAmbiguityTracker()
        resp = self._request(
            "DELETE", self._url(f"/jobs/{self.team}/{job_id}"), retry_tracker=tracker
        )
        deleted = resp.json().get("deleted", False)
        return deleted or tracker.maybe_reached_server

    def cancel_active_job(self, job_id: str) -> bool:
        """Atomically cancel a job server-side only if it is still pending/running.

        Preconditions: ``job_id`` is non-empty (the caller validates).
        Postconditions: returns True only when the server set the status to
            ``cancelled`` because the job was pending/running at write time. The
            status guard is evaluated in the same conditional UPDATE that performs
            the write, so a job that has already reached a terminal status is never
            overwritten (no read-then-write race).
        """
        resp = self._request("POST", self._url(f"/jobs/{self.team}/{job_id}/cancel"))
        return resp.json().get("cancelled", False)

    def list_jobs(self, *, statuses: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        params = {}
        if statuses:
            params["statuses"] = statuses
        resp = self._request("GET", self._url(f"/jobs/{self.team}"), params=params)
        return resp.json().get("jobs", [])

    def update_job(self, job_id: str, *, heartbeat: bool = True, **fields: Any) -> None:
        self._request(
            "PATCH",
            self._url(f"/jobs/{self.team}/{job_id}"),
            json={"heartbeat": heartbeat, "fields": fields},
        )

    def update_job_if_not_cancelled(
        self, job_id: str, *, heartbeat: bool = True, **fields: Any
    ) -> Optional[bool]:
        """Atomically merge ``fields`` into ``job_id`` unless it is already cancelled.

        Preconditions: ``job_id`` is non-empty (the caller validates); ``fields``
            must not set ``status`` to ``JOB_STATUS_CANCELLED`` — this primitive
            only guards against writing over an *existing* cancellation, it is
            not a cancellation mechanism itself (use ``cancel_active_job``, whose
            guard additionally excludes jobs already in a terminal state).
            Enforced by an explicit raise (never an ``assert``, which is stripped
            under ``python -O`` — this guard exists to prevent silent data
            corruption, not just to document caller intent).
        Postconditions: returns True when the server performed the write because
            the job existed and was not cancelled. Returns False when the job
            exists but is already cancelled (no write). Returns None when the job
            does not exist at all (no write) — distinct from False so a caller
            can tell a broken precondition (missing row) apart from a legitimate
            cancellation, without an extra round trip. The cancelled-check is
            evaluated in the same conditional UPDATE that performs the write, so a
            cancel that lands between a caller's earlier read and this call is
            never overwritten (no read-then-write race) — the same guarantee
            ``cancel_active_job`` provides for cancellation itself, mirrored here
            for RUNNING/COMPLETED/FAILED transitions.
        """
        if fields.get("status") == JOB_STATUS_CANCELLED:
            raise ValueError(
                "update_job_if_not_cancelled must not be used to cancel a job "
                "(it would overwrite a completed/failed job too) — use cancel_active_job"
            )
        resp = self._request(
            "POST",
            self._url(f"/jobs/{self.team}/{job_id}/update-if-not-cancelled"),
            json={"heartbeat": heartbeat, "fields": fields},
        )
        return resp.json().get("updated")

    def append_event(
        self,
        job_id: str,
        *,
        action: str,
        outcome: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
        status: Optional[str] = None,
    ) -> None:
        self._request(
            "POST",
            self._url(f"/jobs/{self.team}/{job_id}/event"),
            json={"action": action, "outcome": outcome, "details": details, "status": status},
        )

    def mark_stale_active_jobs_failed(
        self,
        *,
        stale_after_seconds: float,
        reason: str,
        waiting_field: str = "waiting_for_answers",
    ) -> List[str]:
        resp = self._request(
            "POST",
            self._url(f"/jobs/{self.team}/mark-stale-failed"),
            json={
                "stale_after_seconds": stale_after_seconds,
                "reason": reason,
                "waiting_field": waiting_field,
            },
        )
        return resp.json().get("failed_job_ids", [])

    def mark_all_active_jobs_failed(
        self,
        reason: str,
        *,
        http_timeout: float = 30.0,
        http_max_retries: int = 3,
    ) -> List[str]:
        """Mark all active (pending/running) jobs as failed (e.g. on server shutdown).

        Skips jobs in a waiting state (waiting_for_answers, waiting_for_title_selection,
        waiting_for_story_input).
        """
        resp = self._request(
            "POST",
            self._url(f"/jobs/{self.team}/mark-all-running-failed"),
            json={"reason": reason},
            timeout=http_timeout,
            max_retries=http_max_retries,
        )
        return resp.json().get("failed_job_ids", [])

    def mark_all_active_jobs_interrupted(
        self,
        reason: str,
        *,
        http_timeout: float = 30.0,
        http_max_retries: int = 3,
    ) -> List[str]:
        """Mark all active (pending/running) jobs as interrupted due to service shutdown."""
        resp = self._request(
            "POST",
            self._url(f"/jobs/{self.team}/mark-all-running-interrupted"),
            json={"reason": reason},
            timeout=http_timeout,
            max_retries=http_max_retries,
        )
        return resp.json().get("interrupted_job_ids", [])

    # ------------------------------------------------------------------
    # Atomic patch helpers
    # ------------------------------------------------------------------

    def merge_nested(self, job_id: str, path: str, data: Dict[str, Any]) -> None:
        """Merge *data* into a nested dict at *path* (dot-separated)."""
        self._request(
            "POST",
            self._url(f"/jobs/{self.team}/{job_id}/apply"),
            json={"merge_nested": {path: data}},
        )

    def append_to_list(self, job_id: str, field: str, items: List[Any]) -> None:
        """Append *items* to the list stored at *field*."""
        self._request(
            "POST",
            self._url(f"/jobs/{self.team}/{job_id}/apply"),
            json={"append_to": {field: items}},
        )

    def atomic_update(
        self,
        job_id: str,
        *,
        merge_fields: Optional[Dict[str, Any]] = None,
        merge_nested: Optional[Dict[str, Any]] = None,
        append_to: Optional[Dict[str, List[Any]]] = None,
        increment: Optional[Dict[str, int]] = None,
    ) -> None:
        """Perform an atomic batch of merge + append + increment operations."""
        self._request(
            "POST",
            self._url(f"/jobs/{self.team}/{job_id}/apply"),
            json={
                "merge_fields": merge_fields,
                "merge_nested": merge_nested,
                "append_to": append_to,
                "increment": increment,
            },
        )

    def apply_and_get(
        self,
        job_id: str,
        *,
        merge_fields: Optional[Dict[str, Any]] = None,
        merge_nested: Optional[Dict[str, Any]] = None,
        append_to: Optional[Dict[str, List[Any]]] = None,
        increment: Optional[Dict[str, int]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Like :meth:`atomic_update`, but return the updated job record.

        The job service applies the patch under a row lock (``SELECT ... FOR UPDATE``) and returns
        the post-update record, so a caller can read back an atomically-incremented counter — a
        cross-worker fencing token — and learn whether its own increment produced a given value.

        Postconditions:
            - Returns the updated job dict, or None when the job does not exist.
        """
        resp = self._request(
            "POST",
            self._url(f"/jobs/{self.team}/{job_id}/apply"),
            json={
                "merge_fields": merge_fields,
                "merge_nested": merge_nested,
                "append_to": append_to,
                "increment": increment,
            },
        )
        return resp.json().get("job")

    def increment_field(self, job_id: str, field: str, delta: int = 1) -> None:
        """Atomically increment an integer field by *delta*."""
        self.atomic_update(job_id, increment={field: delta})

    def heartbeat(self, job_id: str) -> None:
        """Touch last_heartbeat_at for a job."""
        self._request("POST", self._url(f"/jobs/{self.team}/{job_id}/heartbeat"))


# ---------------------------------------------------------------------------
# Stale job monitor
# ---------------------------------------------------------------------------


def start_stale_job_monitor(
    client: JobServiceClient,
    *,
    interval_seconds: float,
    stale_after_seconds: float,
    reason: str,
) -> threading.Event:
    """Start a background thread that periodically marks stale jobs as failed.

    Beats immediately on start (``beat_first``) so stale jobs left by a previous
    crash are swept on startup, then every ``interval_seconds``. Returns the stop
    ``Event`` the caller sets during shutdown.
    """
    stop_event = threading.Event()
    BackgroundHeartbeat(
        lambda: client.mark_stale_active_jobs_failed(
            stale_after_seconds=stale_after_seconds,
            reason=reason,
        ),
        interval_seconds,
        name=f"{client.team}-stale-job-monitor",
        beat_first=True,
        stop_event=stop_event,
        on_error=lambda exc: logger.warning("stale job monitor error (%s): %s", client.team, exc),
    ).start()
    return stop_event


# ---------------------------------------------------------------------------
# Shared validation helper for resume/restart endpoints
# ---------------------------------------------------------------------------

# Standard status sets for resume/restart gating.
RESUMABLE_STATUSES: frozenset[str] = frozenset(
    {
        JOB_STATUS_PENDING,
        JOB_STATUS_RUNNING,
        JOB_STATUS_FAILED,
        JOB_STATUS_INTERRUPTED,
        "agent_crash",
    }
)
RESTARTABLE_STATUSES: frozenset[str] = frozenset(
    {
        JOB_STATUS_COMPLETED,
        JOB_STATUS_FAILED,
        JOB_STATUS_CANCELLED,
        JOB_STATUS_INTERRUPTED,
        "agent_crash",
    }
)


def validate_job_for_action(
    job_data: Optional[Dict[str, Any]],
    job_id: str,
    allowed_statuses: frozenset[str],
    action_label: str = "action",
) -> Dict[str, Any]:
    """Validate a job exists and is in an allowed status.

    Raises ``ValueError`` with a human-readable message on failure.
    The caller should catch this and convert to an ``HTTPException``.

    Returns the job data dict on success.
    """
    if not job_data:
        raise ValueError(f"Job {job_id} not found")
    status = job_data.get("status", JOB_STATUS_PENDING)
    if status not in allowed_statuses:
        raise ValueError(f"Job cannot be {action_label} (status={status}).")
    return job_data


# ---------------------------------------------------------------------------
# Base job store — eliminates duplicated CRUD wrappers across teams
# ---------------------------------------------------------------------------


class BaseJobStore:
    """Shared job store operations that all teams duplicate.

    Subclass and set ``team`` to get create/get/update/delete/list/reset
    for free.  Override or add team-specific methods as needed.

    The human-in-the-loop pause/answer operations shared by the coding and
    software-engineering teams live as module-level functions at the bottom of
    this file (``add_pending_questions`` / ``submit_answers`` /
    ``is_waiting_for_answers`` / ``get_submitted_answers``) — this module is the
    single home for team-agnostic job-store behaviour.

    Usage::

        class BlogJobStore(BaseJobStore):
            team = "blogging_team"

            def submit_title_selection(self, job_id, title): ...
    """

    team: str = ""  # Subclasses MUST override with a non-empty team name.

    def _client(self) -> JobServiceClient:
        """Return the cached client for this store's team.

        Preconditions:
            - The subclass has set a non-empty ``team``.
        """
        if not self.team:
            raise NotImplementedError(
                f"{type(self).__name__} must set a non-empty 'team' class attribute"
            )
        return get_job_service_client(self.team)

    def create_job(self, job_id: str, *, status: str = JOB_STATUS_PENDING, **fields: Any) -> None:
        self._client().create_job(job_id, status=status, **fields)

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        return self._client().get_job(job_id)

    def update_job(self, job_id: str, **kwargs: Any) -> None:
        self._client().update_job(job_id, **kwargs)

    def delete_job(self, job_id: str) -> bool:
        return self._client().delete_job(job_id)

    def list_jobs(
        self, *, running_only: bool = False, statuses: Optional[List[str]] = None
    ) -> List[Dict[str, Any]]:
        """List jobs, optionally filtered by status.

        Preconditions: at most one of ``running_only`` / ``statuses`` need be set;
            an explicit ``statuses`` takes precedence over ``running_only``.
        Postconditions: returns the matching jobs (empty list when none); never None.
        """
        if statuses is None and running_only:
            statuses = [JOB_STATUS_PENDING, JOB_STATUS_RUNNING]
        return self._client().list_jobs(statuses=statuses) or []

    def cancel_job(self, job_id: str) -> bool:
        """Cooperatively cancel a job: mark it cancelled if it is still active.

        Preconditions: ``job_id`` is non-empty.
        Postconditions: returns True and sets status to ``cancelled`` when the job
            exists and is pending/running; returns False (no write) otherwise. The
            status check and the write happen in one conditional server-side UPDATE
            (``JobServiceClient.cancel_active_job``), so a job that races to a
            terminal status between the decision and the write is never clobbered —
            there is no get-then-update window here.
        """
        if not job_id:
            raise ValueError("cancel_job requires a non-empty job_id")
        return self._client().cancel_active_job(job_id)

    def is_job_cancelled(self, job_id: str) -> bool:
        """Return True if the job exists and has been marked cancelled.

        Preconditions: ``job_id`` is non-empty.
        Postconditions: pure read; returns a bool. Used as the cooperative-cancel
            poll inside orchestrators.
        """
        if not job_id:
            raise ValueError("is_job_cancelled requires a non-empty job_id")
        job = self._client().get_job(job_id)
        return job is not None and job.get("status") == JOB_STATUS_CANCELLED

    def mark_job_running(self, job_id: str) -> None:
        self.update_job(job_id, status=JOB_STATUS_RUNNING, started_at=_now_iso())

    def mark_job_completed(self, job_id: str, **extra: Any) -> None:
        self.update_job(
            job_id, status=JOB_STATUS_COMPLETED, progress=100, completed_at=_now_iso(), **extra
        )

    def mark_job_failed(self, job_id: str, error: str) -> None:
        self.update_job(job_id, status=JOB_STATUS_FAILED, error=error)

    def mark_all_running_jobs_failed(self, reason: str) -> List[str]:
        """Best-effort: mark all active jobs failed (e.g. on shutdown).

        Preconditions: ``reason`` is a human-readable explanation (any string).
        Postconditions: returns the failed job ids; a client error is logged with
            its traceback and swallowed (returns ``[]``) so a shutdown hook never
            raises. Teams that want ``interrupted`` instead override this.
        """
        try:
            return self._client().mark_all_active_jobs_failed(reason)
        except Exception as e:  # noqa: BLE001 - shutdown hook must not raise
            # exc_info so a real defect (not just an operational error) is
            # diagnosable rather than hidden behind a one-line message.
            logger.warning("mark_all_running_jobs_failed (%s): %s", self.team, e, exc_info=True)
            return []

    def reset_job(self, job_id: str) -> None:
        """Reset a job to initial state for restart (preserves input params)."""
        self.update_job(
            job_id,
            status=JOB_STATUS_PENDING,
            progress=0,
            error=None,
            current_phase=None,
            status_text=None,
        )


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(tz=timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Human-in-the-loop pause / answer operations — shared by the coding and
# software-engineering teams' job stores. Each function takes the client
# explicitly so a team wrapper passes its own (monkeypatch-able) client; writes
# are atomic so a concurrent answer submission and a status update cannot
# clobber each other.
# ---------------------------------------------------------------------------


def add_pending_questions(
    client: JobServiceClient, job_id: str, questions: List[Dict[str, Any]]
) -> None:
    """Append pending questions and set ``waiting_for_answers=True`` to pause the job.

    Preconditions: ``client`` is a live ``JobServiceClient``; ``job_id`` is
        non-empty; ``questions`` is a list of structured question dicts.
    Postconditions: the job's ``waiting_for_answers`` is True and ``questions``
        are appended to ``pending_questions`` in one atomic write.
    """
    client.atomic_update(
        job_id,
        merge_fields={"waiting_for_answers": True},
        append_to={"pending_questions": questions},
    )


def submit_answers(client: JobServiceClient, job_id: str, answers: List[Dict[str, Any]]) -> None:
    """Store submitted answers, clear pending questions, and clear the waiting flag.

    Preconditions: ``client`` is a live ``JobServiceClient``; ``job_id`` is non-empty.
    Postconditions: ``waiting_for_answers`` is False, ``pending_questions`` is
        empty, and ``answers`` are appended to ``submitted_answers`` in one atomic
        write (the orchestrator's wait loop resumes on the cleared flag).
    """
    client.atomic_update(
        job_id,
        merge_fields={"pending_questions": [], "waiting_for_answers": False},
        append_to={"submitted_answers": answers},
    )


def append_submitted_answers(
    client: JobServiceClient, job_id: str, answers: List[Dict[str, Any]]
) -> None:
    """Append answers to ``submitted_answers`` WITHOUT clearing the pause envelope.

    Used only for a Temporal-native (``pause_strategy="return"``) pause, where the job
    record's pause envelope (``waiting_for_answers``/``pending_questions``/``resume_token``/
    ``pause_kind``/``pause_context``) is the orchestrator's sole responsibility to clear —
    consumed only when the orchestrator atomically matches ``acknowledged_resume_token``
    against the persisted ``resume_token`` on its next re-entry (see
    ``coding_team_orchestrator``'s ``_check_pending_pause_reentry``). Unlike ``submit_answers``
    above (correct for a block-mode pause, where the orchestrator's own blocked wait loop is
    the only reader/clearer of the flag), clearing the envelope here instead would race a
    worker crash into silently dropping the human's answer — the workflow could resume
    thinking there's nothing to apply.

    Preconditions: ``client`` is a live ``JobServiceClient``; ``job_id`` names a job with a
        persisted, unresolved ``resume_token``.
    Postconditions: ``answers`` are appended to ``submitted_answers`` atomically;
        ``waiting_for_answers``, ``pending_questions``, ``resume_token``, ``pause_kind``, and
        ``pause_context`` are left untouched.
    """
    client.atomic_update(job_id, append_to={"submitted_answers": answers})


def is_waiting_for_answers(client: JobServiceClient, job_id: str) -> bool:
    """True iff the job is currently paused waiting for user answers.

    Preconditions: ``client`` is a live ``JobServiceClient``; ``job_id`` is non-empty.
    Postconditions: pure read; False when the job is missing.
    """
    data = client.get_job(job_id)
    return bool(data.get("waiting_for_answers", False)) if data else False


def get_submitted_answers(client: JobServiceClient, job_id: str) -> List[Dict[str, Any]]:
    """Return the answers submitted for this job (empty when none/unknown).

    Preconditions: ``client`` is a live ``JobServiceClient``; ``job_id`` is non-empty.
    Postconditions: pure read; a stored ``None`` is coerced to ``[]`` (``or []``)
        so a partially-written record never yields ``None`` to callers.
    """
    data = client.get_job(job_id)
    return list(data.get("submitted_answers") or []) if data else []


# Type of a team's ``cache_dir -> JobServiceClient`` factory.
ClientGetter = Callable[[Any], JobServiceClient]


def make_cachedir_hitl(
    client_getter: ClientGetter,
    default_cache_dir: Any,
) -> Tuple[
    Callable[..., None],
    Callable[..., None],
    Callable[..., bool],
    Callable[..., List[Dict[str, Any]]],
]:
    """Build the four ``cache_dir``-keyed HITL wrappers over a team's client getter.

    Both team job-stores (``coding_team.job_store`` and
    ``software_engineering_team.shared.job_store``) expose the same
    human-in-the-loop surface — pause a job with pending questions, submit answers
    to resume, and query the wait flag / submitted answers — keyed by a
    ``cache_dir`` rather than an explicit ``JobServiceClient``. Each wrapper is a
    one-line delegation to the module-level client-based function above; this
    factory single-sources them so the two teams cannot drift.

    Preconditions:
        - ``client_getter(cache_dir)`` returns a live ``JobServiceClient``.
        - ``default_cache_dir`` is the team's default cache root (used as the
          wrappers' ``cache_dir`` default).
    Postconditions:
        - Returns ``(add_pending_questions, submit_answers, is_waiting_for_answers,
          get_submitted_answers)``. Each accepts ``(job_id, ..., cache_dir=default)``
          and carries the contract of its client-based counterpart; the returned
          callables' ``__name__`` match the public wrapper names for clean
          tracebacks. Never raises here.
    """

    def add_pending_questions_cd(job_id, questions, cache_dir=default_cache_dir) -> None:
        """Append pending questions and set waiting_for_answers=True (pauses the job)."""
        add_pending_questions(client_getter(cache_dir), job_id, questions)

    def submit_answers_cd(job_id, answers, cache_dir=default_cache_dir) -> None:
        """Store answers, clear pending questions, and clear the wait flag (resumes the job)."""
        submit_answers(client_getter(cache_dir), job_id, answers)

    def is_waiting_for_answers_cd(job_id, cache_dir=default_cache_dir) -> bool:
        """True iff the job is currently paused waiting for user answers."""
        return is_waiting_for_answers(client_getter(cache_dir), job_id)

    def get_submitted_answers_cd(job_id, cache_dir=default_cache_dir) -> List[Dict[str, Any]]:
        """Return the answers submitted for this job (empty when none/unknown)."""
        return get_submitted_answers(client_getter(cache_dir), job_id)

    add_pending_questions_cd.__name__ = "add_pending_questions"
    submit_answers_cd.__name__ = "submit_answers"
    is_waiting_for_answers_cd.__name__ = "is_waiting_for_answers"
    get_submitted_answers_cd.__name__ = "get_submitted_answers"
    return (
        add_pending_questions_cd,
        submit_answers_cd,
        is_waiting_for_answers_cd,
        get_submitted_answers_cd,
    )
