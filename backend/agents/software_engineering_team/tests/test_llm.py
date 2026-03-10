"""Unit tests for LLM client error handling, retries, and exceptions."""

import json
import os
from unittest.mock import MagicMock, patch

import httpx
import pytest

from software_engineering_team.shared.llm import (
    DummyLLMClient,
    LLMJsonParseError,
    LLMPermanentError,
    LLMRateLimitError,
    LLMTemporaryError,
    OLLAMA_WEEKLY_LIMIT_MESSAGE,
    OllamaLLMClient,
    _clear_client_cache_for_testing,
    get_llm_for_agent,
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


def test_ollama_weekly_limit_message_constant() -> None:
    """OLLAMA_WEEKLY_LIMIT_MESSAGE is defined for use in orchestrator and logs."""
    assert OLLAMA_WEEKLY_LIMIT_MESSAGE == "Ollama LLM usage limit exceeded for week"


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


def test_extract_json_valid_json_inside_markdown_fence() -> None:
    """When response is markdown with a JSON code block, _extract_json returns parsed dict not raw wrapper."""
    client = OllamaLLMClient(model="test", base_url="http://localhost:9999", timeout=5)
    text = 'Here is the result:\n```json\n{"files": {"a.py": "x"}, "summary": "Done"}\n```'
    result = client._extract_json(text)
    assert result == {"files": {"a.py": "x"}, "summary": "Done"}
    assert "content" not in result or result.get("files") is not None


def test_extract_json_object_extraction_fallback() -> None:
    """When text contains a JSON object (e.g. on same line), _extract_json can recover it."""
    client = OllamaLLMClient(model="test", base_url="http://localhost:9999", timeout=5)
    text = 'The response is: {"summary": "ok", "approved": true}'
    result = client._extract_json(text)
    assert isinstance(result, dict)
    assert result.get("summary") == "ok"
    assert result.get("approved") is True


def test_extract_json_unparseable_raises_llm_permanent_error() -> None:
    """When no JSON can be recovered, _extract_json raises LLMPermanentError (fail fast)."""
    client = OllamaLLMClient(model="test", base_url="http://localhost:9999", timeout=5)
    text = "no code blocks or json here at all"
    with pytest.raises(LLMPermanentError, match="Could not parse structured JSON"):
        client._extract_json(text)


def test_extract_json_unparseable_raises_json_parse_error_subclass() -> None:
    """Unparseable text raises LLMJsonParseError (subclass of LLMPermanentError)."""
    client = OllamaLLMClient(model="test", base_url="http://localhost:9999", timeout=5)
    text = "absolutely no JSON here"
    with pytest.raises(LLMJsonParseError) as exc_info:
        client._extract_json(text)
    assert exc_info.value.error_kind == "json_parse"
    assert exc_info.value.response_preview


def test_extract_json_truncated_json_not_repaired() -> None:
    """Truncated JSON is NOT silently repaired; LLMJsonParseError is raised."""
    client = OllamaLLMClient(model="test", base_url="http://localhost:9999", timeout=5)
    truncated = '{"tasks": [{"id": "t1", "title": "Setup'
    with pytest.raises(LLMJsonParseError):
        client._extract_json(truncated)


def test_complete_json_continue_on_parse_error_returns_merged() -> None:
    """When first response fails to parse, complete_json tries one 'continue' request and merges."""
    client = OllamaLLMClient(model="test", base_url="http://localhost:9999", timeout=5)
    # First response: no valid JSON (so all repair strategies fail, LLMJsonParseError raised).
    # Second: continuation that contains valid JSON; merged string still has that object.
    first_content = "no valid json here"
    second_content = '{"n": 1}'
    with patch.object(client, "_ollama_post") as mock_post:
        mock_post.side_effect = [first_content, second_content]
        result = client.complete_json("test prompt")
    assert result == {"n": 1}
    assert mock_post.call_count == 2
    # Second call should be multi-turn (system, user, assistant, user).
    second_payload = mock_post.call_args_list[1][0][0]
    messages = second_payload.get("messages", [])
    assert len(messages) == 4
    assert messages[0]["role"] == "system"
    assert messages[1]["role"] == "user"
    assert messages[2]["role"] == "assistant"
    assert messages[2]["content"] == first_content
    assert messages[3]["role"] == "user"
    assert "Continue" in messages[3]["content"]


def test_complete_json_continue_still_invalid_raises() -> None:
    """When continue response still does not parse, LLMJsonParseError is re-raised."""
    client = OllamaLLMClient(model="test", base_url="http://localhost:9999", timeout=5)
    with patch.object(client, "_ollama_post") as mock_post:
        mock_post.side_effect = ["{", "not valid json"]
        with pytest.raises(LLMJsonParseError):
            client.complete_json("test prompt")
    assert mock_post.call_count == 2


    """LLMJsonParseError is a subclass of LLMPermanentError for backward compat."""
    assert issubclass(LLMJsonParseError, LLMPermanentError)
    err = LLMJsonParseError("test", error_kind="json_parse", response_preview="abc")
    assert isinstance(err, LLMPermanentError)
    assert err.error_kind == "json_parse"
    assert err.response_preview == "abc"


def test_qwen35_397b_cloud_uses_known_context_size() -> None:
    """qwen3.5:397b-cloud uses full 256K context (262144) without /api/show call."""
    client = OllamaLLMClient(model="qwen3.5:397b-cloud", base_url="http://localhost:9999", timeout=5)
    assert client.get_max_context_tokens() == 262144


def test_qwen35_397b_uses_known_context_size() -> None:
    """qwen3.5:397b uses known context size 262144 without /api/show call."""
    client = OllamaLLMClient(model="qwen3.5:397b", base_url="http://localhost:9999", timeout=5)
    assert client.get_max_context_tokens() == 262144


# ---------------------------------------------------------------------------
# get_llm_for_agent tests
# ---------------------------------------------------------------------------


def test_get_llm_for_agent_dummy_provider_returns_dummy_client() -> None:
    """When SW_LLM_PROVIDER=dummy, get_llm_for_agent returns DummyLLMClient."""
    with patch.dict(os.environ, {"SW_LLM_PROVIDER": "dummy"}, clear=False):
        client = get_llm_for_agent("backend")
    assert isinstance(client, DummyLLMClient)


def test_get_llm_for_agent_per_agent_env_overrides() -> None:
    """SW_LLM_MODEL_<agent_key> overrides global and default."""
    _clear_client_cache_for_testing()
    with patch.dict(
        os.environ,
        {
            "SW_LLM_PROVIDER": "ollama",
            "SW_LLM_MODEL_backend": "custom-model",
            "SW_LLM_MODEL": "global-model",
        },
        clear=False,
    ):
        client = get_llm_for_agent("backend")
    assert isinstance(client, OllamaLLMClient)
    assert client.model == "custom-model"


def test_get_llm_for_agent_global_fallback() -> None:
    """When no per-agent env, SW_LLM_MODEL is used."""
    _clear_client_cache_for_testing()
    with patch.dict(
        os.environ,
        {"SW_LLM_PROVIDER": "ollama", "SW_LLM_MODEL": "qwen3.5:397b-cloud"},
        clear=False,
    ):
        client = get_llm_for_agent("backend")
    assert isinstance(client, OllamaLLMClient)
    assert client.model == "qwen3.5:397b-cloud"


def test_get_llm_for_agent_uses_default_when_no_env() -> None:
    """When no env overrides, agent default (e.g. qwen3.5:397b-cloud for backend) is used."""
    _clear_client_cache_for_testing()
    with patch.dict(
        os.environ,
        {
            "SW_LLM_PROVIDER": "ollama",
            "SW_LLM_MODEL": "",
            "SW_LLM_MODEL_backend": "",
        },
        clear=False,
    ):
        client = get_llm_for_agent("backend")
    assert isinstance(client, OllamaLLMClient)
    assert client.model == "qwen3.5:397b-cloud"


def test_get_llm_for_agent_cache_returns_same_instance() -> None:
    """Two calls for same agent with same config return the same cached client instance."""
    _clear_client_cache_for_testing()
    with patch.dict(
        os.environ,
        {"SW_LLM_PROVIDER": "ollama", "SW_LLM_MODEL_backend": "cached-model"},
        clear=False,
    ):
        client1 = get_llm_for_agent("backend")
        client2 = get_llm_for_agent("backend")
    assert client1 is client2


def test_extract_task_assignment_from_content_recovers_tasks() -> None:
    """When LLM returns raw content with embedded JSON, extract_task_assignment_from_content recovers it."""
    from software_engineering_team.shared.llm_response_utils import extract_task_assignment_from_content

    content = '''Here is the task plan:

```json
{
  "tasks": [
    {"id": "git-setup", "type": "git_setup", "title": "Setup", "assignee": "devops"},
    {"id": "backend-1", "type": "backend", "title": "API", "assignee": "backend"}
  ],
  "execution_order": ["git-setup", "backend-1"],
  "rationale": "Standard order"
}
```
'''
    result = extract_task_assignment_from_content(content)
    assert result is not None
    assert len(result.get("tasks", [])) == 2
    assert result["execution_order"] == ["git-setup", "backend-1"]


def test_extract_task_assignment_from_content_returns_none_for_empty() -> None:
    """extract_task_assignment_from_content returns None when no tasks in content."""
    from software_engineering_team.shared.llm_response_utils import extract_task_assignment_from_content

    assert extract_task_assignment_from_content("") is None
    assert extract_task_assignment_from_content("No JSON here") is None
    assert extract_task_assignment_from_content('{"other": "data"}') is None
