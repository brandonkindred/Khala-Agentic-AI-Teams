"""Models for the Deal Finder Agent."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, HttpUrl


class SearchDealsRequest(BaseModel):
    """Request to search for deals."""

    user_id: str
    query: Optional[str] = None
    category: Optional[str] = None
    max_results: int = 10


class DealMatch(BaseModel):
    """A deal matched to user preferences."""

    deal_id: str
    title: str
    description: str
    original_price: Optional[float] = None
    sale_price: Optional[float] = None
    discount_percent: Optional[float] = None
    url: Optional[HttpUrl] = None
    store: str = ""
    category: str = ""
    relevance_score: float = 0.0
    matching_preferences: List[str] = Field(default_factory=list)
    expires_at: Optional[str] = None


class DealAlert(BaseModel):
    """An alert for a matching deal."""

    user_id: str
    deal: DealMatch
    alert_reason: str
    created_at: str


class WishlistItem(BaseModel):
    """An item on the user's wishlist for deal tracking."""

    item_id: str
    user_id: str
    description: str
    target_price: Optional[float] = None
    category: str = ""
    keywords: List[str] = Field(default_factory=list)
    created_at: str


class AddWishlistRequest(BaseModel):
    """Request to add a wishlist item."""

    user_id: str
    description: str
    target_price: Optional[float] = None
    category: str = ""
    keywords: List[str] = Field(default_factory=list)


class DealSearchResult(BaseModel):
    """Result of a deal search."""

    deals: List[DealMatch]
    total_found: int
    query_used: str
