"""coding_team API — shared run-thread registry, timing constants, and pure answer/progress helpers.

Monkeypatched collaborators are dereferenced through the ``main`` module object
at call time so ``monkeypatch.setattr(main, ...)`` keeps taking effect after the
split; models are imported directly.

Invariants:
    - The run-thread registry itself lives in ``shared.run_thread_registry.RunThreadRegistry``;
      ``_active_run_threads``/``_starting_run_jobs``/``_run_thread_lock`` are back-compat aliases
      onto its live internals, so background threads observe the same maps regardless of whether
      they go through the registry or poke these aliases directly.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from shared.hitl.progress import coerce_progress
from shared.hitl.validation import validate_answers
from shared.run_thread_registry import RunThreadRegistry
from software_engineering_team.api.coding_team_models import (
    SubmitAnswersRequest,
)

logger = logging.getLogger(__name__)

# Tracks the orchestrator thread per job (legacy block-mode / github-hook paths). Temporal-native
# pauses do not register here — resume is via workflow signal, not thread restart.
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

    Thin wrapper over ``shared.hitl.progress.coerce_progress`` (see it for the full
    contract). Kept as a named function on this module so the ``main`` re-export and
    its ``monkeypatch.setattr(main, ...)`` target are unchanged after the extraction.
    """
    return coerce_progress(value)


def _validate_answers(data: Dict[str, Any], request: SubmitAnswersRequest) -> List[Dict[str, Any]]:
    """Validate submitted answers against the job's pending questions; return them as plain dicts.

    Thin wrapper over ``shared.hitl.validation.validate_answers`` (see it for the full
    contract: the 400/500 rule set and the ``question_text``-carrying return shape).
    Kept as a named function on this module so the ``main`` re-export and its
    ``monkeypatch.setattr(main, ...)`` target are unchanged after the extraction.
    """
    return validate_answers(data, request)


# Tolerated clock skew between worker hosts: a heartbeat stamped up to this many seconds in the
# future (relative to the checking worker) is still treated as fresh. This covers NTP drift in
# multi-host deployments without blocking admission indefinitely on a far-future/corrupt stamp.
# Shared by PR-review admission (``_review_job_heartbeat_live``).
_HEARTBEAT_CLOCK_SKEW_TOLERANCE_S = 10.0

# GitHub returns 422 Unprocessable Entity for validation errors — specifically a
# review comment whose line is off the diff. Only a 422 is recoverable by
# dropping/demoting the offending comment; other statuses signal a real failure.
_HTTP_UNPROCESSABLE = 422

# Body for the extra COMMENT review(s) the bisection path submits after the
# summary has already been posted on its own — so they don't repeat the summary.
_BISECT_CONTINUATION_BODY = "*(continued — additional findings)*"
