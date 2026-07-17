"""Branding API — outsourcing endpoints (market research + design assets)."""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException

from branding_team.api import background as _bg
from branding_team.api import main as _main
from branding_team.models import (
    BrandPhase,
    CompetitiveSnapshot,
    DesignAssetRequestResult,
    HumanReview,
)

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post(
    "/clients/{client_id}/brands/{brand_id}/request-market-research",
    response_model=CompetitiveSnapshot,
)
async def request_market_research_for_brand(client_id: str, brand_id: str) -> CompetitiveSnapshot:
    """Fetch a competitive snapshot for a brand from the Market Research team.

    Async so the (potentially multi-minute) status polling yields to the event
    loop instead of holding a worker thread. 404 if the brand is unknown; 503
    if the market-research service is unconfigured or fails.
    """
    # get_brand is a synchronous (blocking) DB call — run it off the event loop.
    brand = await asyncio.to_thread(_main.branding_store.get_brand, client_id, brand_id)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    try:
        from branding_team.adapters.market_research import request_market_research_async

        snapshot = await request_market_research_async(brand.mission)
    except Exception:
        # Surface the real cause (transport error, bad response, timeout) in the
        # logs; the client still only sees an opaque 503.
        logger.exception("Market research request failed for brand %s", brand_id)
        raise HTTPException(status_code=503, detail="Market research service unavailable")
    if not snapshot:
        raise HTTPException(status_code=503, detail="Market research service unavailable")
    return snapshot


@router.post(
    "/clients/{client_id}/brands/{brand_id}/request-design-assets",
    response_model=DesignAssetRequestResult,
)
async def request_design_assets_for_brand(
    client_id: str, brand_id: str
) -> DesignAssetRequestResult:
    """Request design assets for a brand's strategic core.

    Reuses the strategic core persisted by a prior pipeline run when present
    (``brand.latest_output.strategic_core``) — the design-asset request only
    reads the positioning statement — and falls back to running Phase 1 only
    when no cached core exists. Async so the blocking store read and the (rare)
    Phase 1 fallback run off the event loop instead of holding a worker thread.
    404 if the brand is unknown.
    """
    brand = await asyncio.to_thread(_main.branding_store.get_brand, client_id, brand_id)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    from branding_team.adapters.design_assets import request_design_assets

    cached = brand.latest_output.strategic_core if brand.latest_output else None
    if cached is not None:
        strategic_core = cached
    else:
        # No persisted strategic core yet: run Phase 1 once, off the event loop
        # and on the bounded pipeline executor (not the shared default one).
        phase1_result = await _bg._run_in_pipeline_executor(
            _main.orchestrator.run_phase,
            brand.mission,
            BrandPhase.STRATEGIC_CORE,
            HumanReview(approved=True),
        )
        strategic_core = phase1_result.strategic_core
    return request_design_assets(strategic_core, brand.mission.company_name)
