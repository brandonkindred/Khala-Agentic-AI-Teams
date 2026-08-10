"""Durable per-call transcript recording for the code review pipeline.

Every LLM call the review pipeline makes (chunk review, false-positive
verification, the merged architecture/side-effect pass, narrative synthesis,
spec-compliance synthesis) can optionally append a ``(stage, target, prompt,
response)`` entry to that review's durable transcript, so a user can inspect
the reviewer's complete "thinking process" once a review job has finished
(see ``review_history_store.append_review_transcript_entry`` for the storage
layer, and the ``code_review_transcripts`` table it writes to).

Design: rather than threading a live recorder object through the coordinator's
deeply recursive, cached, and (for the Temporal path) cross-process call
chain, each call site records directly against whatever ``job_id`` is bound on
:func:`llm_service.current_attribution` for the current thread/task —
``CodeReviewAgent.run`` binds it once, via ``llm_attribution(job_id=...)``,
for the whole in-process review; ``shared.concurrency.parallel_map`` (the map
phase's, the bisection halves', and the tail passes' fan-out mechanism)
propagates that context into every worker thread by default
(``propagate_context=True``), so every actual LLM call made anywhere in the
run sees the same ``job_id`` without any function signature changing. A call
made with no attribution bound (``job_id == ""`` — a caller that never wired
one, e.g. most existing tests and non-job-tracked callers like
``acceptance_verifier_agent``) is a no-op: nothing to attribute the entry to,
and no ``code_review_runs`` row exists for it to attach to anyway.

Recording only happens for a call that actually reached the model: a
map-phase cache hit, a single-flight waiter, or the submission-level
short-circuit never call this module at all, so the transcript reflects real
LLM activity, not replayed cache hits.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from llm_service import current_attribution

logger = logging.getLogger(__name__)


def model_label(model: Any) -> str:
    """Best-effort human-readable identifier for a resolved model/client.

    Mirrors ``mapping._review_model_fingerprint``'s attribute-probing tail
    (duplicated rather than imported: that function also computes the map-phase
    cache fingerprint, a different concern this module has no business coupling
    to), but this copy is purely cosmetic — its output is never hashed into a
    cache key, so a heuristic mismatch is a display nit, not a correctness bug.

    Postconditions:
        - Returns the first non-empty ``model_id``/``model_name``/``model``
          string attribute found on ``model`` (or, for a ``dict``-shaped
          ``.config``, the same lookup within it), else the type name. Never
          raises.
    """
    for attr in ("model_id", "model_name", "model"):
        value = getattr(model, attr, None)
        if isinstance(value, str) and value:
            return value
    config = getattr(model, "config", None)
    if isinstance(config, dict):
        candidate = config.get("model_id") or config.get("model")
        if isinstance(candidate, str) and candidate:
            return candidate
    return type(model).__name__


def record_transcript_entry(
    stage: str,
    target: str,
    prompt: str,
    response: str,
    *,
    model: str = "",
    duration_ms: float = 0.0,
) -> None:
    """Append one completed LLM call to the current job's durable transcript.

    Preconditions:
        - ``stage`` is a short, stable identifier for the pipeline step that made
          this call (e.g. ``"chunk_review"``, ``"false_positive_filter"``,
          ``"architecture_side_effect"``, ``"synthesis"``, ``"spec_compliance"``).
        - ``target`` names what the call covered (a chunk's file label, a
          verification group's file path, or ``""`` for a once-per-submission
          pass); ``prompt``/``response`` are the full text sent to and received
          from the model — never truncated here.
        - ``duration_ms`` is the caller's own measured wall-clock time for the
          call (``0.0`` when not measured); used only to backdate this entry's
          ``started_at`` so entries the reader sorts by that field approximate
          real call order even though concurrent chunk reviews complete out of
          start order.

    Postconditions:
        - When no ``job_id`` is bound on the current attribution context (no
          ``llm_attribution(job_id=...)`` block is active — most tests and every
          caller that never wired one), this is a no-op: there is no
          ``code_review_runs`` row to attach the entry to.
        - Otherwise appends one entry (best-effort; never raises — persistence
          failures are logged and swallowed, matching
          ``review_history_store``'s own write contract) so a reviewer-side
          persistence hiccup can never fail or slow down the review itself
          beyond the one extra Postgres round-trip.
    """
    job_id = current_attribution().job_id
    if not job_id:
        return
    started_at = datetime.now(timezone.utc) - timedelta(milliseconds=max(duration_ms, 0.0))
    entry = {
        "stage": stage,
        "target": target,
        "model": model,
        "prompt": prompt,
        "response": response,
        "started_at": started_at.isoformat(),
        "duration_ms": int(duration_ms),
    }
    try:
        from software_engineering_team.review_history_store import (
            append_review_transcript_entry,
        )

        append_review_transcript_entry(job_id, entry)
    except Exception:  # noqa: BLE001 - transcript recording must never break a review
        logger.warning("CodeReview transcript: failed to record %s entry", stage, exc_info=True)
