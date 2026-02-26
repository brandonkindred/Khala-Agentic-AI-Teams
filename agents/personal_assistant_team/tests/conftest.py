"""Pytest configuration and fixtures for Personal Assistant tests."""

import pytest
import tempfile
import shutil
from pathlib import Path


@pytest.fixture(scope="session")
def test_data_dir():
    """Create a session-scoped temporary directory for test data."""
    temp = tempfile.mkdtemp(prefix="pa_test_")
    yield Path(temp)
    shutil.rmtree(temp)


@pytest.fixture
def user_id():
    """Provide a test user ID."""
    return "test_user_123"


class DummyLLM:
    """A dummy LLM client for testing that returns predefined responses."""

    def __init__(self, responses=None):
        self.responses = responses or {}
        self.calls = []

    def complete(self, prompt, **kwargs):
        self.calls.append(("complete", prompt, kwargs))
        return self.responses.get("complete", "Test response")

    def complete_json(self, prompt, **kwargs):
        self.calls.append(("complete_json", prompt, kwargs))
        
        if "intent" in prompt.lower():
            return {
                "primary_intent": "general",
                "secondary_intents": [],
                "entities": {},
                "confidence": 0.8,
            }
        
        if "extract" in prompt.lower():
            return {
                "extracted_info": [],
                "reasoning": "No info extracted",
            }
        
        return self.responses.get("complete_json", {"status": "ok"})


@pytest.fixture
def dummy_llm():
    """Provide a dummy LLM client."""
    return DummyLLM()
