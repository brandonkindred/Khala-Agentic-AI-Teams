"""Unit tests for LLM client error handling, retries, and exceptions."""

import json
import os
from unittest.mock import MagicMock, patch

import httpx
import pytest

from shared.llm import (
    LLMPermanentError,
    LLMRateLimitError,
    LLMTemporaryError,
    OllamaLLMClient,
)


def test_ollama_429_raises_rate_limit_error_after_retries() -> None:
    """When Ollama returns 429 repeatedly, LLMRateLimitError is raised after max retries."""
    client = OllamaLLMClient(model="test", base_url="http://localhost:9999", timeout=5)
    with patch("httpx.Client") as mock_client_cls:
        mock_response = MagicMock()
        mock_response.status_code = 429
        mock_response.headers = {}
        mock_response.text = "Too Many Requests"
        mock_client = MagicMock()
        mock_client.post.return_value = mock_response
        mock_client_cls.return_value.__enter__.return_value = mock_client

        with patch.dict(os.environ, {"SW_LLM_MAX_RETRIES": "1"}, clear=False):
            with pytest.raises(LLMRateLimitError) as exc_info:
                client.complete_json("test prompt")

    assert exc_info.value.status_code == 429
    assert "429" in str(exc_info.value) or "rate" in str(exc_info.value).lower()


def test_ollama_500_raises_temporary_error_after_retries() -> None:
    """When Ollama returns 500 repeatedly, LLMTemporaryError is raised after max retries."""
    client = OllamaLLMClient(model="test", base_url="http://localhost:9999", timeout=5)
    with patch("httpx.Client") as mock_client_cls:
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.text = "Internal Server Error"
        mock_client = MagicMock()
        mock_client.post.return_value = mock_response
        mock_client_cls.return_value.__enter__.return_value = mock_client

        with patch.dict(os.environ, {"SW_LLM_MAX_RETRIES": "1"}, clear=False):
            with pytest.raises(LLMTemporaryError) as exc_info:
                client.complete_json("test prompt")

    assert exc_info.value.status_code == 500


def test_ollama_400_raises_permanent_error_no_retry() -> None:
    """When Ollama returns 400, LLMPermanentError is raised immediately (no retry)."""
    client = OllamaLLMClient(model="test", base_url="http://localhost:9999", timeout=5)
    with patch("httpx.Client") as mock_client_cls:
        mock_response = MagicMock()
        mock_response.status_code = 400
        mock_response.text = "Bad Request"
        mock_client = MagicMock()
        mock_client.post.return_value = mock_response
        mock_client_cls.return_value.__enter__.return_value = mock_client

        with pytest.raises(LLMPermanentError) as exc_info:
            client.complete_json("test prompt")

    assert exc_info.value.status_code == 400
    assert "400" in str(exc_info.value)


def test_ollama_200_returns_parsed_json() -> None:
    """When Ollama returns 200 with valid JSON, content is parsed and returned."""
    client = OllamaLLMClient(model="test", base_url="http://localhost:9999", timeout=5)
    with patch("httpx.Client") as mock_client_cls:
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "choices": [{"message": {"content": '{"key": "value"}'}}]
        }
        mock_client = MagicMock()
        mock_client.post.return_value = mock_response
        mock_client_cls.return_value.__enter__.return_value = mock_client

        result = client.complete_json("test prompt")

    assert result == {"key": "value"}


def test_ollama_malformed_response_raises_permanent_error() -> None:
    """When response has missing choices/message/content, LLMPermanentError is raised."""
    client = OllamaLLMClient(model="test", base_url="http://localhost:9999", timeout=5)
    with patch("httpx.Client") as mock_client_cls:
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"choices": []}  # No first choice
        mock_client = MagicMock()
        mock_client.post.return_value = mock_response
        mock_client_cls.return_value.__enter__.return_value = mock_client

        with pytest.raises(LLMPermanentError) as exc_info:
            client.complete_json("test prompt")

    assert "choices" in str(exc_info.value).lower() or "format" in str(exc_info.value).lower()


def test_ollama_connection_error_raises_temporary_error_after_retries() -> None:
    """When connection fails, LLMTemporaryError is raised after retries."""
    client = OllamaLLMClient(model="test", base_url="http://localhost:9999", timeout=5)
    with patch("httpx.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client.post.side_effect = httpx.ConnectError("Connection refused")
        mock_client_cls.return_value.__enter__.return_value = mock_client

        with patch.dict(os.environ, {"SW_LLM_MAX_RETRIES": "1"}, clear=False):
            with pytest.raises(LLMTemporaryError) as exc_info:
                client.complete_json("test prompt")

    assert "connection" in str(exc_info.value).lower() or "Connection" in str(exc_info.value)
