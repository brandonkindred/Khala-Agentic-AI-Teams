"""User Profile Agent - manages and learns user preferences."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from ..models import ProfileUpdateSignal, UserProfile
from ..shared.llm import LLMClient, JSONExtractionFailure
from ..shared.user_profile_store import UserProfileStore
from .models import (
    ExtractedPreference,
    LearnFromTextRequest,
    LearnFromTextResult,
    ProfileExtractionResult,
    ProfileQueryRequest,
    ProfileUpdateRequest,
    ProfileUpdateResult,
)
from .prompts import PROFILE_EXTRACTION_PROMPT, PROFILE_QUERY_PROMPT

logger = logging.getLogger(__name__)


class UserProfileAgent:
    """
    Agent for managing user profiles and learning preferences.
    
    This agent:
    - Maintains comprehensive user profile documents
    - Extracts preferences from conversations and interactions
    - Learns user patterns over time
    - Provides profile information for other agents
    """

    CONFIDENCE_THRESHOLD = 0.7

    def __init__(
        self,
        llm: LLMClient,
        user_id: str,
        profile_store: Optional[UserProfileStore] = None,
    ) -> None:
        """
        Initialize the User Profile Agent.
        
        Args:
            llm: LLM client for extractions
            user_id: The user ID this agent manages
            profile_store: Optional profile store (creates default if not provided)
        """
        self.llm = llm
        self.user_id = user_id
        self.profile_store = profile_store or UserProfileStore()
        
        if not self.profile_store.profile_exists(user_id):
            self.profile_store.create_profile(user_id)

    def get_profile(self) -> UserProfile:
        """Get the current user profile."""
        profile = self.profile_store.load_profile(self.user_id)
        if profile is None:
            profile = self.profile_store.create_profile(self.user_id)
        return profile

    def get_profile_summary(self) -> str:
        """Get a text summary of the profile for use in prompts."""
        summary = self.profile_store.get_profile_summary(self.user_id)
        return summary or "No profile information available."

    def update_profile(self, request: ProfileUpdateRequest) -> ProfileUpdateResult:
        """
        Update the user's profile with new data.
        
        Args:
            request: The update request
            
        Returns:
            ProfileUpdateResult indicating success/failure
        """
        try:
            self.profile_store.update_category(
                user_id=request.user_id,
                category=request.category,
                data=request.data,
                merge=request.merge,
            )
            
            return ProfileUpdateResult(
                success=True,
                updated_fields=list(request.data.keys()),
                message=f"Updated {request.category} profile",
            )
        except Exception as e:
            logger.error("Failed to update profile: %s", e)
            return ProfileUpdateResult(
                success=False,
                message=str(e),
            )

    def extract_preferences(self, text: str) -> ProfileExtractionResult:
        """
        Extract preferences and personal information from text.
        
        Args:
            text: Text to analyze (conversation, email, etc.)
            
        Returns:
            Extracted preferences with confidence scores
        """
        prompt = PROFILE_EXTRACTION_PROMPT.format(text=text)
        
        try:
            data = self.llm.complete_json(
                prompt,
                temperature=0.2,
                expected_keys=["extracted_info", "reasoning"],
            )
        except JSONExtractionFailure as e:
            logger.error("Failed to extract preferences (JSON extraction failed):\n%s", e)
            return ProfileExtractionResult(
                extracted_info=[],
                reasoning=f"JSON extraction failed after multiple recovery attempts. Error: {e.args[0]}"
            )
        except Exception as e:
            logger.error("Failed to extract preferences: %s", e)
            return ProfileExtractionResult(extracted_info=[], reasoning=str(e))
        
        extracted = []
        for item in data.get("extracted_info", []):
            try:
                extracted.append(ExtractedPreference(
                    category=item.get("category", ""),
                    field=item.get("field", ""),
                    value=item.get("value"),
                    confidence=float(item.get("confidence", 0.0)),
                    source_text=text[:200],
                ))
            except Exception as e:
                logger.warning("Failed to parse extracted preference: %s", e)
        
        return ProfileExtractionResult(
            extracted_info=extracted,
            reasoning=data.get("reasoning", ""),
        )

    def learn_from_text(self, request: LearnFromTextRequest) -> LearnFromTextResult:
        """
        Learn about the user from a piece of text.
        
        Args:
            request: The learning request
            
        Returns:
            What was extracted and what was applied
        """
        extraction = self.extract_preferences(request.text)
        
        applied = []
        pending = []
        
        for pref in extraction.extracted_info:
            if pref.confidence >= self.CONFIDENCE_THRESHOLD:
                if request.auto_apply:
                    self._apply_preference(pref)
                    applied.append(pref)
                else:
                    pending.append(pref)
            else:
                pending.append(pref)
        
        return LearnFromTextResult(
            extracted=extraction.extracted_info,
            applied=applied,
            pending_confirmation=pending,
        )

    def _apply_preference(self, pref: ExtractedPreference) -> bool:
        """Apply a single extracted preference to the profile."""
        try:
            value = pref.value
            
            if isinstance(value, str):
                self.profile_store.add_to_list(
                    user_id=self.user_id,
                    category=pref.category,
                    field=pref.field,
                    items=[value],
                )
            elif isinstance(value, list):
                self.profile_store.add_to_list(
                    user_id=self.user_id,
                    category=pref.category,
                    field=pref.field,
                    items=value,
                )
            else:
                self.profile_store.update_category(
                    user_id=self.user_id,
                    category=pref.category,
                    data={pref.field: value},
                    merge=True,
                )
            
            logger.info(
                "Applied preference: %s.%s = %s",
                pref.category, pref.field, pref.value
            )
            return True
        except Exception as e:
            logger.error("Failed to apply preference: %s", e)
            return False

    def confirm_and_apply(self, preferences: List[ExtractedPreference]) -> List[ExtractedPreference]:
        """
        Apply a list of confirmed preferences.
        
        Args:
            preferences: Preferences confirmed by the user
            
        Returns:
            List of successfully applied preferences
        """
        applied = []
        for pref in preferences:
            if self._apply_preference(pref):
                applied.append(pref)
        return applied

    def query_profile(self, request: ProfileQueryRequest) -> str:
        """
        Query the profile using natural language.
        
        Args:
            request: The query request
            
        Returns:
            Natural language response based on profile
        """
        profile = self.get_profile()
        profile_text = self._format_profile_for_query(profile, request.categories)
        
        prompt = PROFILE_QUERY_PROMPT.format(
            profile=profile_text,
            query=request.query,
        )
        
        return self.llm.complete(prompt, temperature=0.3)

    def _format_profile_for_query(
        self,
        profile: UserProfile,
        categories: List[str],
    ) -> str:
        """Format profile data for inclusion in a query prompt."""
        profile_dict = profile.model_dump()
        
        if not categories:
            categories = [
                "identity", "preferences", "goals", "lifestyle",
                "professional", "relationships", "financial",
                "health", "travel", "shopping"
            ]
        
        lines = []
        for cat in categories:
            if cat in profile_dict:
                cat_data = profile_dict[cat]
                if cat_data:
                    lines.append(f"\n## {cat.title()}")
                    for key, value in cat_data.items():
                        if value:
                            if isinstance(value, list) and len(value) > 0:
                                lines.append(f"- {key}: {', '.join(str(v) for v in value[:10])}")
                            elif isinstance(value, dict):
                                for k, v in value.items():
                                    if v:
                                        lines.append(f"- {key}.{k}: {v}")
                            elif isinstance(value, str) and value:
                                lines.append(f"- {key}: {value}")
        
        return "\n".join(lines) if lines else "No profile data available."

    def add_food_like(self, food: str) -> ProfileUpdateResult:
        """Convenience method to add a food the user likes."""
        self.profile_store.add_to_list(
            user_id=self.user_id,
            category="preferences",
            field="food_likes",
            items=[food],
        )
        return ProfileUpdateResult(
            success=True,
            updated_fields=["food_likes"],
            message=f"Added '{food}' to food likes",
        )

    def add_food_dislike(self, food: str) -> ProfileUpdateResult:
        """Convenience method to add a food the user dislikes."""
        self.profile_store.add_to_list(
            user_id=self.user_id,
            category="preferences",
            field="food_dislikes",
            items=[food],
        )
        return ProfileUpdateResult(
            success=True,
            updated_fields=["food_dislikes"],
            message=f"Added '{food}' to food dislikes",
        )

    def add_goal(
        self,
        title: str,
        description: str = "",
        is_long_term: bool = False,
    ) -> ProfileUpdateResult:
        """Add a goal to the user's profile."""
        from uuid import uuid4
        from ..models import Goal, Priority
        
        goal = Goal(
            goal_id=str(uuid4())[:8],
            title=title,
            description=description,
            priority=Priority.MEDIUM,
        )
        
        profile = self.get_profile()
        profile_dict = profile.model_dump()
        
        field = "long_term_goals" if is_long_term else "short_term_goals"
        if field not in profile_dict["goals"]:
            profile_dict["goals"][field] = []
        
        profile_dict["goals"][field].append(goal.model_dump())
        
        self.profile_store.update_category(
            user_id=self.user_id,
            category="goals",
            data=profile_dict["goals"],
            merge=False,
        )
        
        return ProfileUpdateResult(
            success=True,
            updated_fields=[field],
            message=f"Added goal: {title}",
        )

    def set_identity(
        self,
        full_name: Optional[str] = None,
        preferred_name: Optional[str] = None,
        email: Optional[str] = None,
        timezone: Optional[str] = None,
    ) -> ProfileUpdateResult:
        """Set basic identity information."""
        data = {}
        if full_name:
            data["full_name"] = full_name
        if preferred_name:
            data["preferred_name"] = preferred_name
        if email:
            data["email"] = email
        if timezone:
            data["timezone"] = timezone
        
        if not data:
            return ProfileUpdateResult(success=False, message="No data provided")
        
        self.profile_store.update_category(
            user_id=self.user_id,
            category="identity",
            data=data,
            merge=True,
        )
        
        return ProfileUpdateResult(
            success=True,
            updated_fields=list(data.keys()),
            message="Updated identity information",
        )

    def process_signal(self, signal: ProfileUpdateSignal) -> LearnFromTextResult:
        """
        Process a profile update signal from another agent.
        
        Args:
            signal: The update signal
            
        Returns:
            Learning result
        """
        return self.learn_from_text(LearnFromTextRequest(
            user_id=self.user_id,
            text=signal.raw_text,
            source=signal.source,
            auto_apply=signal.confidence >= 0.9,
        ))
