"""Pydantic models for Agent Console Phase 3 data."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Saved inputs
# ---------------------------------------------------------------------------


class SavedInput(BaseModel):
    """One user-saved input payload for a specific agent."""

    id: str
    agent_id: str
    name: str
    input_data: Any
    author: str
    description: str | None = None
    created_at: datetime
    updated_at: datetime


class SavedInputCreate(BaseModel):
    """POST /saved-inputs body."""

    name: str = Field(..., min_length=1, max_length=120)
    input_data: Any
    description: str | None = Field(default=None, max_length=500)


class SavedInputUpdate(BaseModel):
    """PUT /saved-inputs/{id} body — all fields optional."""

    name: str | None = Field(default=None, min_length=1, max_length=120)
    input_data: Any | None = None
    description: str | None = Field(default=None, max_length=500)


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------


class RunSummary(BaseModel):
    """Light projection used by run-history list endpoints."""

    id: str
    agent_id: str
    team: str
    saved_input_id: str | None
    status: Literal["ok", "error"]
    duration_ms: int
    trace_id: str
    author: str
    created_at: datetime


class RunRecord(RunSummary):
    """Full run row including input, output, and logs."""

    input_data: Any
    output_data: Any | None = None
    error: str | None = None
    logs_tail: list[str] = Field(default_factory=list)
    sandbox_url: str | None = None


class RunCreate(BaseModel):
    """Internal shape used by the invoke route to persist a run."""

    agent_id: str
    team: str
    saved_input_id: str | None
    input_data: Any
    output_data: Any | None
    error: str | None
    status: Literal["ok", "error"]
    duration_ms: int
    trace_id: str
    logs_tail: list[str]
    author: str
    sandbox_url: str | None


# ---------------------------------------------------------------------------
# Diff
# ---------------------------------------------------------------------------


class DiffSide(BaseModel):
    """Describes one side of a diff request."""

    kind: Literal["run", "saved_input", "inline"]
    ref: str | None = Field(
        default=None,
        description="ID when kind is 'run' or 'saved_input'; None when kind is 'inline'.",
    )
    data: Any | None = Field(
        default=None,
        description="Inline JSON payload; only used when kind is 'inline'.",
    )
    side: Literal["input", "output"] = Field(
        default="output",
        description="For run refs only: which column to diff. Ignored for other kinds.",
    )


class DiffRequest(BaseModel):
    left: DiffSide
    right: DiffSide


class DiffResult(BaseModel):
    unified_diff: str
    left_label: str
    right_label: str
    is_identical: bool
