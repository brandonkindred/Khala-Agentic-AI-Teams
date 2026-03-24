"""Task Agent - manages task lists, grocery lists, and todos."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4

from ..models import Priority, TaskItem, TaskList, TaskStatus
from ..shared.llm import JSONExtractionFailure, LLMClient
from ..shared.user_profile_store import UserProfileStore
from .models import (
    AddItemRequest,
    AddItemsFromTextRequest,
    CompleteItemRequest,
    CreateListRequest,
    ListTasksRequest,
    ParsedTaskItem,
    ParseTasksResult,
    UpdateItemRequest,
)
from .prompts import CATEGORIZE_ITEMS_PROMPT, PARSE_TASKS_PROMPT, SUGGEST_ITEMS_PROMPT

logger = logging.getLogger(__name__)


class TaskAgent:
    """
    Agent for managing task and grocery lists.

    Capabilities:
    - Create and manage multiple lists
    - Add items via natural language
    - Track completion status
    - Categorize grocery items
    - Suggest commonly needed items
    """

    def __init__(
        self,
        llm: LLMClient,
        storage_dir: Optional[str] = None,
        profile_store: Optional[UserProfileStore] = None,
    ) -> None:
        """
        Initialize the Task Agent.

        Args:
            llm: LLM client for parsing
            storage_dir: Directory for task storage
            profile_store: User profile storage
        """
        self.llm = llm
        self.storage_dir = Path(storage_dir or os.getenv("PA_TASKS_DIR", ".agent_cache/tasks"))
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.profile_store = profile_store or UserProfileStore()

    def _get_user_dir(self, user_id: str) -> Path:
        """Get task storage directory for a user."""
        user_dir = self.storage_dir / user_id
        user_dir.mkdir(parents=True, exist_ok=True)
        return user_dir

    def _get_list_file(self, user_id: str, list_id: str) -> Path:
        """Get path to a list file."""
        return self._get_user_dir(user_id) / f"{list_id}.json"

    def _load_list(self, user_id: str, list_id: str) -> Optional[TaskList]:
        """Load a task list."""
        file_path = self._get_list_file(user_id, list_id)
        if not file_path.exists():
            return None

        try:
            data = json.loads(file_path.read_text())
            return TaskList(**data)
        except Exception as e:
            logger.error("Failed to load list %s: %s", list_id, e)
            return None

    def _save_list(self, task_list: TaskList) -> None:
        """Save a task list."""
        file_path = self._get_list_file(task_list.user_id, task_list.list_id)
        task_list.updated_at = datetime.utcnow().isoformat()
        file_path.write_text(json.dumps(task_list.model_dump(), indent=2, default=str))

    def create_list(self, request: CreateListRequest) -> TaskList:
        """
        Create a new task list.

        Args:
            request: Create list request

        Returns:
            Created TaskList
        """
        list_id = str(uuid4())[:8]

        task_list = TaskList(
            list_id=list_id,
            user_id=request.user_id,
            name=request.name,
            description=request.description,
            is_recurring=request.is_recurring,
            recurrence_pattern=request.recurrence_pattern,
        )

        self._save_list(task_list)
        logger.info("Created list %s for user %s", list_id, request.user_id)

        return task_list

    def get_list(self, user_id: str, list_id: str) -> Optional[TaskList]:
        """Get a task list by ID."""
        return self._load_list(user_id, list_id)

    def get_all_lists(self, user_id: str) -> List[TaskList]:
        """Get all task lists for a user."""
        user_dir = self._get_user_dir(user_id)
        lists = []

        for file_path in user_dir.glob("*.json"):
            try:
                data = json.loads(file_path.read_text())
                lists.append(TaskList(**data))
            except Exception as e:
                logger.warning("Failed to load list %s: %s", file_path.name, e)

        return lists

    def get_or_create_default_list(self, user_id: str, name: str = "Tasks") -> TaskList:
        """Get or create a default list."""
        lists = self.get_all_lists(user_id)

        for lst in lists:
            if lst.name.lower() == name.lower():
                return lst

        return self.create_list(
            CreateListRequest(
                user_id=user_id,
                name=name,
            )
        )

    def add_item(self, request: AddItemRequest) -> TaskItem:
        """
        Add an item to a task list.

        Args:
            request: Add item request

        Returns:
            Created TaskItem
        """
        task_list = self._load_list(request.user_id, request.list_id)
        if task_list is None:
            task_list = self.get_or_create_default_list(request.user_id)

        item = TaskItem(
            item_id=str(uuid4())[:8],
            description=request.description,
            quantity=request.quantity,
            priority=request.priority,
            due_date=request.due_date,
            notes=request.notes,
            tags=request.tags,
        )

        task_list.items.append(item)
        self._save_list(task_list)

        return item

    def add_items(self, user_id: str, list_id: str, items: List[Dict[str, Any]]) -> List[TaskItem]:
        """Add multiple items to a list."""
        task_list = self._load_list(user_id, list_id)
        if task_list is None:
            task_list = self.get_or_create_default_list(user_id)

        added_items = []
        for item_data in items:
            item = TaskItem(
                item_id=str(uuid4())[:8],
                description=item_data.get("description", ""),
                quantity=item_data.get("quantity"),
                priority=Priority(item_data.get("priority", "medium")),
                tags=item_data.get("tags", []),
            )
            task_list.items.append(item)
            added_items.append(item)

        self._save_list(task_list)
        return added_items

    def parse_items_from_text(
        self,
        user_id: str,
        text: str,
    ) -> ParseTasksResult:
        """
        Parse task items from natural language.

        Args:
            user_id: The user ID
            text: Natural language text

        Returns:
            Parsed tasks result
        """
        existing_lists = self.get_all_lists(user_id)
        list_names = [lst.name for lst in existing_lists] or ["Tasks", "Groceries"]

        prompt = PARSE_TASKS_PROMPT.format(
            text=text,
            existing_lists=", ".join(list_names),
            current_date=datetime.utcnow().strftime("%Y-%m-%d"),
        )

        try:
            data = self.llm.complete_json(
                prompt,
                temperature=0.2,
                expected_keys=["items", "list_name"],
            )
        except JSONExtractionFailure as e:
            logger.error("Failed to parse tasks (JSON extraction failed):\n%s", e)
            return ParseTasksResult(items=[])
        except Exception as e:
            logger.error("Failed to parse tasks: %s", e)
            return ParseTasksResult(items=[])

        items = []
        for item_data in data.get("items", []):
            items.append(
                ParsedTaskItem(
                    description=item_data.get("description", ""),
                    quantity=item_data.get("quantity"),
                    priority=Priority(item_data.get("priority", "medium")),
                    due_date=item_data.get("due_date"),
                    tags=item_data.get("tags", []),
                )
            )

        return ParseTasksResult(
            list_name=data.get("list_name", "default"),
            items=items,
        )

    def add_items_from_text(self, request: AddItemsFromTextRequest) -> Dict[str, Any]:
        """
        Parse and add items from natural language.

        Args:
            request: Add items request with text

        Returns:
            Result with added items
        """
        parsed = self.parse_items_from_text(request.user_id, request.text)

        if not parsed.items:
            return {
                "success": False,
                "message": "Could not parse any items from the text",
            }

        list_id = request.list_id
        if not list_id:
            list_name = parsed.list_name
            if list_name == "default":
                list_name = "Tasks"

            task_list = self.get_or_create_default_list(request.user_id, list_name)
            list_id = task_list.list_id

        items_data = [
            {
                "description": item.description,
                "quantity": item.quantity,
                "priority": item.priority.value,
                "tags": item.tags,
            }
            for item in parsed.items
        ]

        added = self.add_items(request.user_id, list_id, items_data)

        return {
            "success": True,
            "list_id": list_id,
            "added_items": [item.model_dump() for item in added],
        }

    def update_item(self, request: UpdateItemRequest) -> bool:
        """
        Update a task item.

        Args:
            request: Update request

        Returns:
            True if successful
        """
        task_list = self._load_list(request.user_id, request.list_id)
        if task_list is None:
            return False

        for item in task_list.items:
            if item.item_id == request.item_id:
                for key, value in request.updates.items():
                    if hasattr(item, key):
                        setattr(item, key, value)
                self._save_list(task_list)
                return True

        return False

    def complete_item(self, request: CompleteItemRequest) -> bool:
        """
        Mark an item as completed.

        Args:
            request: Complete request

        Returns:
            True if successful
        """
        task_list = self._load_list(request.user_id, request.list_id)
        if task_list is None:
            return False

        for item in task_list.items:
            if item.item_id == request.item_id:
                item.status = TaskStatus.COMPLETED
                item.completed_at = datetime.utcnow().isoformat()
                self._save_list(task_list)
                return True

        return False

    def delete_item(self, user_id: str, list_id: str, item_id: str) -> bool:
        """Delete an item from a list."""
        task_list = self._load_list(user_id, list_id)
        if task_list is None:
            return False

        original_count = len(task_list.items)
        task_list.items = [i for i in task_list.items if i.item_id != item_id]

        if len(task_list.items) < original_count:
            self._save_list(task_list)
            return True

        return False

    def delete_list(self, user_id: str, list_id: str) -> bool:
        """Delete a task list."""
        file_path = self._get_list_file(user_id, list_id)
        if file_path.exists():
            file_path.unlink()
            return True
        return False

    def list_items(self, request: ListTasksRequest) -> List[TaskItem]:
        """
        List items matching criteria.

        Args:
            request: List request

        Returns:
            Matching items
        """
        if request.list_id:
            task_list = self._load_list(request.user_id, request.list_id)
            if task_list is None:
                return []
            items = task_list.items
        else:
            items = []
            for lst in self.get_all_lists(request.user_id):
                items.extend(lst.items)

        if request.status_filter:
            items = [i for i in items if i.status == request.status_filter]
        elif not request.include_completed:
            items = [i for i in items if i.status != TaskStatus.COMPLETED]

        return items

    def get_pending_items(self, user_id: str) -> List[TaskItem]:
        """Get all pending items across all lists."""
        return self.list_items(
            ListTasksRequest(
                user_id=user_id,
                status_filter=TaskStatus.PENDING,
            )
        )

    def categorize_grocery_items(self, items: List[str]) -> List[Dict[str, str]]:
        """
        Categorize grocery items by department/aisle.

        Args:
            items: List of item descriptions

        Returns:
            Categorized items
        """
        prompt = CATEGORIZE_ITEMS_PROMPT.format(
            items="\n".join(f"- {item}" for item in items),
        )

        try:
            data = self.llm.complete_json(
                prompt,
                temperature=0.1,
                expected_keys=["categorized_items"],
            )
            return data.get("categorized_items", [])
        except JSONExtractionFailure as e:
            logger.error("Failed to categorize items (JSON extraction failed):\n%s", e)
            return [{"description": item, "category": "other"} for item in items]
        except Exception as e:
            logger.error("Failed to categorize items: %s", e)
            return [{"description": item, "category": "other"} for item in items]

    def suggest_items(self, user_id: str, list_id: str) -> List[Dict[str, Any]]:
        """
        Suggest items based on user profile and history.

        Args:
            user_id: The user ID
            list_id: Current list ID

        Returns:
            List of suggestions
        """
        profile = self.profile_store.load_profile(user_id)
        preferences = ""
        if profile:
            if profile.preferences.food_likes:
                preferences += f"Food likes: {', '.join(profile.preferences.food_likes[:10])}\n"
            if profile.preferences.dietary_restrictions:
                preferences += (
                    f"Dietary restrictions: {', '.join(profile.preferences.dietary_restrictions)}\n"
                )

        current_list = self._load_list(user_id, list_id)
        current_items = ""
        if current_list:
            current_items = "\n".join(f"- {i.description}" for i in current_list.items[:20])

        all_lists = self.get_all_lists(user_id)
        recent_items = []
        for lst in all_lists[:3]:
            recent_items.extend(i.description for i in lst.items[:10])

        prompt = SUGGEST_ITEMS_PROMPT.format(
            preferences=preferences or "No preferences available",
            recent_lists="\n".join(f"- {i}" for i in recent_items[:20]) or "No recent lists",
            current_list=current_items or "Empty list",
        )

        try:
            data = self.llm.complete_json(
                prompt,
                temperature=0.4,
                expected_keys=["suggestions"],
            )
            return data.get("suggestions", [])
        except JSONExtractionFailure as e:
            logger.error("Failed to suggest items (JSON extraction failed):\n%s", e)
            return []
        except Exception as e:
            logger.error("Failed to suggest items: %s", e)
            return []
