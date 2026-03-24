"""User profile persistence with file-based storage."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from ..models import UserProfile

logger = logging.getLogger(__name__)


class UserProfileStoreError(Exception):
    """Raised when profile operations fail."""


class UserProfileStore:
    """
    File-based storage for user profiles.

    Profiles are stored as YAML files organized by category for easy
    human readability and editing. The store also maintains a consolidated
    JSON file for quick loading.
    """

    PROFILE_CATEGORIES = [
        "identity",
        "preferences",
        "goals",
        "lifestyle",
        "professional",
        "relationships",
        "financial",
        "health",
        "travel",
        "shopping",
    ]

    def __init__(self, storage_dir: Optional[str] = None) -> None:
        """
        Initialize the profile store.

        Args:
            storage_dir: Directory for storing profiles.
                        Defaults to .agent_cache/user_profiles/
        """
        self.storage_dir = Path(
            storage_dir or os.getenv("PA_PROFILE_DIR", ".agent_cache/user_profiles")
        )
        self.storage_dir.mkdir(parents=True, exist_ok=True)

    def _get_user_dir(self, user_id: str) -> Path:
        """Get the profile directory for a user (does not create it)."""
        return self.storage_dir / user_id

    def _ensure_user_dir(self, user_id: str) -> Path:
        """Get the profile directory for a user, creating it if necessary."""
        user_dir = self.storage_dir / user_id
        user_dir.mkdir(parents=True, exist_ok=True)
        return user_dir

    def _get_category_file(self, user_id: str, category: str) -> Path:
        """Get the path to a category YAML file."""
        return self._get_user_dir(user_id) / f"{category}.yaml"

    def _get_consolidated_file(self, user_id: str) -> Path:
        """Get the path to the consolidated profile JSON."""
        return self._get_user_dir(user_id) / "profile.json"

    def create_profile(self, user_id: str) -> UserProfile:
        """
        Create a new empty profile for a user.

        Args:
            user_id: The user's ID

        Returns:
            A new UserProfile instance
        """
        profile = UserProfile(user_id=user_id)
        self.save_profile(profile)
        logger.info("Created new profile for user %s", user_id)
        return profile

    def load_profile(self, user_id: str) -> Optional[UserProfile]:
        """
        Load a user's complete profile.

        Args:
            user_id: The user's ID

        Returns:
            UserProfile or None if not found
        """
        consolidated_file = self._get_consolidated_file(user_id)

        if consolidated_file.exists():
            try:
                data = json.loads(consolidated_file.read_text())
                return UserProfile(**data)
            except Exception as e:
                logger.error("Failed to load consolidated profile for %s: %s", user_id, e)

        if not self._get_user_dir(user_id).exists():
            return None

        return self._load_from_category_files(user_id)

    def _load_from_category_files(self, user_id: str) -> Optional[UserProfile]:
        """Load profile by reading individual category YAML files."""
        user_dir = self._get_user_dir(user_id)

        if not user_dir.exists():
            return None

        profile_data: Dict[str, Any] = {"user_id": user_id}

        for category in self.PROFILE_CATEGORIES:
            category_file = self._get_category_file(user_id, category)
            if category_file.exists():
                try:
                    data = yaml.safe_load(category_file.read_text())
                    if data:
                        profile_data[category] = data
                except Exception as e:
                    logger.warning("Failed to load %s for user %s: %s", category, user_id, e)

        try:
            return UserProfile(**profile_data)
        except Exception as e:
            logger.error("Failed to construct profile for %s: %s", user_id, e)
            return None

    def save_profile(self, profile: UserProfile) -> None:
        """
        Save a user's complete profile.

        Saves both individual YAML files per category and a consolidated JSON.

        Args:
            profile: The UserProfile to save
        """
        profile.updated_at = datetime.utcnow().isoformat()
        self._ensure_user_dir(profile.user_id)

        profile_dict = profile.model_dump()

        for category in self.PROFILE_CATEGORIES:
            if category in profile_dict:
                category_file = self._get_category_file(profile.user_id, category)
                try:
                    yaml_content = yaml.dump(
                        profile_dict[category],
                        default_flow_style=False,
                        allow_unicode=True,
                        sort_keys=False,
                    )
                    category_file.write_text(yaml_content)
                except Exception as e:
                    logger.error("Failed to save %s for user %s: %s", category, profile.user_id, e)

        consolidated_file = self._get_consolidated_file(profile.user_id)
        try:
            consolidated_file.write_text(json.dumps(profile_dict, indent=2))
        except Exception as e:
            logger.error("Failed to save consolidated profile for %s: %s", profile.user_id, e)

        logger.info("Saved profile for user %s", profile.user_id)

    def update_category(
        self,
        user_id: str,
        category: str,
        data: Dict[str, Any],
        merge: bool = True,
    ) -> UserProfile:
        """
        Update a specific category of a user's profile.

        Args:
            user_id: The user's ID
            category: The category to update (e.g., "preferences", "goals")
            data: The data to update/merge
            merge: If True, merge with existing data. If False, replace.

        Returns:
            Updated UserProfile
        """
        if category not in self.PROFILE_CATEGORIES:
            raise UserProfileStoreError(f"Invalid category: {category}")

        profile = self.load_profile(user_id)
        if profile is None:
            profile = self.create_profile(user_id)

        profile_dict = profile.model_dump()

        if merge and category in profile_dict:
            existing = profile_dict[category]
            if isinstance(existing, dict) and isinstance(data, dict):
                self._deep_merge(existing, data)
                profile_dict[category] = existing
            else:
                profile_dict[category] = data
        else:
            profile_dict[category] = data

        profile_dict["updated_at"] = datetime.utcnow().isoformat()
        updated_profile = UserProfile(**profile_dict)
        self.save_profile(updated_profile)

        return updated_profile

    def _deep_merge(self, base: Dict, updates: Dict) -> None:
        """Deep merge updates into base dict."""
        for key, value in updates.items():
            if key in base and isinstance(base[key], dict) and isinstance(value, dict):
                self._deep_merge(base[key], value)
            elif key in base and isinstance(base[key], list) and isinstance(value, list):
                existing_set = (
                    set(base[key]) if all(isinstance(x, str) for x in base[key]) else None
                )
                if existing_set:
                    for item in value:
                        if item not in existing_set:
                            base[key].append(item)
                else:
                    base[key].extend(value)
            else:
                base[key] = value

    def add_to_list(
        self,
        user_id: str,
        category: str,
        field: str,
        items: List[str],
    ) -> UserProfile:
        """
        Add items to a list field in a profile category.

        Args:
            user_id: The user's ID
            category: The category (e.g., "preferences")
            field: The list field (e.g., "food_likes")
            items: Items to add

        Returns:
            Updated UserProfile
        """
        profile = self.load_profile(user_id)
        if profile is None:
            profile = self.create_profile(user_id)

        profile_dict = profile.model_dump()

        if category not in profile_dict:
            profile_dict[category] = {}

        category_data = profile_dict[category]

        if field not in category_data:
            category_data[field] = []

        existing = set(category_data[field])
        for item in items:
            if item not in existing:
                category_data[field].append(item)

        profile_dict["updated_at"] = datetime.utcnow().isoformat()
        updated_profile = UserProfile(**profile_dict)
        self.save_profile(updated_profile)

        return updated_profile

    def remove_from_list(
        self,
        user_id: str,
        category: str,
        field: str,
        items: List[str],
    ) -> UserProfile:
        """
        Remove items from a list field in a profile category.

        Args:
            user_id: The user's ID
            category: The category (e.g., "preferences")
            field: The list field (e.g., "food_dislikes")
            items: Items to remove

        Returns:
            Updated UserProfile
        """
        profile = self.load_profile(user_id)
        if profile is None:
            return self.create_profile(user_id)

        profile_dict = profile.model_dump()

        if category in profile_dict and field in profile_dict[category]:
            items_set = set(items)
            profile_dict[category][field] = [
                x for x in profile_dict[category][field] if x not in items_set
            ]

        profile_dict["updated_at"] = datetime.utcnow().isoformat()
        updated_profile = UserProfile(**profile_dict)
        self.save_profile(updated_profile)

        return updated_profile

    def delete_profile(self, user_id: str) -> bool:
        """
        Delete a user's profile.

        Args:
            user_id: The user's ID

        Returns:
            True if deleted, False if not found
        """
        user_dir = self._get_user_dir(user_id)

        if not user_dir.exists():
            return False

        import shutil

        shutil.rmtree(user_dir)
        logger.info("Deleted profile for user %s", user_id)
        return True

    def list_users(self) -> List[str]:
        """List all user IDs with stored profiles."""
        if not self.storage_dir.exists():
            return []

        return [
            d.name
            for d in self.storage_dir.iterdir()
            if d.is_dir() and (d / "profile.json").exists()
        ]

    def profile_exists(self, user_id: str) -> bool:
        """Check if a profile exists for a user."""
        return self._get_consolidated_file(user_id).exists()

    def get_profile_summary(self, user_id: str) -> Optional[str]:
        """
        Get a text summary of a user's profile for use in prompts.

        Args:
            user_id: The user's ID

        Returns:
            A formatted string summary or None
        """
        profile = self.load_profile(user_id)
        if profile is None:
            return None

        lines = []

        if profile.identity.preferred_name:
            lines.append(f"Name: {profile.identity.preferred_name}")
        if profile.identity.timezone:
            lines.append(f"Timezone: {profile.identity.timezone}")

        if profile.preferences.food_likes:
            lines.append(f"Food likes: {', '.join(profile.preferences.food_likes[:5])}")
        if profile.preferences.food_dislikes:
            lines.append(f"Food dislikes: {', '.join(profile.preferences.food_dislikes[:5])}")
        if profile.preferences.dietary_restrictions:
            lines.append(
                f"Dietary restrictions: {', '.join(profile.preferences.dietary_restrictions)}"
            )

        if profile.professional.job_title:
            lines.append(f"Job: {profile.professional.job_title}")

        if profile.goals.short_term_goals:
            goals = [g.title for g in profile.goals.short_term_goals[:3]]
            lines.append(f"Current goals: {', '.join(goals)}")

        return "\n".join(lines) if lines else "No profile information available."
