"""Tests for UserProfileStore."""

import shutil
import tempfile

import pytest

from ..models import UserProfile
from ..shared.user_profile_store import UserProfileStore


class TestUserProfileStore:
    """Tests for UserProfileStore."""

    @pytest.fixture
    def temp_dir(self):
        """Create a temporary directory for testing."""
        temp = tempfile.mkdtemp()
        yield temp
        shutil.rmtree(temp)

    @pytest.fixture
    def store(self, temp_dir):
        """Create a UserProfileStore with temp directory."""
        return UserProfileStore(storage_dir=temp_dir)

    def test_create_profile(self, store):
        """Test creating a new profile."""
        profile = store.create_profile("test_user")

        assert profile.user_id == "test_user"
        assert store.profile_exists("test_user")

    def test_load_profile(self, store):
        """Test loading a profile."""
        store.create_profile("test_user")

        profile = store.load_profile("test_user")

        assert profile is not None
        assert profile.user_id == "test_user"

    def test_load_nonexistent_profile(self, store):
        """Test loading a profile that doesn't exist."""
        profile = store.load_profile("nonexistent")

        assert profile is None

    def test_save_and_load_profile(self, store):
        """Test saving and loading a profile with data."""
        profile = UserProfile(
            user_id="test_user",
        )
        profile.identity.full_name = "John Doe"
        profile.preferences.food_likes = ["pizza", "sushi"]

        store.save_profile(profile)

        loaded = store.load_profile("test_user")

        assert loaded is not None
        assert loaded.identity.full_name == "John Doe"
        assert "pizza" in loaded.preferences.food_likes

    def test_update_category(self, store):
        """Test updating a profile category."""
        store.create_profile("test_user")

        updated = store.update_category(
            user_id="test_user",
            category="preferences",
            data={"food_likes": ["tacos", "burritos"]},
            merge=False,
        )

        assert "tacos" in updated.preferences.food_likes
        assert "burritos" in updated.preferences.food_likes

    def test_update_category_merge(self, store):
        """Test merging data into a profile category."""
        store.create_profile("test_user")
        store.update_category(
            user_id="test_user",
            category="preferences",
            data={"food_likes": ["pizza"]},
        )

        updated = store.update_category(
            user_id="test_user",
            category="preferences",
            data={"food_likes": ["sushi"], "food_dislikes": ["olives"]},
            merge=True,
        )

        assert "pizza" in updated.preferences.food_likes
        assert "sushi" in updated.preferences.food_likes
        assert "olives" in updated.preferences.food_dislikes

    def test_add_to_list(self, store):
        """Test adding items to a list field."""
        store.create_profile("test_user")

        updated = store.add_to_list(
            user_id="test_user",
            category="preferences",
            field="food_likes",
            items=["pizza", "pasta"],
        )

        assert "pizza" in updated.preferences.food_likes
        assert "pasta" in updated.preferences.food_likes

    def test_add_to_list_no_duplicates(self, store):
        """Test that adding duplicate items doesn't create duplicates."""
        store.create_profile("test_user")

        store.add_to_list("test_user", "preferences", "food_likes", ["pizza"])
        updated = store.add_to_list("test_user", "preferences", "food_likes", ["pizza", "sushi"])

        pizza_count = updated.preferences.food_likes.count("pizza")
        assert pizza_count == 1

    def test_remove_from_list(self, store):
        """Test removing items from a list field."""
        store.create_profile("test_user")
        store.add_to_list("test_user", "preferences", "food_likes", ["pizza", "sushi", "tacos"])

        updated = store.remove_from_list(
            user_id="test_user",
            category="preferences",
            field="food_likes",
            items=["pizza"],
        )

        assert "pizza" not in updated.preferences.food_likes
        assert "sushi" in updated.preferences.food_likes

    def test_delete_profile(self, store):
        """Test deleting a profile."""
        store.create_profile("test_user")

        result = store.delete_profile("test_user")

        assert result is True
        assert not store.profile_exists("test_user")

    def test_delete_nonexistent_profile(self, store):
        """Test deleting a profile that doesn't exist."""
        result = store.delete_profile("nonexistent")

        assert result is False

    def test_list_users(self, store):
        """Test listing all users."""
        store.create_profile("user1")
        store.create_profile("user2")
        store.create_profile("user3")

        users = store.list_users()

        assert len(users) == 3
        assert "user1" in users
        assert "user2" in users
        assert "user3" in users

    def test_get_profile_summary(self, store):
        """Test getting a profile summary."""
        store.create_profile("test_user")
        store.update_category(
            user_id="test_user",
            category="identity",
            data={"preferred_name": "John", "timezone": "America/New_York"},
        )
        store.add_to_list("test_user", "preferences", "food_likes", ["pizza", "sushi"])

        summary = store.get_profile_summary("test_user")

        assert summary is not None
        assert "John" in summary
        assert "pizza" in summary or "sushi" in summary
