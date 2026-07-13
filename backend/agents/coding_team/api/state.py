"""coding_team API — shared run-thread registry, timing constants, and pure answer/progress helpers.

Monkeypatched collaborators are dereferenced through the ``main`` module object
at call time so ``monkeypatch.setattr(main, ...)`` keeps taking effect after the
split; models are imported directly.

Invariants:
    - The run-thread registry itself lives in ``shared_run_thread_registry.RunThreadRegistry``;
      ``_active_run_threads``/``_starting_run_jobs``/``_run_thread_lock`` are back-compat aliases
      onto its live internals, so background threads and the answers/resume routes observe the
      same maps regardless of whether they go through the registry or poke these aliases directly.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

# Ensure backend/agents is on path for coding_team and job_service_client
from coding_team.api.models import (
    SubmitAnswersRequest,
)
from shared_hitl.progress import coerce_progress
from shared_hitl.validation import validate_answers
from shared_run_thread_registry import RunThreadRegistry

logger = logging.getLogger(__name__)

# Tracks the orchestrator thread per job so the answers endpoint can tell whether a blocked wait
# loop will pick up answers automatically (thread alive) or the job needs an explicit /resume (the
# thread died, e.g. on a server restart). Mirrors the SE team's _active_orchestrator_threads.
_registry = RunThreadRegistry()
# Back-compat aliases — existing call sites and tests reference these names directly.
_active_run_threads = _registry.threads
_starting_run_jobs = _registry.starting_jobs
_run_thread_lock = _registry.lock
_register_run_thread = _registry.register
_clear_run_thread = _registry.clear
_is_run_thread_alive = _registry.is_alive
_claim_run_thread = _registry.claim


def _coerce_progress(value: Any) -> Optional[int]:
    """Coerce a stored progress value to an int in [0, 100], or None.

    Thin wrapper over ``shared_hitl.progress.coerce_progress`` (see it for the full
    contract). Kept as a named function on this module so the ``main`` re-export and
    its ``monkeypatch.setattr(main, ...)`` target are unchanged after the extraction.
    """
    return coerce_progress(value)


def _validate_answers(data: Dict[str, Any], request: SubmitAnswersRequest) -> List[Dict[str, Any]]:
    """Validate submitted answers against the job's pending questions; return them as plain dicts.

    Thin wrapper over ``shared_hitl.validation.validate_answers`` (see it for the full
    contract: the 400/500 rule set and the ``question_text``-carrying return shape).
    Kept as a named function on this module so the ``main`` re-export and its
    ``monkeypatch.setattr(main, ...)`` target are unchanged after the extraction.
    """
    return validate_answers(data, request)


# A paused orchestrator's wait loop heartbeats every poll (~5s); anything older than this many
# seconds means no live wait loop exists anywhere — including other worker processes, which the
# process-local thread registry cannot see.

_ANSWER_WAIT_HEARTBEAT_STALE_S = 30.0

# Tolerated clock skew between worker hosts: a heartbeat stamped up to this many seconds in the
# future (relative to the checking worker) is still treated as fresh. This covers NTP drift in
# multi-host deployments without blocking resume indefinitely on a far-future/corrupt stamp.
_HEARTBEAT_CLOCK_SKEW_TOLERANCE_S = 10.0

# GitHub returns 422 Unprocessable Entity for validation errors — specifically a
# review comment whose line is off the diff. Only a 422 is recoverable by
# dropping/demoting the offending comment; other statuses signal a real failure.
_HTTP_UNPROCESSABLE = 422

# Body for the extra COMMENT review(s) the bisection path submits after the
# summary has already been posted on its own — so they don't repeat the summary.
_BISECT_CONTINUATION_BODY = "*(continued — additional findings)*"


def _answer_wait_heartbeat_fresh(data: Dict[str, Any]) -> bool:
    """True when a live answer-wait loop (possibly in another worker process) heartbeated recently.

    Preconditions:
        - ``data`` is a job record dict (possibly empty).
    Postconditions:
        - Returns True iff ``answer_wait_heartbeat_at`` parses as an ISO timestamp whose age is in
          ``(-_HEARTBEAT_CLOCK_SKEW_TOLERANCE_S, _ANSWER_WAIT_HEARTBEAT_STALE_S)``. Stamps more
          than ``_HEARTBEAT_CLOCK_SKEW_TOLERANCE_S`` seconds in the future (implausible skew or
          corruption) are NOT fresh — they must never block resume indefinitely. Missing/garbage
          values → False, never raises.
    """
    raw = (data or {}).get("answer_wait_heartbeat_at")
    if not raw:
        return False
    try:
        beat = datetime.fromisoformat(str(raw))
    except ValueError:
        return False
    if beat.tzinfo is None:
        beat = beat.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - beat).total_seconds()
    return age > -_HEARTBEAT_CLOCK_SKEW_TOLERANCE_S and age < _ANSWER_WAIT_HEARTBEAT_STALE_S
