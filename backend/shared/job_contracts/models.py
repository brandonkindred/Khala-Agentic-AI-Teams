"""Shared base DTOs for job status/list/cancel/delete responses.

Every team that runs long-lived background jobs (branding_team, blogging,
market_research_team, social_media_marketing_team, software_engineering_team,
...) independently defines its own status-poll, list-item, cancel, and delete
response models. Surveying them shows a consistent common core — ``job_id``,
``status``, ``progress``, ``error``, and created/updated timestamps — with each
team layering its own extra fields on top (e.g. branding's ``client_id``/
``brand_id``/``current_phase``, marketing's ``current_stage``/
``llm_model_name``/``eta_hint``).

This module extracts that common core as base classes, meant to be subclassed
by each team's own response models to fold in the shared fields without
copy-pasting them. It is purely additive: no existing team module is changed
to use these bases here, and no route/service return type is migrated — that
repointing is deliberately left to separate, per-team follow-up work so this
change carries zero behavioral risk to any running team.

``status`` is typed as a plain ``str`` (not an ``Enum``), matching every
existing per-team status DTO surveyed — teams use different, team-specific
vocabularies (pending/running/completed/failed/cancelled, etc.) and an enum
here would either under-cover some team's values or force a lossy shared
vocabulary. Allowed values stay documented, per team, in each subclass or
call site.

Preconditions:
    - ``backend/agents`` is on ``sys.path`` (the ``shared_*`` convention: see
      ``backend/pytest.ini``'s ``pythonpath`` and ``backend/conftest.py``).
Postconditions:
    - Pure data models; importing this module has no side effects beyond
      class definition. ``pydantic`` is the only third-party dependency.
Invariants:
    - Every field beyond ``job_id``/``status`` is ``Optional`` (or carries a
      default), so a base instance can always be constructed from a partial
      dict and safely round-trips through ``model_dump()``/``model_validate()``
      even when a subclass adds its own required fields.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class JobStatusResponseBase(BaseModel):
    """Common fields for a job status-poll response.

    Preconditions:
        - Callers must supply ``job_id`` and ``status`` explicitly (no
          defaults) — every team's real status DTO requires both.
    Postconditions:
        - Instance carries the four fields common across every surveyed
          team's status response; ``progress``/``error``/``created_at``/
          ``updated_at`` default to ``None`` when a team's job record does
          not track them.
    Invariants:
        - A subclass may add its own required or optional fields, and may
          redeclare any of these fields with a narrower/renamed type; both
          are ordinary Pydantic v2 subclassing and are exercised by this
          module's tests.
    """

    job_id: str = Field(..., description="Unique identifier of the job.")
    status: str = Field(
        ...,
        description=(
            "Current lifecycle status of the job (e.g. pending, running, completed, "
            "failed, cancelled); each team defines its own allowed values."
        ),
    )
    progress: Optional[int] = Field(
        default=None,
        description="Percent complete in [0, 100], when the team tracks progress; None otherwise.",
    )
    error: Optional[str] = Field(
        default=None,
        description="Error message when the job failed; None otherwise.",
    )
    created_at: Optional[str] = Field(
        default=None,
        description="Timestamp the job was created (team-defined string format, typically ISO-8601), when tracked.",
    )
    updated_at: Optional[str] = Field(
        default=None,
        description="Timestamp the job was last updated (team-defined string format, typically ISO-8601), when tracked.",
    )


class JobListItemBase(BaseModel):
    """Common fields for one entry in a job-listing response.

    Preconditions:
        - Callers must supply ``job_id`` and ``status`` explicitly (no
          defaults).
    Postconditions:
        - Instance carries the true common denominator across every
          surveyed team's list-item DTO. Per-team extras (``client_id``,
          ``brand_id``, ``phase``, ``brief``, ``repo_path``, ...) are added
          by each team's subclass, not present here.
    Invariants:
        - Same subclassing guarantees as :class:`JobStatusResponseBase`.
    """

    job_id: str = Field(..., description="Unique identifier of the job.")
    status: str = Field(..., description="Current lifecycle status of the job; each team defines its own values.")
    created_at: Optional[str] = Field(
        default=None,
        description="Timestamp the job was created, when tracked.",
    )
    updated_at: Optional[str] = Field(
        default=None,
        description="Timestamp the job was last updated, when tracked.",
    )


class CancelJobResponseBase(BaseModel):
    """Common fields for a job-cancellation response.

    Preconditions:
        - Callers must supply ``job_id`` explicitly (no default).
    Postconditions:
        - Instance carries a job id plus a status/message pair defaulted to
          the values shared by the three teams (blogging,
          social_media_marketing_team, software_engineering_team) that
          already define a typed cancel response; a subclass may override
          either default.
    Invariants:
        - Same subclassing guarantees as :class:`JobStatusResponseBase`.
    """

    job_id: str = Field(..., description="Unique identifier of the cancelled job.")
    status: str = Field(default="cancelled", description="Status reflecting the cancellation request.")
    message: str = Field(
        default="Job cancellation requested.",
        description="Human-readable confirmation message.",
    )


class DeleteJobResponseBase(BaseModel):
    """Common fields for a job-deletion response.

    Preconditions:
        - Callers must supply ``job_id`` explicitly (no default).
    Postconditions:
        - Instance carries a job id plus a confirmation message defaulted to
          the value shared by teams with a typed delete response.
    Invariants:
        - Same subclassing guarantees as :class:`JobStatusResponseBase`.
    """

    job_id: str = Field(..., description="Unique identifier of the deleted job.")
    message: str = Field(default="Job deleted.", description="Human-readable confirmation message.")
