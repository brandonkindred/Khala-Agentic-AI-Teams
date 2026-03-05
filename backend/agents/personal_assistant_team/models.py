"""Shared Pydantic models for the Personal Assistant team."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, HttpUrl


class Priority(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    URGENT = "urgent"


class TaskStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class EmailProvider(str, Enum):
    GMAIL = "gmail"
    OUTLOOK = "outlook"
    IMAP = "imap"


class ReservationType(str, Enum):
    RESTAURANT = "restaurant"
    APPOINTMENT = "appointment"
    SERVICE = "service"
    TRAVEL = "travel"


class CommunicationPreference(str, Enum):
    EMAIL = "email"
    SMS = "sms"
    PUSH = "push"
    NONE = "none"


class ClothingPreferences(BaseModel):
    """User's clothing preferences."""

    sock_styles: List[str] = Field(default_factory=list)
    sock_materials: List[str] = Field(default_factory=list)
    shirt_sizes: List[str] = Field(default_factory=list)
    pant_sizes: List[str] = Field(default_factory=list)
    shoe_sizes: List[str] = Field(default_factory=list)
    preferred_styles: List[str] = Field(default_factory=list)
    colors_preferred: List[str] = Field(default_factory=list)
    colors_avoided: List[str] = Field(default_factory=list)


class IdentityProfile(BaseModel):
    """Core identity information."""

    full_name: str = ""
    preferred_name: str = ""
    email: str = ""
    phone: str = ""
    timezone: str = "UTC"
    languages: List[str] = Field(default_factory=lambda: ["en"])
    communication_preference: CommunicationPreference = CommunicationPreference.EMAIL
    birthday: Optional[str] = None
    address: Optional[str] = None


class PreferencesProfile(BaseModel):
    """User preferences for food, clothing, flowers, etc."""

    food_likes: List[str] = Field(default_factory=list)
    food_dislikes: List[str] = Field(default_factory=list)
    cuisines_ranked: List[str] = Field(default_factory=list)
    dietary_restrictions: List[str] = Field(default_factory=list)
    favorite_flowers: List[str] = Field(default_factory=list)
    favorite_colors: List[str] = Field(default_factory=list)
    clothing_preferences: ClothingPreferences = Field(default_factory=ClothingPreferences)
    brands_liked: List[str] = Field(default_factory=list)
    brands_disliked: List[str] = Field(default_factory=list)
    music_genres: List[str] = Field(default_factory=list)
    hobbies: List[str] = Field(default_factory=list)


class Goal(BaseModel):
    """A single goal with metadata."""

    goal_id: str
    title: str
    description: str = ""
    target_date: Optional[str] = None
    priority: Priority = Priority.MEDIUM
    category: str = "general"
    progress_notes: List[str] = Field(default_factory=list)
    completed: bool = False


class GoalsProfile(BaseModel):
    """User goals and aspirations."""

    short_term_goals: List[Goal] = Field(default_factory=list)
    long_term_goals: List[Goal] = Field(default_factory=list)
    dreams: List[str] = Field(default_factory=list)
    bucket_list: List[str] = Field(default_factory=list)
    values: List[str] = Field(default_factory=list)


class LifestyleProfile(BaseModel):
    """User lifestyle information."""

    hobbies: List[str] = Field(default_factory=list)
    exercise_habits: List[str] = Field(default_factory=list)
    sleep_schedule: str = ""
    morning_routine: str = ""
    evening_routine: str = ""
    work_life_balance_notes: str = ""


class ProfessionalProfile(BaseModel):
    """Professional and work information."""

    job_title: str = ""
    company: str = ""
    industry: str = ""
    work_schedule: str = ""
    work_timezone: str = ""
    professional_goals: List[str] = Field(default_factory=list)
    skills: List[str] = Field(default_factory=list)
    networking_preferences: str = ""
    linkedin_url: Optional[str] = None


class ImportantPerson(BaseModel):
    """Information about an important person in the user's life."""

    name: str
    relationship: str
    birthday: Optional[str] = None
    anniversary: Optional[str] = None
    contact_info: str = ""
    notes: str = ""
    gift_ideas: List[str] = Field(default_factory=list)
    preferences: Dict[str, Any] = Field(default_factory=dict)


class RelationshipsProfile(BaseModel):
    """User's important relationships."""

    important_people: List[ImportantPerson] = Field(default_factory=list)
    family_notes: str = ""
    friend_notes: str = ""
    colleague_notes: str = ""


class SpendingCategory(BaseModel):
    """Budget category with limits."""

    category: str
    monthly_budget: Optional[float] = None
    notes: str = ""


class FinancialProfile(BaseModel):
    """Financial preferences and budget information."""

    currency: str = "USD"
    budget_style: str = ""
    spending_categories: List[SpendingCategory] = Field(default_factory=list)
    deal_alert_threshold_pct: float = 20.0
    preferred_payment_methods: List[str] = Field(default_factory=list)
    financial_goals: List[str] = Field(default_factory=list)


class HealthProfile(BaseModel):
    """Health-related information."""

    allergies: List[str] = Field(default_factory=list)
    medications: List[str] = Field(default_factory=list)
    medical_conditions: List[str] = Field(default_factory=list)
    fitness_goals: List[str] = Field(default_factory=list)
    preferred_doctors: List[str] = Field(default_factory=list)
    health_notes: str = ""


