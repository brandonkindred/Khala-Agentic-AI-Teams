"""Adapter to request design assets (stub; calls a design service when configured)."""

from __future__ import annotations

import os
from typing import Optional, Union
from uuid import uuid4

from branding_team.models import DesignAssetRequestResult, StrategicCoreOutput


def _design_service_url() -> Optional[str]:
    return os.environ.get("BRANDING_DESIGN_SERVICE_URL") or os.environ.get("UNIFIED_API_BASE_URL")


def request_design_assets(
    strategic_core: Union[StrategicCoreOutput, object],
    brand_name: str = "",
) -> DesignAssetRequestResult:
    """Request design assets for a brand proposal.

    Accepts a ``StrategicCoreOutput`` (or any object with a
    ``positioning_statement`` attribute) so callers can pass Phase 1 output
    directly.
    """
    base = _design_service_url()
    request_id = f"design_{uuid4().hex[:12]}"

    positioning = getattr(strategic_core, "positioning_statement", "")
    if base:
        # Future: POST to a configured design service.
        pass

    return DesignAssetRequestResult(
        request_id=request_id,
        status="pending",
        artifacts=[
            "Design request queued; attach a design service when available.",
            f"Brand direction: {positioning[:100]}..." if positioning else "No positioning yet.",
        ],
    )
