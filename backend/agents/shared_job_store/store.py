"""Team-agnostic job-store operations shared by every ``JobServiceClient`` wrapper.

The coding team and the software-engineering team each keep a thin ``job_store``
module that binds a pooled ``JobServiceClient`` for their team and adds
team-specific fields/statuses. The read + human-in-the-loop (HITL) pause/answer
operations below, however, are byte-identical across both teams, so they live here
once.

Each function takes the ``client`` explicitly (rather than resolving it) so a team
wrapper can pass its own ``_client()`` — which is exactly what test fixtures
monkeypatch — keeping client interception intact through the delegation.

Preconditions (all functions):
    - ``client`` is a ``JobServiceClient``-like object (``get_job`` / ``atomic_update``).
    - ``job_id`` is a non-empty job identifier.
Postconditions:
    - No function raises for a missing job beyond what the underlying transport does;
      the boolean/list accessors return ``False`` / ``[]`` when the job is absent.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


def get_job(client: Any, job_id: str) -> Optional[Dict[str, Any]]:
    """Return the job record, or ``None`` if not found."""
    return client.get_job(job_id)


def add_pending_questions(client: Any, job_id: str, questions: List[Dict[str, Any]]) -> None:
    """Append pending questions and set ``waiting_for_answers=True`` to pause the job.

    Postconditions: the job's ``waiting_for_answers`` is True and ``questions`` are
    appended to ``pending_questions`` (atomic, so a concurrent answer submission and
    a status update cannot clobber each other).
    """
    client.atomic_update(
        job_id,
        merge_fields={"waiting_for_answers": True},
        append_to={"pending_questions": questions},
    )


def submit_answers(client: Any, job_id: str, answers: List[Dict[str, Any]]) -> None:
    """Store submitted answers, clear pending questions, and clear the waiting flag.

    Postconditions: ``waiting_for_answers`` is False, ``pending_questions`` is empty,
    and ``answers`` are appended to ``submitted_answers`` (the orchestrator's wait
    loop resumes on the cleared flag and reads ``submitted_answers``).
    """
    client.atomic_update(
        job_id,
        merge_fields={"pending_questions": [], "waiting_for_answers": False},
        append_to={"submitted_answers": answers},
    )


def is_waiting_for_answers(client: Any, job_id: str) -> bool:
    """True iff the job is currently paused waiting for user answers."""
    data = client.get_job(job_id)
    return bool(data.get("waiting_for_answers", False)) if data else False


def get_submitted_answers(client: Any, job_id: str) -> List[Dict[str, Any]]:
    """Return the answers submitted for this job (empty when none/unknown).

    Coerces a stored ``None`` to ``[]`` (``... or []``) so a partially-written
    record never yields ``None`` to callers.
    """
    data = client.get_job(job_id)
    return list(data.get("submitted_answers") or []) if data else []
