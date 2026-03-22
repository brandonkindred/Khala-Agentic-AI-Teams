"""Models for the User Profile Agent."""

from __future__ import annotations

from typing import Any, Dict, List

from pydantic import BaseModel, Field


class ProfileUpdateRequest(BaseModel):
    """Request to update a user's profile."""

    user_id: str
    category: str
    data: Dict[str, Any]
    merge: bool = True


class ProfileQueryRequest(BaseModel):
    """Request to query profile information."""

    user_id: str
    query: str
    categories: List[str] = Field(default_factory=list)


class ExtractedPreference(BaseModel):
    """A preference extracted from text."""

    category: str
    field: str
    value: Any
    confidence: float
    source_text: str = ""


class ProfileExtractionResult(BaseModel):
    """Result of profile extraction from text."""

    extracted_info: List[ExtractedPreference]
    reasoning: str = ""


class ProfileUpdateResult(BaseModel):
    """Result of a profile update operation."""

    success: bool
    updated_fields: List[str] = Field(default_factory=list)
    message: str = ""


class LearnFromTextRequest(BaseModel):
    """Request to learn about user from text."""

    user_id: str
    text: str
    source: str = "conversation"
    auto_apply: bool = False


class LearnFromTextResult(BaseModel):
    """Result of learning from text."""

    extracted: List[ExtractedPreference]
    applied: List[ExtractedPreference] = Field(default_factory=list)
    pending_confirmation: List[ExtractedPreference] = Field(default_factory=list)
