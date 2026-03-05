"""Models for the Email Agent."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class EmailReadRequest(BaseModel):
    """Request to read emails."""

    user_id: str
    limit: int = 20
    unread_only: bool = False
    folder: str = "INBOX"


class EmailDraftRequest(BaseModel):
    """Request to draft an email."""

    user_id: str
    intent: str
    context: Dict[str, Any] = Field(default_factory=dict)
    reply_to_message_id: Optional[str] = None


class EmailSendRequest(BaseModel):
    """Request to send an email."""

    user_id: str
    to: List[str]
    subject: str
    body: str
    cc: List[str] = Field(default_factory=list)
    bcc: List[str] = Field(default_factory=list)


class EmailSummary(BaseModel):
    """Summary of an email."""

    message_id: str
    subject: str
    sender: str
    summary: str
    key_points: List[str] = Field(default_factory=list)
    extracted_events: List[Dict[str, Any]] = Field(default_factory=list)
    action_items: List[str] = Field(default_factory=list)
    sentiment: str = "neutral"


class EmailSearchRequest(BaseModel):
    """Request to search emails."""

    user_id: str
    query: str
    limit: int = 20


class ConnectEmailRequest(BaseModel):
    """Request to connect an email account."""

    user_id: str
    provider: str
    credentials: Dict[str, Any]


class DraftResult(BaseModel):
    """Result of drafting an email."""

    subject: str
    body: str
    suggested_recipients: List[str] = Field(default_factory=list)
    tone: str = "professional"
