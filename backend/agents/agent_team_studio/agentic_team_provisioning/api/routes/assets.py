"""Agentic team provisioning API — team asset (file system) endpoints.

Handlers delegate to ``api.services.assets`` so business logic stays out of the router.
"""

from __future__ import annotations

from typing import List

from fastapi import APIRouter, UploadFile

from agent_team_studio.agentic_team_provisioning.api.services import assets as assets_svc
from agent_team_studio.agentic_team_provisioning.models import AssetInfo

router = APIRouter()


@router.get("/teams/{team_id}/assets", response_model=List[AssetInfo])
def list_team_assets(team_id: str):
    """See ``api.services.assets.list_team_assets`` for the full contract."""
    return assets_svc.list_team_assets(team_id)


@router.get("/teams/{team_id}/assets/{name}")
def download_team_asset(team_id: str, name: str):
    """See ``api.services.assets.download_team_asset`` for the full contract."""
    return assets_svc.download_team_asset(team_id, name)


@router.post("/teams/{team_id}/assets", response_model=AssetInfo)
async def upload_team_asset(team_id: str, file: UploadFile):
    """See ``api.services.assets.upload_team_asset`` for the full contract."""
    return await assets_svc.upload_team_asset(team_id, file)
