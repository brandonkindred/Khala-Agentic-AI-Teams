"""Neutral, team-agnostic job-store operations (read + HITL pause/answer).

Both the coding team and the software-engineering team wrap the same
``JobServiceClient`` in a team ``job_store`` module. The read and human-in-the-loop
pause/answer operations were byte-identical across the two; this package is their
single home. Each function takes the ``client`` explicitly so a team wrapper passes
its own (monkeypatch-able) ``_client()`` — team-specific ``create_job`` /
``update_job`` / ``list_jobs`` and the extra statuses/fields stay in each team's
thin wrapper.

Preconditions:
    - ``backend/agents`` is on ``sys.path`` (the ``shared_*`` convention).
Postconditions:
    - Stdlib-only; importing has no side effects.
"""

from __future__ import annotations

from shared_job_store.store import (
    add_pending_questions,
    get_job,
    get_submitted_answers,
    is_waiting_for_answers,
    submit_answers,
)

__all__ = [
    "get_job",
    "add_pending_questions",
    "submit_answers",
    "is_waiting_for_answers",
    "get_submitted_answers",
]
