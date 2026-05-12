"""Unit tests for ClientProfileStore and MealFeedbackStore (Postgres-backed)."""

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
from shared_postgres import dict_row, get_conn, is_postgres_enabled

pytestmark = pytest.mark.skipif(
    not is_postgres_enabled(),
    reason="Nutrition stores require Postgres (set POSTGRES_HOST).",
)


class TestClientProfileStore:
    """Tests for ClientProfileStore."""

    @pytest.fixture
    def store(self):
        return ClientProfileStore()

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

    def test_module_level_create_get_save(self):
        p = create_profile("c3")
        assert p.client_id == "c3"
        save_profile("c3", ClientProfile(client_id="c3", dietary_needs=["vegan"]))
        loaded = get_profile("c3")
        assert loaded is not None
        assert "vegan" in loaded.dietary_needs


class TestMealFeedbackStore:
    """Tests for MealFeedbackStore."""

    @pytest.fixture
    def store(self):
        return MealFeedbackStore()

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

    def test_module_level_functions(self):
        rec_id = record_recommendation("c2", {"name": "Omelette"})
        record_feedback(rec_id, rating=4, would_make_again=True)
        entries = get_meal_history("c2")
        assert len(entries) == 1
        assert entries[0].feedback and entries[0].feedback.rating == 4

    def test_record_recommendation_without_guardrail_kwargs(self, store):
        # Legacy / pre-SPEC-007-W5 insert path: all four new columns
        # must persist as NULL (acceptance: "accepts guardrail_version=None").
        rec_id = store.record_recommendation("c-legacy", {"name": "Toast"})
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT guardrail_version, parsed_ingredients_json, "
                "       flags_json, restriction_snapshot_hash "
                "FROM nutrition_recommendations WHERE recommendation_id = %s",
                (rec_id,),
            )
            row = cur.fetchone()
        assert row is not None
        assert row["guardrail_version"] is None
        assert row["parsed_ingredients_json"] is None
        assert row["flags_json"] is None
        assert row["restriction_snapshot_hash"] is None

    def test_record_recommendation_with_guardrail_metadata(self, store):
        parsed = [{"raw": "almonds", "canonical_id": "tree_nut"}]
        flags = [{"tag": "allergen", "detail": "tree_nut"}]
        rec_id = store.record_recommendation(
            "c-guarded",
            {"name": "Almond bowl"},
            guardrail_version="1.0.0",
            parsed_ingredients=parsed,
            flags=flags,
            restriction_snapshot_hash="abc123",
        )
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT guardrail_version, parsed_ingredients_json, "
                "       flags_json, restriction_snapshot_hash "
                "FROM nutrition_recommendations WHERE recommendation_id = %s",
                (rec_id,),
            )
            row = cur.fetchone()
        assert row is not None
        assert row["guardrail_version"] == "1.0.0"
        assert row["parsed_ingredients_json"] == parsed
        assert row["flags_json"] == flags
        assert row["restriction_snapshot_hash"] == "abc123"
