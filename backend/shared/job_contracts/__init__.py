"""Shared base DTOs for job status/list/cancel/delete responses.

Base classes teams subclass to add their own extra fields on top of the
common job-response core (``job_id``, ``status``, ``progress``, ``error``,
timestamps). See ``models`` for the full contract and rationale.

Layout:
    - ``models`` — the four base Pydantic models.

Preconditions:
    - ``backend/agents`` is on ``sys.path`` (the ``shared_*`` convention).
Postconditions:
    - Importing has no side effects beyond class definition.
"""

from __future__ import annotations

from shared.job_contracts.models import (
    CancelJobResponseBase,
    DeleteJobResponseBase,
    JobListItemBase,
    JobStatusResponseBase,
)

__all__ = [
    "CancelJobResponseBase",
    "DeleteJobResponseBase",
    "JobListItemBase",
    "JobStatusResponseBase",
]
