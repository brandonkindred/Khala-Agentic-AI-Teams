"""Pydantic request/response models for the job service API."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class CreateJobRequest(BaseModel):
    job_id: str
    status: str = "pending"
    fields: dict[str, Any] = {}


class ReplaceJobRequest(BaseModel):
    payload: dict[str, Any]


class UpdateJobRequest(BaseModel):
    heartbeat: bool = True
    fields: dict[str, Any] = {}


class ApplyPatchRequest(BaseModel):
    merge_fields: dict[str, Any] | None = None
    merge_nested: dict[str, Any] | None = None
    append_to: dict[str, list[Any]] | None = None
    increment: dict[str, int] | None = None


class AppendEventRequest(BaseModel):
    action: str
    outcome: str | None = None
    details: dict[str, Any] | None = None
    status: str | None = None


class MarkStaleRequest(BaseModel):
    stale_after_seconds: float
    reason: str
    waiting_field: str = "waiting_for_answers"


class MarkAllFailedRequest(BaseModel):
    reason: str


class JobResponse(BaseModel):
    """Single job data returned from get/create endpoints."""

    job: dict[str, Any] | None = None


class JobListResponse(BaseModel):
    """List of jobs returned from list endpoint."""

    jobs: list[dict[str, Any]]


class DeleteResponse(BaseModel):
    deleted: bool


class MarkStaleResponse(BaseModel):
    failed_job_ids: list[str]


class HealthResponse(BaseModel):
    status: str
    service: str = "job-service"
