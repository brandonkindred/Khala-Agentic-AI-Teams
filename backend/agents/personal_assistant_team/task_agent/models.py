"""Models for the Task Agent."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from ..models import Priority, TaskStatus


class CreateListRequest(BaseModel):
    """Request to create a task list."""

    user_id: str
    name: str
    description: str = ""
    is_recurring: bool = False
    recurrence_pattern: Optional[str] = None


class AddItemRequest(BaseModel):
    """Request to add an item to a list."""

    user_id: str
    list_id: str
    description: str
    quantity: Optional[str] = None
    priority: Priority = Priority.MEDIUM
    due_date: Optional[datetime] = None
    notes: Optional[str] = None
    tags: List[str] = Field(default_factory=list)


class AddItemsFromTextRequest(BaseModel):
    """Request to add items from natural language."""

    user_id: str
    list_id: Optional[str] = None
    text: str


class UpdateItemRequest(BaseModel):
    """Request to update a task item."""

    user_id: str
    list_id: str
    item_id: str
    updates: Dict[str, Any]


class CompleteItemRequest(BaseModel):
    """Request to complete a task item."""

    user_id: str
    list_id: str
    item_id: str


class ListTasksRequest(BaseModel):
    """Request to list tasks."""

    user_id: str
    list_id: Optional[str] = None
    status_filter: Optional[TaskStatus] = None
    include_completed: bool = False


class ParsedTaskItem(BaseModel):
    """A task item parsed from natural language."""

    description: str
    quantity: Optional[str] = None
    priority: Priority = Priority.MEDIUM
    due_date: Optional[str] = None
    tags: List[str] = Field(default_factory=list)
    confidence: float = 1.0


class ParseTasksResult(BaseModel):
    """Result of parsing tasks from text."""

    list_name: str = "default"
    items: List[ParsedTaskItem]
    needs_confirmation: bool = False
