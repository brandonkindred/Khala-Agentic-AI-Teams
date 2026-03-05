"""Adapters for delegating to other teams (Market Research, design assets)."""

from .design_assets import request_design_assets
from .market_research import request_market_research

__all__ = ["request_market_research", "request_design_assets"]
