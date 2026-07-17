"""Branding API — client endpoints (create / list / get)."""

from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, HTTPException, Query

from branding_team.api import main as _main
from branding_team.api.models import CreateClientRequest
from branding_team.models import Client

router = APIRouter()


@router.post("/clients", response_model=Client, status_code=201)
def create_client(payload: CreateClientRequest) -> Client:
    return _main.branding_store.create_client(
        name=payload.name,
        contact_info=payload.contact_info,
        notes=payload.notes,
    )


@router.get("/clients", response_model=List[Client])
def list_clients(
    limit: Optional[int] = Query(None, gt=0),
    offset: int = Query(0, ge=0),
) -> List[Client]:
    """List clients, optionally paginated.

    ``limit``/``offset`` are validated by FastAPI (``gt=0`` / ``ge=0``), so
    out-of-range input yields a 422 rather than reaching the store's
    ``_validate_pagination`` guard and surfacing as a 500.
    """
    return _main.branding_store.list_clients(limit=limit, offset=offset)


@router.get("/clients/{client_id}", response_model=Client)
def get_client(client_id: str) -> Client:
    client = _main.branding_store.get_client(client_id)
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")
    return client
