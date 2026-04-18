"""
Agent Console Phase 3 — saved inputs CRUD.

Routes live under ``/api/agents/{agent_id}/saved-inputs`` and
``/api/agents/saved-inputs/{saved_id}``. Storage is delegated to
:class:`agent_console.AgentConsoleStore`.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from agent_console import (
    AgentConsoleStorageUnavailable,
    SavedInput,
    get_store,
    resolve_author,
)
from agent_console.models import SavedInputCreate, SavedInputUpdate
from agent_console.store import SavedInputNameConflict
from agent_registry import get_registry

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agents", tags=["agent-console"])


@router.get("/{agent_id}/saved-inputs", response_model=list[SavedInput])
def list_saved_inputs(agent_id: str) -> list[SavedInput]:
    _require_known_agent(agent_id)
    try:
        return get_store().list_saved_inputs(agent_id)
    except AgentConsoleStorageUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/{agent_id}/saved-inputs", response_model=SavedInput)
def create_saved_input(agent_id: str, body: SavedInputCreate) -> SavedInput:
    _require_known_agent(agent_id)
    try:
        return get_store().create_saved_input(
            agent_id=agent_id,
            name=body.name,
            input_data=body.input_data,
            author=resolve_author(),
            description=body.description,
        )
    except SavedInputNameConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except AgentConsoleStorageUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/saved-inputs/{saved_id}", response_model=SavedInput)
def get_saved_input(saved_id: str) -> SavedInput:
    try:
        result = get_store().get_saved_input(saved_id)
    except AgentConsoleStorageUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail=f"Unknown saved input: {saved_id}")
    return result


@router.put("/saved-inputs/{saved_id}", response_model=SavedInput)
def update_saved_input(saved_id: str, body: SavedInputUpdate) -> SavedInput:
    try:
        updated = get_store().update_saved_input(
            saved_id,
            name=body.name,
            input_data=body.input_data,
            description=body.description,
        )
    except SavedInputNameConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except AgentConsoleStorageUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if updated is None:
        raise HTTPException(status_code=404, detail=f"Unknown saved input: {saved_id}")
    return updated


@router.delete("/saved-inputs/{saved_id}")
def delete_saved_input(saved_id: str) -> dict[str, str]:
    try:
        deleted = get_store().delete_saved_input(saved_id)
    except AgentConsoleStorageUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Unknown saved input: {saved_id}")
    return {"id": saved_id, "status": "deleted"}


def _require_known_agent(agent_id: str) -> None:
    if get_registry().get(agent_id) is None:
        raise HTTPException(status_code=404, detail=f"Unknown agent: {agent_id}")
