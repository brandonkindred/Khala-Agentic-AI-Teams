"""Tests for OllamaLLMClient with mocked httpx."""

import json
from unittest.mock import MagicMock, patch

import pytest

from llm_service.clients.ollama import OllamaLLMClient
from llm_service.interface import LLMJsonParseError, LLMRateLimitError, LLMPermanentError


def test_ollama_get_max_context_tokens_known_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LLM_CONTEXT_SIZE", raising=False)
    monkeypatch.delenv("SW_LLM_CONTEXT_SIZE", raising=False)
    client = OllamaLLMClient(model="qwen3.5:397b-cloud", base_url="http://localhost:9999", timeout=5)
    assert client.get_max_context_tokens() == 262144


def test_ollama_get_max_context_tokens_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_CONTEXT_SIZE", "50000")
    client = OllamaLLMClient(model="unknown-model", base_url="http://localhost:9999", timeout=5)
    assert client.get_max_context_tokens() == 50000


def test_ollama_complete_json_parses_response(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "choices": [
            {
                "message": {"content": '{"answer": 42}'},
                "finish_reason": "stop",
            }
        ]
    }
    with patch("httpx.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client.__enter__.return_value.post.return_value = mock_response
        mock_client_cls.return_value = mock_client
        client = OllamaLLMClient(model="test", base_url="http://localhost:9999", timeout=5)
        result = client.complete_json("What is 6*7?", temperature=0)
    assert result == {"answer": 42}


def test_ollama_complete_json_429_raises_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("LLM_MAX_RETRIES", "0")
    mock_response = MagicMock()
    mock_response.status_code = 429
    mock_response.text = "Rate limited"
    with patch("httpx.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client.__enter__.return_value.post.return_value = mock_response
        mock_client_cls.return_value = mock_client
        client = OllamaLLMClient(model="test", base_url="http://localhost:9999", timeout=5)
        with pytest.raises(LLMRateLimitError) as exc_info:
            client.complete_json("hello", temperature=0)
        assert exc_info.value.status_code == 429


def test_ollama_complete_json_404_raises_permanent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("LLM_MAX_RETRIES", "0")
    mock_response = MagicMock()
    mock_response.status_code = 404
    mock_response.text = '{"error":{"message":"model not found"}}'
    with patch("httpx.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client.__enter__.return_value.post.return_value = mock_response
        mock_client_cls.return_value = mock_client
        client = OllamaLLMClient(model="test", base_url="http://localhost:9999", timeout=5)
        with pytest.raises(LLMPermanentError) as exc_info:
            client.complete_json("hello", temperature=0)
        assert exc_info.value.status_code == 404
