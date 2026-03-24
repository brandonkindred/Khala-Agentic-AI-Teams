"""Tests for shared models."""

from datetime import datetime

from ..models import (
    CalendarEvent,
    ClothingPreferences,
    Deal,
    Goal,
    GoalsProfile,
    IdentityProfile,
    PreferencesProfile,
    Priority,
    Reservation,
    ReservationType,
    TaskItem,
    TaskList,
    TaskStatus,
    UserProfile,
)


class TestUserProfile:
    """Tests for UserProfile model."""

    def test_create_empty_profile(self):
        """Test creating an empty profile."""
        profile = UserProfile(user_id="test_user")

        assert profile.user_id == "test_user"
        assert profile.schema_version == "1.0"
        assert profile.identity.full_name == ""
        assert profile.preferences.food_likes == []

    def test_profile_with_identity(self):
        """Test profile with identity information."""
        profile = UserProfile(
            user_id="test_user",
            identity=IdentityProfile(
                full_name="John Doe",
                preferred_name="John",
                email="john@example.com",
                timezone="America/New_York",
            ),
        )

        assert profile.identity.full_name == "John Doe"
        assert profile.identity.preferred_name == "John"
        assert profile.identity.timezone == "America/New_York"

    def test_profile_with_preferences(self):
        """Test profile with preferences."""
        profile = UserProfile(
            user_id="test_user",
            preferences=PreferencesProfile(
                food_likes=["pizza", "sushi"],
                food_dislikes=["olives"],
                cuisines_ranked=["Italian", "Japanese", "Mexican"],
                dietary_restrictions=["vegetarian"],
                favorite_flowers=["roses", "tulips"],
                favorite_colors=["blue", "green"],
            ),
        )

        assert "pizza" in profile.preferences.food_likes
        assert "olives" in profile.preferences.food_dislikes
        assert profile.preferences.cuisines_ranked[0] == "Italian"

    def test_profile_with_goals(self):
        """Test profile with goals."""
        goal = Goal(
            goal_id="g1",
            title="Learn Spanish",
            description="Become conversational in Spanish",
            category="education",
        )

        profile = UserProfile(
            user_id="test_user",
            goals=GoalsProfile(
                short_term_goals=[goal],
                dreams=["Travel the world"],
                bucket_list=["Visit Japan", "Learn to surf"],
            ),
        )

        assert len(profile.goals.short_term_goals) == 1
        assert profile.goals.short_term_goals[0].title == "Learn Spanish"
        assert "Travel the world" in profile.goals.dreams

    def test_clothing_preferences(self):
        """Test clothing preferences."""
        clothing = ClothingPreferences(
            sock_styles=["athletic", "dress"],
            sock_materials=["cotton", "wool"],
            shirt_sizes=["M", "L"],
            preferred_styles=["casual", "business casual"],
            colors_preferred=["navy", "gray"],
        )

        profile = UserProfile(
            user_id="test_user",
            preferences=PreferencesProfile(
                clothing_preferences=clothing,
            ),
        )

        assert "athletic" in profile.preferences.clothing_preferences.sock_styles
        assert "M" in profile.preferences.clothing_preferences.shirt_sizes


class TestTaskModels:
    """Tests for task-related models."""

    def test_create_task_item(self):
        """Test creating a task item."""
        item = TaskItem(
            item_id="t1",
            description="Buy milk",
            quantity="2 gallons",
            priority=Priority.HIGH,
        )

        assert item.item_id == "t1"
        assert item.description == "Buy milk"
        assert item.quantity == "2 gallons"
        assert item.priority == Priority.HIGH
        assert item.status == TaskStatus.PENDING

    def test_create_task_list(self):
        """Test creating a task list."""
        items = [
            TaskItem(item_id="t1", description="Milk"),
            TaskItem(item_id="t2", description="Bread"),
            TaskItem(item_id="t3", description="Eggs"),
        ]

        task_list = TaskList(
            list_id="l1",
            user_id="test_user",
            name="Groceries",
            items=items,
        )

        assert task_list.name == "Groceries"
        assert len(task_list.items) == 3
        assert task_list.items[0].description == "Milk"

    def test_task_completion(self):
        """Test completing a task."""
        item = TaskItem(
            item_id="t1",
            description="Task",
            status=TaskStatus.COMPLETED,
            completed_at=datetime.utcnow().isoformat(),
        )

        assert item.status == TaskStatus.COMPLETED
        assert item.completed_at is not None


class TestCalendarEvent:
    """Tests for calendar event model."""

    def test_create_event(self):
        """Test creating a calendar event."""
        event = CalendarEvent(
            event_id="e1",
            title="Team Meeting",
            start_time=datetime(2026, 2, 26, 14, 0),
            end_time=datetime(2026, 2, 26, 15, 0),
            location="Conference Room A",
            attendees=["alice@example.com", "bob@example.com"],
        )

        assert event.title == "Team Meeting"
        assert event.location == "Conference Room A"
        assert len(event.attendees) == 2

    def test_all_day_event(self):
        """Test all-day event."""
        event = CalendarEvent(
            event_id="e2",
            title="Company Holiday",
            start_time=datetime(2026, 12, 25, 0, 0),
            end_time=datetime(2026, 12, 25, 23, 59),
            is_all_day=True,
        )

        assert event.is_all_day is True


class TestDealAndReservation:
    """Tests for deal and reservation models."""

    def test_create_deal(self):
        """Test creating a deal."""
        deal = Deal(
            deal_id="d1",
            title="50% off Running Shoes",
            description="Nike Air Max on sale",
            original_price=150.00,
            sale_price=75.00,
            discount_percent=50.0,
            store="Nike",
            category="shoes",
            relevance_score=0.85,
            matching_preferences=["running", "Nike"],
        )

        assert deal.discount_percent == 50.0
        assert deal.relevance_score == 0.85
        assert "Nike" in deal.matching_preferences

    def test_create_reservation(self):
        """Test creating a reservation."""
        reservation = Reservation(
            reservation_id="r1",
            reservation_type=ReservationType.RESTAURANT,
            venue_name="Italian Bistro",
            datetime=datetime(2026, 2, 28, 19, 0),
            party_size=4,
            notes="Anniversary dinner",
            status="confirmed",
        )

        assert reservation.venue_name == "Italian Bistro"
        assert reservation.party_size == 4
        assert reservation.status == "confirmed"
