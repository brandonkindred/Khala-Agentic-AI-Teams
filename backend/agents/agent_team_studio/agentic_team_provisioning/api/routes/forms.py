"""Agentic team provisioning API — team form-record (database) endpoints.

Handlers delegate to ``api.services.forms`` so business logic stays out of the router.
"""

from __future__ import annotations

from typing import List

from fastapi import APIRouter

from agent_team_studio.agentic_team_provisioning.api.services import forms as forms_svc
from agent_team_studio.agentic_team_provisioning.models import (
    CreateFormRecordRequest,
    FormRecord,
    UpdateFormRecordRequest,
)

router = APIRouter()


@router.get("/teams/{team_id}/forms", response_model=List[str])
def list_team_form_keys(team_id: str):
    """See ``api.services.forms.list_team_form_keys`` for the full contract."""
    return forms_svc.list_team_form_keys(team_id)


@router.get("/teams/{team_id}/forms/{form_key}", response_model=List[FormRecord])
def list_team_form_records(team_id: str, form_key: str):
    """See ``api.services.forms.list_team_form_records`` for the full contract."""
    return forms_svc.list_team_form_records(team_id, form_key)


@router.post("/teams/{team_id}/forms/{form_key}", response_model=FormRecord, status_code=201)
def create_team_form_record(team_id: str, form_key: str, req: CreateFormRecordRequest):
    """See ``api.services.forms.create_team_form_record`` for the full contract."""
    return forms_svc.create_team_form_record(team_id, form_key, req)


@router.put("/teams/{team_id}/forms/{form_key}/{record_id}", response_model=FormRecord)
def update_team_form_record(
    team_id: str, form_key: str, record_id: str, req: UpdateFormRecordRequest
):
    """See ``api.services.forms.update_team_form_record`` for the full contract."""
    return forms_svc.update_team_form_record(team_id, form_key, record_id, req)


@router.delete("/teams/{team_id}/forms/{form_key}/{record_id}", status_code=204)
def delete_team_form_record(team_id: str, form_key: str, record_id: str):
    """See ``api.services.forms.delete_team_form_record`` for the full contract."""
    return forms_svc.delete_team_form_record(team_id, form_key, record_id)
