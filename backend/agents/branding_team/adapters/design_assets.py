"""Adapter to request design assets (stub; calls a design service when configured)."""

from __future__ import annotations

import os
from typing import Optional
from uuid import uuid4

from branding_team.models import BrandCodification, DesignAssetRequestResult


def _design_service_url() -> Optional[str]:
    return os.environ.get("BRANDING_DESIGN_SERVICE_URL") or os.environ.get("UNIFIED_API_BASE_URL")


def request_design_assets(
    codification: BrandCodification,
    brand_name: str = "",
) -> DesignAssetRequestResult:
    """
    Request design assets for a brand proposal. If BRANDING_DESIGN_SERVICE_URL or
    a compatible design service is configured, call it; otherwise return a structured stub.
    """
    base = _design_service_url()
    request_id = f"design_{uuid4().hex[:12]}"
    if base:
        # Future: POST to a configured design service with intake derived from codification.
        # For now we still return stub until a design service is wired up and contract is defined.
        pass
    return DesignAssetRequestResult(
        request_id=request_id,
        status="pending",
        artifacts=[
            "Design request queued; attach a design service when available.",
            f"Brand direction: {codification.positioning_statement[:100]}...",
        ],
    )