class TravelProfile(BaseModel):
    """Travel preferences and history."""

    favorite_destinations: List[str] = Field(default_factory=list)
    bucket_list_destinations: List[str] = Field(default_factory=list)
    travel_style: str = ""
    preferred_airlines: List[str] = Field(default_factory=list)
    preferred_hotels: List[str] = Field(default_factory=list)
    frequent_flyer_programs: List[str] = Field(default_factory=list)
    passport_country: str = ""
    visa_notes: str = ""


class ShoppingProfile(BaseModel):
    """Shopping preferences."""

    favorite_stores: List[str] = Field(default_factory=list)
    online_stores: List[str] = Field(default_factory=list)
    wishlist_items: List[str] = Field(default_factory=list)
    sizes: Dict[str, str] = Field(default_factory=dict)
    style_preferences: List[str] = Field(default_factory=list)


class UserProfile(BaseModel):
    """Complete user profile containing all dimensions."""

    user_id: str
    schema_version: str = "1.0"
    created_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())
    identity: IdentityProfile = Field(default_factory=IdentityProfile)
    preferences: PreferencesProfile = Field(default_factory=PreferencesProfile)
    goals: GoalsProfile = Field(default_factory=GoalsProfile)
    lifestyle: LifestyleProfile = Field(default_factory=LifestyleProfile)
    professional: ProfessionalProfile = Field(default_factory=ProfessionalProfile)
    relationships: RelationshipsProfile = Field(default_factory=RelationshipsProfile)
    financial: FinancialProfile = Field(default_factory=FinancialProfile)
    health: HealthProfile = Field(default_factory=HealthProfile)
    travel: TravelProfile = Field(default_factory=TravelProfile)
    shopping: ShoppingProfile = Field(default_factory=ShoppingProfile)


class TaskItem(BaseModel):
    """A single task or list item."""

    item_id: str
    description: str
    quantity: Optional[str] = None
    priority: Priority = Priority.MEDIUM
    due_date: Optional[datetime] = None
    status: TaskStatus = TaskStatus.PENDING
    notes: Optional[str] = None
    tags: List[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())
    completed_at: Optional[str] = None


class TaskList(BaseModel):
    """A list of tasks (e.g., grocery list, todo list)."""

    list_id: str
    user_id: str
    name: str
    description: str = ""
    items: List[TaskItem] = Field(default_factory=list)
    shared_with: List[str] = Field(default_factory=list)
    is_recurring: bool = False
    recurrence_pattern: Optional[str] = None
    created_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())


class EmailMessage(BaseModel):
    """Represents an email message."""

    message_id: str
    subject: str
    sender: str
    recipients: List[str]
    cc: List[str] = Field(default_factory=list)
    bcc: List[str] = Field(default_factory=list)
    body: str
    html_body: Optional[str] = None
    timestamp: str
    is_read: bool = False
    labels: List[str] = Field(default_factory=list)
    attachments: List[str] = Field(default_factory=list)


class EmailDraft(BaseModel):
    """Email draft to be sent."""

    to: List[str]
    cc: List[str] = Field(default_factory=list)
    bcc: List[str] = Field(default_factory=list)
    subject: str
    body: str
    html_body: Optional[str] = None


class CalendarEvent(BaseModel):
    """A calendar event."""

    event_id: str
    title: str
    description: str = ""
    start_time: datetime
    end_time: datetime
    location: Optional[str] = None
    attendees: List[str] = Field(default_factory=list)
    reminders: List[int] = Field(default_factory=list)
    is_all_day: bool = False
    recurrence_rule: Optional[str] = None
    source: str = ""
    created_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())


class Deal(BaseModel):
    """A deal or discount opportunity."""

    deal_id: str
    title: str
    description: str
    original_price: Optional[float] = None
    sale_price: Optional[float] = None
    discount_percent: Optional[float] = None
    url: Optional[HttpUrl] = None
    store: str = ""
    category: str = ""
    expires_at: Optional[str] = None
    relevance_score: float = 0.0
    matching_preferences: List[str] = Field(default_factory=list)
    found_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())


class Reservation(BaseModel):
    """A reservation (restaurant, appointment, etc.)."""

    reservation_id: str
    reservation_type: ReservationType
    venue_name: str
    venue_address: Optional[str] = None
    venue_phone: Optional[str] = None
    datetime: datetime
    party_size: int = 1
    confirmation_number: Optional[str] = None
    notes: str = ""
    status: str = "pending"
    created_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())


class AssistantRequest(BaseModel):
    """A free-form request to the personal assistant."""

    request_id: str
    user_id: str
    message: str
    context: Dict[str, Any] = Field(default_factory=dict)
    timestamp: str = Field(default_factory=lambda: datetime.utcnow().isoformat())


class AssistantResponse(BaseModel):
    """Response from the personal assistant."""

    request_id: str
    message: str
    actions_taken: List[str] = Field(default_factory=list)
    data: Dict[str, Any] = Field(default_factory=dict)
    follow_up_suggestions: List[str] = Field(default_factory=list)
    timestamp: str = Field(default_factory=lambda: datetime.utcnow().isoformat())


class ProfileUpdateSignal(BaseModel):
    """A signal indicating a potential profile update based on user interaction."""

    signal_type: str
    source: str
    raw_text: str
    extracted_info: Dict[str, Any]
    confidence: float
    timestamp: str = Field(default_factory=lambda: datetime.utcnow().isoformat())
