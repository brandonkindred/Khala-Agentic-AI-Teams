"""Unit tests for ClientProfileStore and MealFeedbackStore."""

import pytest

from nutrition_meal_planning_team.models import (
    ClientProfile,
    HouseholdInfo,
)
from nutrition_meal_planning_team.shared.client_profile_store import (
    ClientProfileStore,
    create_profile,
    get_profile,
    save_profile,
)
from nutrition_meal_planning_team.shared.meal_feedback_store import (
    MealFeedbackStore,
    get_meal_history,
    record_feedback,
    record_recommendation,
)


class TestClientProfileStore:
    """Tests for ClientProfileStore."""

    @pytest.fixture
    def storage_dir(self, tmp_path):
        return tmp_path / "profiles"

    @pytest.fixture
    def store(self, storage_dir):
        return ClientProfileStore(storage_dir=storage_dir)

    def test_create_profile(self, store):
        profile = store.create_profile("client1")
        assert profile.client_id == "client1"
        loaded = store.get_profile("client1")
        assert loaded is not None
        assert loaded.client_id == "client1"

    def test_get_profile_missing(self, store):
        assert store.get_profile("nonexistent") is None

    def test_save_and_get_profile(self, store):
        profile = ClientProfile(
            client_id="c2",
            household=HouseholdInfo(number_of_people=2, description="couple"),
            dietary_needs=["vegetarian"],
        )
        store.save_profile("c2", profile)
        loaded = store.get_profile("c2")
        assert loaded is not None
        assert loaded.household.number_of_people == 2
        assert "vegetarian" in loaded.dietary_needs

    def test_module_level_create_get_save(self, storage_dir):
        p = create_profile("c3", storage_dir=storage_dir)
        assert p.client_id == "c3"
        save_profile(
            "c3", ClientProfile(client_id="c3", dietary_needs=["vegan"]), storage_dir=storage_dir
        )
        loaded = get_profile("c3", storage_dir=storage_dir)
        assert loaded is not None
        assert "vegan" in loaded.dietary_needs


class TestMealFeedbackStore:
    """Tests for MealFeedbackStore."""

    @pytest.fixture
    def storage_dir(self, tmp_path):
        return tmp_path / "recommendations"

    @pytest.fixture
    def store(self, storage_dir):
        return MealFeedbackStore(storage_dir=storage_dir)

    def test_record_recommendation_returns_id(self, store):
        rec_id = store.record_recommendation(
            "client1", {"name": "Salad", "ingredients": ["lettuce"]}
        )
        assert rec_id
        assert len(rec_id) == 36  # uuid4 hex + hyphens

    def test_get_meal_history_empty(self, store):
        entries = store.get_meal_history("client1")
        assert entries == []

    def test_record_and_get_history(self, store):
        rec_id = store.record_recommendation("client1", {"name": "Soup", "meal_type": "lunch"})
        entries = store.get_meal_history("client1")
        assert len(entries) == 1
        assert entries[0].recommendation_id == rec_id
        assert entries[0].meal_snapshot.get("name") == "Soup"

    def test_record_feedback(self, store):
        rec_id = store.record_recommendation("client1", {"name": "Pasta"})
        ok = store.record_feedback(rec_id, rating=5, would_make_again=True, notes="Great")
        assert ok is True
        entries = store.get_meal_history("client1")
        assert len(entries) == 1
        assert entries[0].feedback is not None
        assert entries[0].feedback.rating == 5
        assert entries[0].feedback.would_make_again is True

    def test_record_feedback_nonexistent(self, store):
        ok = store.record_feedback("nonexistent-uuid", rating=1)
        assert ok is False

    def test_module_level_functions(self, storage_dir):
        rec_id = record_recommendation("c2", {"name": "Omelette"}, storage_dir=storage_dir)
        record_feedback(rec_id, rating=4, would_make_again=True, storage_dir=storage_dir)
        entries = get_meal_history("c2", storage_dir=storage_dir)
        assert len(entries) == 1
        assert entries[0].feedback and entries[0].feedback.rating == 4
