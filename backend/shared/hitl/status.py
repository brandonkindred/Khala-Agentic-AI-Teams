"""Materialization of stored pending-question records into typed models.

Used by the job-status routes to turn the raw ``pending_questions`` list stored
on a job record into :class:`PendingQuestion` instances for the response.
"""

from __future__ import annotations

from typing import Any, List

from shared_hitl.models import PendingQuestion


def pending_questions_from_raw(raw: List[Any]) -> List[PendingQuestion]:
    """Build :class:`PendingQuestion` models from a stored ``pending_questions`` list.

    Preconditions:
        - ``raw`` is the stored ``pending_questions`` value (an iterable; entries
          are expected to be dicts but may be malformed).
    Postconditions:
        - Returns one :class:`PendingQuestion` per dict entry, built with
          ``model_validate`` so **every** field the record carries is preserved
          (including ``recommendation``/``allow_multiple`` and nested option
          ``rationale``/``confidence``) — a full-fidelity round-trip, not a
          hand-enumerated subset.
        - Non-dict entries are skipped, so a corrupted record cannot raise.
        - Raises ``pydantic.ValidationError`` if a dict entry is missing a
          required field (``id``/``question_text``) or has a mistyped one — the
          same failure the equivalent direct construction would raise.
    """
    return [PendingQuestion.model_validate(q) for q in raw if isinstance(q, dict)]
