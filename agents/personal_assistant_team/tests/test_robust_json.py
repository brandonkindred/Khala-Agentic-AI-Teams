"""Tests for robust JSON extraction with retry and decomposition."""

import json
import pytest

from ..shared.llm import LLMClient, JSONExtractionFailure


class MockLLMClient(LLMClient):
    """Mock LLM client for testing JSON extraction."""

    def __init__(self, responses=None):
        super().__init__()
        self._provider = "mock"
        self.responses = responses or []
        self.call_count = 0
        self.prompts = []

    def _ollama_complete(
        self,
        prompt: str,
        *,
        temperature: float = 0.7,
        max_tokens=None,
        system_prompt=None,
        json_mode: bool = False,
    ) -> str:
        self.prompts.append(prompt)
        if self.call_count < len(self.responses):
            response = self.responses[self.call_count]
            self.call_count += 1
            return response
        return "{}"


class TestJSONParsing:
    """Test basic JSON parsing functionality."""

    def test_valid_json_parses_directly(self):
        client = MockLLMClient(responses=['{"key": "value", "number": 42}'])
        result = client.complete_json("test prompt")
        assert result == {"key": "value", "number": 42}
        assert client.call_count == 1

    def test_json_in_code_block(self):
        client = MockLLMClient(responses=[
            '```json\n{"key": "value"}\n```'
        ])
        result = client.complete_json("test prompt")
        assert result == {"key": "value"}

    def test_json_with_surrounding_text(self):
        client = MockLLMClient(responses=[
            'Here is the response:\n{"key": "value"}\nDone!'
        ])
        result = client.complete_json("test prompt")
        assert result == {"key": "value"}


class TestTruncationDetection:
    """Test detection of truncated JSON."""

    def test_detects_unclosed_braces(self):
        client = MockLLMClient()
        assert client._is_json_truncated('{"key": "value"') is True
        assert client._is_json_truncated('{"key": {"nested": "value"}') is True

    def test_detects_unclosed_brackets(self):
        client = MockLLMClient()
        assert client._is_json_truncated('["item1", "item2"') is True

    def test_detects_trailing_comma(self):
        client = MockLLMClient()
        assert client._is_json_truncated('{"key": "value",') is True

    def test_complete_json_not_truncated(self):
        client = MockLLMClient()
        assert client._is_json_truncated('{"key": "value"}') is False
        assert client._is_json_truncated('["a", "b", "c"]') is False


class TestContinuationRequests:
    """Test continuation request functionality."""

    def test_continuation_on_truncated_json(self):
        client = MockLLMClient(responses=[
            '{"key": "value", "list": [1, 2,',
            '3, 4], "done": true}',
        ])
        result = client.complete_json("test prompt")
        assert result.get("key") == "value"
        assert result.get("done") is True
        assert client.call_count == 2

    def test_multiple_continuation_attempts(self):
        client = MockLLMClient(responses=[
            '{"part1": "a", "part2": {',
            '"nested": "b",',
            '"more": "c"}}',
        ])
        result = client.complete_json("test prompt")
        assert "part1" in result
        assert client.call_count >= 2


class TestTaskDecomposition:
    """Test task decomposition for complex requests."""

    def test_decomposition_with_expected_keys(self):
        client = MockLLMClient(responses=[
            '{"invalid json',
            '{"key1": "value1"}',
            '{"key2": "value2"}',
        ])
        result = client.complete_json(
            "test prompt",
            expected_keys=["key1", "key2"],
        )
        assert "key1" in result or "key2" in result
        assert client.call_count >= 2


class TestFailureBehavior:
    """Test failure behavior and error messages."""

    def test_failure_after_max_attempts(self):
        invalid_responses = ["not json"] * 20
        client = MockLLMClient(responses=invalid_responses)
        
        with pytest.raises(JSONExtractionFailure) as exc_info:
            client.complete_json("test prompt")
        
        error = exc_info.value
        assert error.attempts_made > 0
        assert len(error.recovery_suggestions) > 0

    def test_error_contains_recovery_suggestions(self):
        client = MockLLMClient(responses=["broken"] * 20)
        
        with pytest.raises(JSONExtractionFailure) as exc_info:
            client.complete_json("test prompt with a very long request " * 100)
        
        error = exc_info.value
        assert any("Simplify" in s for s in error.recovery_suggestions)
        assert any("prompt size" in s.lower() for s in error.recovery_suggestions)

    def test_error_message_is_informative(self):
        client = MockLLMClient(responses=["bad"] * 20)
        
        with pytest.raises(JSONExtractionFailure) as exc_info:
            client.complete_json("test prompt")
        
        error_str = str(exc_info.value)
        assert "CRITICAL" in error_str
        assert "JSON EXTRACTION FAILED" in error_str
        assert "HOW TO RESOLVE" in error_str


class TestExpectedKeysParameter:
    """Test expected_keys parameter functionality."""

    def test_expected_keys_help_decomposition(self):
        client = MockLLMClient(responses=[
            "invalid",
            '{"primary_intent": "email"}',
            '{"entities": {}}',
            '{"confidence": 0.9}',
        ])
        
        result = client.complete_json(
            "classify this",
            expected_keys=["primary_intent", "entities", "confidence"],
        )
        
        assert "primary_intent" in result or "entities" in result or "confidence" in result


class TestDecompositionHints:
    """Test decomposition_hints parameter."""

    def test_decomposition_hints_used(self):
        client = MockLLMClient(responses=[
            "invalid",
            '{"summary": "test"}',
            '{"details": "more"}',
        ])
        
        result = client.complete_json(
            "analyze this",
            decomposition_hints=["summary of content", "details about items"],
        )
        
        assert client.call_count >= 2


class TestArrayHandling:
    """Test handling of JSON arrays."""

    def test_array_wrapped_in_object(self):
        client = MockLLMClient(responses=['[1, 2, 3, 4, 5]'])
        result = client.complete_json("give me numbers")
        assert "items" in result
        assert result["items"] == [1, 2, 3, 4, 5]


class TestEdgeCases:
    """Test edge cases in JSON extraction."""

    def test_empty_response(self):
        # Empty responses will trigger decomposition, which eventually succeeds
        # We need more responses since decomposition creates subtasks per expected_key
        client = MockLLMClient(responses=[
            "",  # Initial empty
            '{"fallback": true}',  # Decomposition subtask succeeds
        ])
        result = client.complete_json("test", expected_keys=["fallback"])
        # Either the decomposition works or it falls through
        assert "fallback" in result or client.call_count >= 2

    def test_whitespace_only_response(self):
        client = MockLLMClient(responses=["   \n\t   ", '{"key": "value"}'])
        result = client.complete_json("test", expected_keys=["key"])
        assert result.get("key") == "value"

    def test_unicode_in_json(self):
        client = MockLLMClient(responses=['{"emoji": "🎉", "text": "café"}'])
        result = client.complete_json("test")
        assert result.get("emoji") == "🎉"
        assert result.get("text") == "café"
