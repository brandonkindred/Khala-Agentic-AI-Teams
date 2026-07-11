"""Durable snapshot of a loaded ``RepoContext``, keyed by job id.

In Temporal mode the audit fans out across five activities. Passing the loaded
``RepoContext`` (whose ``code_summary`` is the whole uncapped code corpus) through
workflow history would blow Temporal payload/history limits, and re-scanning the
live repo path in each activity risks combining results from different repository
states if the checkout mutates mid-audit.

This module writes the ``RepoContext`` **once** (in ``soc2_load_repo``) to a blob
on the shared ``AGENT_CACHE`` volume and lets each audit activity read that same
immutable snapshot back — so only the small ``job_id`` crosses activity
boundaries, and every criterion audits an identical repo state. The blob is
cleaned up when the audit completes or fails.

``AGENT_CACHE`` (the persistent ``/data/agents`` volume in Docker) is used when
set so the snapshot survives a worker restart between activities; otherwise a
temp dir is used (fine for local/thread-mode dev where Temporal durability isn't
in play).

Invariants:
    - A snapshot path is a pure function of ``job_id`` and the cache root, so any
      activity in the run can locate it without an explicit handle.
"""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path

from .models import RepoContext

logger = logging.getLogger(__name__)

_TEAM = "soc2_compliance_team"
_SUBDIR = "context_snapshots"


def _snapshot_dir() -> Path:
    """Return the directory holding context snapshots, creating it if needed.

    Postconditions:
        - Returns an existing directory under ``AGENT_CACHE`` (namespaced by
          team) when that env var is set, else under the system temp dir.
    """
    base = os.getenv("AGENT_CACHE", "").strip()
    root = (
        Path(base) / _TEAM / _SUBDIR if base else Path(tempfile.gettempdir()) / f"{_TEAM}_{_SUBDIR}"
    )
    root.mkdir(parents=True, exist_ok=True)
    return root


def snapshot_path(job_id: str) -> Path:
    """Deterministic snapshot path for ``job_id``.

    Preconditions:
        - ``job_id`` is a non-empty, filesystem-safe id (a UUID from the API).
    """
    assert job_id, "job_id must be non-empty"
    return _snapshot_dir() / f"{job_id}.json"


def save_snapshot(job_id: str, context: RepoContext) -> str:
    """Persist ``context`` for ``job_id`` and return the snapshot path.

    Preconditions:
        - ``context`` is the ``RepoContext`` loaded once for this job.
    Postconditions:
        - The snapshot is written (overwriting any prior one for ``job_id``);
          returns its path as a string.
    """
    path = snapshot_path(job_id)
    path.write_text(context.model_dump_json(), encoding="utf-8")
    return str(path)


def load_snapshot(job_id: str) -> RepoContext:
    """Read back the ``RepoContext`` snapshot for ``job_id``.

    Preconditions:
        - ``save_snapshot`` was called for ``job_id`` earlier in the run.
    Postconditions:
        - Returns the reconstructed ``RepoContext``. Raises ``FileNotFoundError``
          if the snapshot is missing.
    """
    return RepoContext.model_validate_json(snapshot_path(job_id).read_text(encoding="utf-8"))


def delete_snapshot(job_id: str) -> None:
    """Best-effort cleanup of the snapshot for ``job_id``.

    Postconditions:
        - The snapshot file is removed if present; never raises (a missing file
          or unlink error is logged, not propagated).
    """
    try:
        snapshot_path(job_id).unlink(missing_ok=True)
    except OSError:
        logger.warning("Could not delete SOC2 context snapshot for job %s", job_id, exc_info=True)
