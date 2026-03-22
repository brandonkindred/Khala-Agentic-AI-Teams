"""Tests for TaskAgent."""

import shutil
import tempfile

import pytest

from ..models import Priority, TaskStatus
from ..shared.llm import LLMClient
from ..task_agent.agent import TaskAgent
from ..task_agent.models import (
    AddItemRequest,
    AddItemsFromTextRequest,
    CompleteItemRequest,
    CreateListRequest,
)


class DummyLLMClient(LLMClient):
    """Dummy LLM client for testing."""

    def __init__(self):
        pass

    def complete(self, prompt: str, **kwargs) -> str:
        return "Test response"

    def complete_json(self, prompt: str, **kwargs):
        if "grocery" in prompt.lower() or "milk" in prompt.lower():
            return {
                "list_name": "Groceries",
                "items": [
                    {"description": "Milk", "quantity": "1 gallon", "priority": "medium"},
                    {"description": "Bread", "quantity": None, "priority": "medium"},
                ],
            }
        return {"list_name": "Tasks", "items": []}


class TestTaskAgent:
    """Tests for TaskAgent."""

    @pytest.fixture
    def temp_dir(self):
        """Create a temporary directory for testing."""
        temp = tempfile.mkdtemp()
        yield temp
        shutil.rmtree(temp)

    @pytest.fixture
    def agent(self, temp_dir):
        """Create a TaskAgent with dummy LLM."""
        return TaskAgent(
            llm=DummyLLMClient(),
            storage_dir=temp_dir,
        )

    def test_create_list(self, agent):
        """Test creating a task list."""
        task_list = agent.create_list(CreateListRequest(
            user_id="test_user",
            name="Groceries",
            description="Weekly shopping",
        ))
        
        assert task_list.name == "Groceries"
        assert task_list.user_id == "test_user"
        assert len(task_list.items) == 0

    def test_get_all_lists(self, agent):
        """Test getting all lists for a user."""
        agent.create_list(CreateListRequest(user_id="test_user", name="Groceries"))
        agent.create_list(CreateListRequest(user_id="test_user", name="Todos"))
        
        lists = agent.get_all_lists("test_user")
        
        assert len(lists) == 2
        names = [lst.name for lst in lists]
        assert "Groceries" in names
        assert "Todos" in names

    def test_add_item(self, agent):
        """Test adding an item to a list."""
        task_list = agent.create_list(CreateListRequest(
            user_id="test_user",
            name="Groceries",
        ))
        
        item = agent.add_item(AddItemRequest(
            user_id="test_user",
            list_id=task_list.list_id,
            description="Milk",
            quantity="2 gallons",
            priority=Priority.HIGH,
        ))
        
        assert item.description == "Milk"
        assert item.quantity == "2 gallons"
        assert item.priority == Priority.HIGH

    def test_add_items(self, agent):
        """Test adding multiple items."""
        task_list = agent.create_list(CreateListRequest(
            user_id="test_user",
            name="Groceries",
        ))
        
        items = agent.add_items("test_user", task_list.list_id, [
            {"description": "Milk", "priority": "high"},
            {"description": "Bread", "priority": "medium"},
            {"description": "Eggs", "priority": "medium"},
        ])
        
        assert len(items) == 3

    def test_add_items_from_text(self, agent):
        """Test adding items from natural language."""
        result = agent.add_items_from_text(AddItemsFromTextRequest(
            user_id="test_user",
            text="I need to buy milk and bread from the grocery store",
        ))
        
        assert result["success"] is True
        assert len(result["added_items"]) >= 1

    def test_complete_item(self, agent):
        """Test completing an item."""
        task_list = agent.create_list(CreateListRequest(
            user_id="test_user",
            name="Tasks",
        ))
        
        item = agent.add_item(AddItemRequest(
            user_id="test_user",
            list_id=task_list.list_id,
            description="Test task",
        ))
        
        result = agent.complete_item(CompleteItemRequest(
            user_id="test_user",
            list_id=task_list.list_id,
            item_id=item.item_id,
        ))
        
        assert result is True
        
        updated_list = agent.get_list("test_user", task_list.list_id)
        completed_item = next(i for i in updated_list.items if i.item_id == item.item_id)
        assert completed_item.status == TaskStatus.COMPLETED

    def test_delete_item(self, agent):
        """Test deleting an item."""
        task_list = agent.create_list(CreateListRequest(
            user_id="test_user",
            name="Tasks",
        ))
        
        item = agent.add_item(AddItemRequest(
            user_id="test_user",
            list_id=task_list.list_id,
            description="Test task",
        ))
        
        result = agent.delete_item("test_user", task_list.list_id, item.item_id)
        
        assert result is True
        
        updated_list = agent.get_list("test_user", task_list.list_id)
        assert len(updated_list.items) == 0

    def test_get_pending_items(self, agent):
        """Test getting pending items across all lists."""
        list1 = agent.create_list(CreateListRequest(user_id="test_user", name="List1"))
        list2 = agent.create_list(CreateListRequest(user_id="test_user", name="List2"))
        
        agent.add_item(AddItemRequest(user_id="test_user", list_id=list1.list_id, description="Task 1"))
        agent.add_item(AddItemRequest(user_id="test_user", list_id=list2.list_id, description="Task 2"))
        
        pending = agent.get_pending_items("test_user")
        
        assert len(pending) == 2

    def test_delete_list(self, agent):
        """Test deleting a list."""
        task_list = agent.create_list(CreateListRequest(
            user_id="test_user",
            name="Test List",
        ))
        
        result = agent.delete_list("test_user", task_list.list_id)
        
        assert result is True
        assert agent.get_list("test_user", task_list.list_id) is None
