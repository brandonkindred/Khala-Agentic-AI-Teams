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
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, List, Tuple


def _base_dir() -> Path:
    """Resolve the transcript-store root at call time.

    Preconditions:
        - None (``AGENT_CACHE`` may be unset).
    Postconditions:
        - Returns ``<AGENT_CACHE or a temp dir>/market_research_team/transcripts``.
          Never reads the environment at import time.
    """
    root = os.getenv("AGENT_CACHE", "").strip() or os.path.join(
        tempfile.gettempdir(), "market_research_agent_cache"
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
