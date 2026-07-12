"""Durable per-job transcript store for the Temporal path.

The fine-grained Temporal workflow fans UX out one activity per transcript. To
keep (potentially large) transcript bodies out of Temporal workflow history —
which records the ingest activity's result *and* every ``ux_one`` activity's
input — ``market_research_ingest`` persists the loaded transcripts to the shared
``AGENT_CACHE`` volume keyed by job and passes only lightweight references
(``{"index": i, "source": ...}``) through the workflow. Each ``ux_one`` activity
loads its own transcript from this store.

The in-process **thread path** does not use this store — it passes
``(source, text)`` in memory (nothing crosses a Temporal boundary there), so the
shared ``orchestrator.ingest``/``ux_one`` seam is unchanged; persistence lives
only in the Temporal activity wrappers.

Layout mirrors the repo's cache convention (``$AGENT_CACHE/{team}_team/...``,
e.g. blogging's ``blogging_team/runs``): each transcript is written to
``<root>/market_research_team/transcripts/<job_id>/<index>.json``. ``AGENT_CACHE``
is resolved at call time (never at import) so the temporalio workflow sandbox is
never tripped.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Callable, List, Tuple

logger = logging.getLogger(__name__)

# One-shot warning guard (mirrors blogging/shared/run_pipeline_job.py's
# _tempfile_fallback_warned convention) — logs once per process instead of
# once per activity call.
_tempfile_fallback_warned = False


def _base_dir() -> Path:
    """Resolve the transcript-store root at call time.

    Preconditions:
        - None (``AGENT_CACHE`` may be unset).
    Postconditions:
        - Returns ``<AGENT_CACHE or a temp dir>/market_research_team/transcripts``.
          Never reads the environment at import time. When ``AGENT_CACHE`` is
          unset, logs one warning per process — the fallback tempdir is
          per-process/non-shared, so a multi-worker deployment would silently
          fail to find another worker's persisted transcripts without this
          diagnostic pointing at the real misconfiguration.
    """
    global _tempfile_fallback_warned
    agent_cache = os.getenv("AGENT_CACHE", "").strip()
    if agent_cache:
        root = agent_cache
    else:
        root = os.path.join(tempfile.gettempdir(), "market_research_agent_cache")
        if not _tempfile_fallback_warned:
            _tempfile_fallback_warned = True
            logger.warning(
                "AGENT_CACHE is not set — persisted transcripts will be written to %s, "
                "a per-process temp directory that is NOT shared across worker "
                "processes/hosts. In a multi-worker Temporal deployment this will cause "
                "ux_one activities to fail with FileNotFoundError if scheduled on a "
                "different worker than the one that ran ingest. Set AGENT_CACHE to a "
                "shared volume for production deployments.",
                root,
            )
    return Path(root) / "market_research_team" / "transcripts"


def _job_dir(job_id: str) -> Path:
    return _base_dir() / job_id


def save_transcripts(job_id: str, loaded: List[Tuple[str, str]]) -> List[dict[str, Any]]:
    """Persist loaded transcripts and return constant-size references.

    Preconditions:
        - ``job_id`` is the run's job id; ``loaded`` is ``[(source, text), ...]``
          from ``orchestrator.ingest`` (may be empty).

    Postconditions:
        - Writes ``<root>/<job_id>/<index>.json`` (``{"source", "text"}``) for
          each transcript and returns ``[{"index": i, "source": source}, ...]`` —
          references that carry no transcript body. Idempotent: re-running
          overwrites the same files (safe under Temporal activity retries).
    """
    job_dir = _job_dir(job_id)
    job_dir.mkdir(parents=True, exist_ok=True)
    refs: List[dict[str, Any]] = []
    for index, (source, text) in enumerate(loaded):
        (job_dir / f"{index}.json").write_text(
            json.dumps({"source": source, "text": text}), encoding="utf-8"
        )
        refs.append({"index": index, "source": source})
    return refs


def load_transcript(job_id: str, index: int) -> Tuple[str, str]:
    """Load one persisted transcript by index.

    Preconditions:
        - ``save_transcripts`` persisted ``job_id`` and ``index`` is one of the
          returned refs' indices.

    Postconditions:
        - Returns ``(source, text)`` for that transcript (raises ``FileNotFound``
          / ``KeyError`` if the ref was never persisted — a programming error,
          surfaced loudly rather than masked).
    """
    data = json.loads((_job_dir(job_id) / f"{index}.json").read_text(encoding="utf-8"))
    return data["source"], data["text"]


def clear_transcripts(job_id: str) -> None:
    """Delete a job's persisted transcripts (best-effort terminal cleanup).

    Preconditions:
        - None.

    Postconditions:
        - Removes the job's transcript directory if present; never raises (a
          cleanup failure must not affect the run's terminal status).
    """
    shutil.rmtree(_job_dir(job_id), ignore_errors=True)


def sweep_orphaned(is_active: Callable[[str], bool]) -> int:
    """Clear persisted transcripts for jobs the caller reports as no longer active.

    ``clear_transcripts`` only runs from ``finalize_activity``/``mark_failed_activity``,
    so a job whose WORKER process died (crash, forced container recycle) before
    either ran leaves its transcript directory behind with nothing to clean it up
    — the same class of orphan the team-service's own job-status startup
    reconciliation (``mark_all_active_jobs_interrupted``) exists to catch for job
    *records*, but that mechanism has no knowledge of this store's *files*. Calling
    this once at service startup (after that reconciliation has run) closes that
    gap for the common "worker restarted" case. It does NOT help a workflow that
    is forcibly ``TerminateWorkflowExecution``-ed on an otherwise-healthy,
    long-running worker — that bypasses all app code, including this sweep, until
    the process itself is restarted.

    Preconditions:
        - ``is_active(job_id)`` returns ``True`` iff ``job_id`` still has
          in-progress work in the job store (PENDING/RUNNING); any exception
          from ``is_active`` for a given job is treated as "not active" (err on
          the side of clearing) and logged.
    Postconditions:
        - Removes every persisted job directory for which ``is_active`` returns
          ``False`` and returns the count cleared. Never raises — this runs
          during app startup and a job-service hiccup at that moment must not
          block boot.
    """
    base = _base_dir()
    if not base.is_dir():
        return 0
    cleared = 0
    for job_dir in base.iterdir():
        if not job_dir.is_dir():
            continue
        job_id = job_dir.name
        try:
            active = is_active(job_id)
        except Exception:
            logger.warning(
                "sweep_orphaned: could not check status for job %s; clearing", job_id, exc_info=True
            )
            active = False
        if not active:
            shutil.rmtree(job_dir, ignore_errors=True)
            cleared += 1
    return cleared
