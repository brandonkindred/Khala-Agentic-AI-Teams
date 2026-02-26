"""Deal Finder Agent - finds deals matching user preferences."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4

from ..models import Deal
from ..shared.llm import LLMClient, JSONExtractionFailure
from ..shared.user_profile_store import UserProfileStore
from ..tools.web_search import SearchResult, WebSearchTool
from ..tools.web_fetch import WebFetchTool
from .models import (
    AddWishlistRequest,
    DealMatch,
    DealSearchResult,
    SearchDealsRequest,
    WishlistItem,
)
from .prompts import DEAL_RELEVANCE_PROMPT, EXTRACT_DEAL_INFO_PROMPT, GENERATE_SEARCH_QUERIES_PROMPT

logger = logging.getLogger(__name__)


class DealFinderAgent:
    """
    Agent for finding deals that match user preferences.
    
    Capabilities:
    - Search for deals across the web
    - Score deals based on user preferences
    - Track wishlist items for price alerts
    - Generate personalized deal recommendations
    """

    def __init__(
        self,
        llm: LLMClient,
        profile_store: Optional[UserProfileStore] = None,
        storage_dir: Optional[str] = None,
    ) -> None:
        """
        Initialize the Deal Finder Agent.
        
        Args:
            llm: LLM client for relevance scoring
            profile_store: User profile storage
            storage_dir: Directory for wishlist storage
        """
        self.llm = llm
        self.profile_store = profile_store or UserProfileStore()
        self.storage_dir = Path(
            storage_dir or os.getenv("PA_DEALS_DIR", ".agent_cache/deals")
        )
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        
        self.web_search = WebSearchTool()
        self.web_fetch = WebFetchTool()

    def _get_wishlist_file(self, user_id: str) -> Path:
        """Get path to user's wishlist file."""
        return self.storage_dir / f"{user_id}_wishlist.json"

    def _load_wishlist(self, user_id: str) -> List[WishlistItem]:
        """Load user's wishlist."""
        file_path = self._get_wishlist_file(user_id)
        if not file_path.exists():
            return []
        
        try:
            data = json.loads(file_path.read_text())
            return [WishlistItem(**item) for item in data]
        except Exception as e:
            logger.error("Failed to load wishlist: %s", e)
            return []

    def _save_wishlist(self, user_id: str, wishlist: List[WishlistItem]) -> None:
        """Save user's wishlist."""
        file_path = self._get_wishlist_file(user_id)
        data = [item.model_dump() for item in wishlist]
        file_path.write_text(json.dumps(data, indent=2))

    def add_to_wishlist(self, request: AddWishlistRequest) -> WishlistItem:
        """
        Add an item to the user's wishlist.
        
        Args:
            request: Add wishlist request
            
        Returns:
            Created WishlistItem
        """
        wishlist = self._load_wishlist(request.user_id)
        
        item = WishlistItem(
            item_id=str(uuid4())[:8],
            user_id=request.user_id,
            description=request.description,
            target_price=request.target_price,
            category=request.category,
            keywords=request.keywords or [request.description],
            created_at=datetime.utcnow().isoformat(),
        )
        
        wishlist.append(item)
        self._save_wishlist(request.user_id, wishlist)
        
        return item

    def remove_from_wishlist(self, user_id: str, item_id: str) -> bool:
        """Remove an item from the wishlist."""
        wishlist = self._load_wishlist(user_id)
        original_len = len(wishlist)
        wishlist = [i for i in wishlist if i.item_id != item_id]
        
        if len(wishlist) < original_len:
            self._save_wishlist(user_id, wishlist)
            return True
        return False

    def get_wishlist(self, user_id: str) -> List[WishlistItem]:
        """Get user's wishlist."""
        return self._load_wishlist(user_id)

    def search_deals(self, request: SearchDealsRequest) -> DealSearchResult:
        """
        Search for deals.
        
        Args:
            request: Search request
            
        Returns:
            DealSearchResult with matched deals
        """
        query = request.query
        
        if not query:
            query = self._generate_search_query(request.user_id, request.category)
        
        search_query = f"{query} deals discounts sale"
        
        try:
            results = self.web_search.search_deals(query, max_results=request.max_results)
        except Exception as e:
            logger.error("Web search failed: %s", e)
            return DealSearchResult(deals=[], total_found=0, query_used=search_query)
        
        deals = self._process_search_results(request.user_id, results)
        
        return DealSearchResult(
            deals=deals,
            total_found=len(deals),
            query_used=search_query,
        )

    def _generate_search_query(self, user_id: str, category: Optional[str]) -> str:
        """Generate a search query based on user preferences."""
        profile = self.profile_store.load_profile(user_id)
        wishlist = self._load_wishlist(user_id)
        
        if wishlist:
            return wishlist[0].description
        
        if category:
            return category
        
        if profile:
            if profile.preferences.brands_liked:
                return profile.preferences.brands_liked[0]
            if profile.preferences.hobbies:
                return profile.preferences.hobbies[0]
        
        return "best deals today"

    def _process_search_results(
        self,
        user_id: str,
        results: List[SearchResult],
    ) -> List[DealMatch]:
        """Process search results and score deals."""
        deals = []
        
        for result in results:
            deal = DealMatch(
                deal_id=str(uuid4())[:8],
                title=result.title,
                description=result.snippet,
                url=result.url,
                store=self._extract_store_name(str(result.url)),
            )
            
            scored = self._score_deal(user_id, deal)
            deals.append(scored)
        
        deals.sort(key=lambda d: d.relevance_score, reverse=True)
        return deals

    def _extract_store_name(self, url: str) -> str:
        """Extract store name from URL."""
        from urllib.parse import urlparse
        domain = urlparse(url).netloc
        domain = domain.replace("www.", "")
        parts = domain.split(".")
        if len(parts) >= 2:
            return parts[0].title()
        return domain

    def _score_deal(self, user_id: str, deal: DealMatch) -> DealMatch:
        """Score a deal's relevance to the user."""
        profile = self.profile_store.load_profile(user_id)
        wishlist = self._load_wishlist(user_id)
        
        preferences = ""
        discount_threshold = 20.0
        
        if profile:
            if profile.preferences.brands_liked:
                preferences += f"Liked brands: {', '.join(profile.preferences.brands_liked)}\n"
            if profile.preferences.hobbies:
                preferences += f"Hobbies: {', '.join(profile.preferences.hobbies)}\n"
            if profile.shopping.favorite_stores:
                preferences += f"Favorite stores: {', '.join(profile.shopping.favorite_stores)}\n"
            if profile.shopping.wishlist_items:
                preferences += f"Wishlist: {', '.join(profile.shopping.wishlist_items[:5])}\n"
            discount_threshold = profile.financial.deal_alert_threshold_pct
        
        wishlist_text = "\n".join(
            f"- {item.description} (target: ${item.target_price})" if item.target_price
            else f"- {item.description}"
            for item in wishlist[:5]
        ) or "No wishlist items"
        
        prompt = DEAL_RELEVANCE_PROMPT.format(
            preferences=preferences or "No preferences available",
            wishlist=wishlist_text,
            title=deal.title,
            description=deal.description,
            store=deal.store,
            original_price=deal.original_price or "Unknown",
            sale_price=deal.sale_price or "Unknown",
            discount_percent=deal.discount_percent or "Unknown",
            discount_threshold=discount_threshold,
        )
        
        try:
            data = self.llm.complete_json(
                prompt,
                temperature=0.2,
                expected_keys=["relevance_score", "matching_preferences"],
            )
            deal.relevance_score = float(data.get("relevance_score", 0.5))
            deal.matching_preferences = data.get("matching_preferences", [])
        except JSONExtractionFailure as e:
            logger.warning("Failed to score deal (JSON extraction failed):\n%s", e)
            deal.relevance_score = 0.5
        except Exception as e:
            logger.warning("Failed to score deal: %s", e)
            deal.relevance_score = 0.5
        
        return deal

    def find_deals_for_wishlist(self, user_id: str) -> List[DealMatch]:
        """
        Find deals matching wishlist items.
        
        Args:
            user_id: The user ID
            
        Returns:
            List of matching deals
        """
        wishlist = self._load_wishlist(user_id)
        if not wishlist:
            return []
        
        all_deals = []
        
        for item in wishlist[:5]:
            try:
                results = self.web_search.search_deals(
                    item.description,
                    max_results=5,
                )
                
                for result in results:
                    deal = DealMatch(
                        deal_id=str(uuid4())[:8],
                        title=result.title,
                        description=result.snippet,
                        url=result.url,
                        store=self._extract_store_name(str(result.url)),
                    )
                    
                    if item.target_price and deal.sale_price:
                        if deal.sale_price <= item.target_price:
                            deal.relevance_score = 1.0
                            deal.matching_preferences.append(
                                f"Matches wishlist: {item.description}"
                            )
                    else:
                        deal.relevance_score = 0.8
                        deal.matching_preferences.append(
                            f"Related to wishlist: {item.description}"
                        )
                    
                    all_deals.append(deal)
            except Exception as e:
                logger.warning("Failed to search for wishlist item %s: %s", item.description, e)
        
        all_deals.sort(key=lambda d: d.relevance_score, reverse=True)
        return all_deals[:10]

    def get_personalized_deals(self, user_id: str) -> List[DealMatch]:
        """
        Get personalized deal recommendations.
        
        Args:
            user_id: The user ID
            
        Returns:
            List of recommended deals
        """
        profile = self.profile_store.load_profile(user_id)
        wishlist = self._load_wishlist(user_id)
        
        preferences = ""
        if profile:
            if profile.preferences.brands_liked:
                preferences += f"Brands: {', '.join(profile.preferences.brands_liked[:5])}\n"
            if profile.preferences.hobbies:
                preferences += f"Hobbies: {', '.join(profile.preferences.hobbies[:5])}\n"
        
        wishlist_text = ", ".join(i.description for i in wishlist[:5])
        
        prompt = GENERATE_SEARCH_QUERIES_PROMPT.format(
            preferences=preferences or "General interests",
            wishlist=wishlist_text or "None",
            recent_interests="Not tracked",
        )
        
        try:
            data = self.llm.complete_json(
                prompt,
                temperature=0.4,
                expected_keys=["queries"],
            )
            queries = data.get("queries", [])
        except JSONExtractionFailure as e:
            logger.error("Failed to generate queries (JSON extraction failed):\n%s", e)
            queries = [{"query": "best deals today", "priority": "medium"}]
        except Exception as e:
            logger.error("Failed to generate queries: %s", e)
            queries = [{"query": "best deals today", "priority": "medium"}]
        
        all_deals = []
        
        for query_data in queries[:3]:
            try:
                query = query_data.get("query", "")
                results = self.web_search.search_deals(query, max_results=5)
                deals = self._process_search_results(user_id, results)
                all_deals.extend(deals)
            except Exception as e:
                logger.warning("Search failed for query: %s", e)
        
        seen_titles = set()
        unique_deals = []
        for deal in all_deals:
            if deal.title not in seen_titles:
                seen_titles.add(deal.title)
                unique_deals.append(deal)
        
        unique_deals.sort(key=lambda d: d.relevance_score, reverse=True)
        return unique_deals[:10]
