"""Unit tests for llm_service config resolution."""

import pytest

from llm_service import config


def test_resolve_provider_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    assert config.resolve_provider() == "ollama"


def test_resolve_provider_dummy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "dummy")
    assert config.resolve_provider() == "dummy"


def test_resolve_model_agent_key_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.setenv("LLM_MODEL_soc2", "custom-model")
    assert config.resolve_model("soc2") == "custom-model"


def test_resolve_model_global_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_MODEL", "global-model")
    monkeypatch.delenv("LLM_MODEL_soc2", raising=False)
    assert config.resolve_model("soc2") == "global-model"


def test_resolve_model_agent_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.delenv("LLM_MODEL_backend", raising=False)
    assert config.resolve_model("backend") == "qwen3.5:397b-cloud"


def test_resolve_base_url_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    assert config.resolve_base_url() == "https://ollama.com"


def test_resolve_timeout_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LLM_TIMEOUT", raising=False)
    assert config.resolve_timeout() == 600.0


def test_resolve_context_size_for_model_known(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LLM_CONTEXT_SIZE", raising=False)
    assert config.resolve_context_size_for_model("qwen3.5:397b-cloud") == 262144


def test_resolve_context_size_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_CONTEXT_SIZE", "100000")
    assert config.resolve_context_size_for_model("unknown-model") == 100000
