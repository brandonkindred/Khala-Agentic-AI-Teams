"""Tests for the Strands ModelProvider adapter (llm_service.strands_provider)."""

from __future__ import annotations

import os
from unittest import mock

import pytest
from strands.models.ollama import OllamaModel

from llm_service.strands_provider import (
    _clear_strands_model_cache_for_testing,
    _resolve_ollama_auth_headers,
    get_strands_model,
)


@pytest.fixture(autouse=True)
def _clear_cache():
    """Clear model cache before each test."""
    _clear_strands_model_cache_for_testing()
    yield
    _clear_strands_model_cache_for_testing()


class TestResolveOllamaAuthHeaders:
    def test_no_key_set(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            assert _resolve_ollama_auth_headers() == {}

    def test_ollama_api_key(self):
        with mock.patch.dict(os.environ, {"OLLAMA_API_KEY": "test-key"}, clear=True):
            headers = _resolve_ollama_auth_headers()
            assert headers == {"Authorization": "Bearer test-key"}

    def test_llm_ollama_api_key_fallback(self):
        with mock.patch.dict(os.environ, {"LLM_OLLAMA_API_KEY": "alt-key"}, clear=True):
            headers = _resolve_ollama_auth_headers()
            assert headers == {"Authorization": "Bearer alt-key"}


class TestGetStrandsModel:
    def test_returns_ollama_model(self):
        with mock.patch.dict(os.environ, {"LLM_MODEL": "test-model", "LLM_BASE_URL": "http://localhost:11434"}):
            model = get_strands_model()
            assert isinstance(model, OllamaModel)

    def test_caches_by_model_and_url(self):
        with mock.patch.dict(os.environ, {"LLM_MODEL": "test-model", "LLM_BASE_URL": "http://localhost:11434"}):
            m1 = get_strands_model()
            m2 = get_strands_model()
            assert m1 is m2

    def test_different_agent_keys_with_same_model_share_cache(self):
        with mock.patch.dict(os.environ, {"LLM_MODEL": "shared-model", "LLM_BASE_URL": "http://localhost:11434"}):
            m1 = get_strands_model("agent_a")
            m2 = get_strands_model("agent_b")
            assert m1 is m2

    def test_per_agent_model_override(self):
        with mock.patch.dict(
            os.environ,
            {
                "LLM_MODEL": "default-model",
                "LLM_MODEL_special": "special-model",
                "LLM_BASE_URL": "http://localhost:11434",
            },
        ):
            default = get_strands_model()
            special = get_strands_model("special")
            assert default is not special

    def test_max_tokens_from_env(self):
        with mock.patch.dict(
            os.environ,
            {"LLM_MODEL": "test-model", "LLM_BASE_URL": "http://localhost:11434", "LLM_MAX_TOKENS": "4096"},
        ):
            model = get_strands_model()
            assert isinstance(model, OllamaModel)
            assert model.config.get("max_tokens") == 4096
