"""Unit tests for agent_team_studio.agent_provisioning_team.shared.llm_client."""

from __future__ import annotations

from agent_team_studio.agent_provisioning_team.shared.llm_client import (
    LLMClient,
    LLMRequest,
    sanitize_prompt_var,
)


def test_sanitize_prompt_var_none_returns_empty_string() -> None:
    assert sanitize_prompt_var(None) == ""


def test_sanitize_prompt_var_strips_disallowed_characters() -> None:
    assert sanitize_prompt_var("a<b>c") == "abc"


def test_sanitize_prompt_var_coerces_non_string_input() -> None:
    assert sanitize_prompt_var(42) == "42"


def test_sanitize_prompt_var_within_max_len_is_unchanged() -> None:
    text = "x" * 100
    assert sanitize_prompt_var(text, max_len=200) == text


def test_sanitize_prompt_var_truncates_at_max_len() -> None:
    text = "x" * 300
    out = sanitize_prompt_var(text, max_len=200)
    assert len(out) == 200 + len("…[truncated]")
    assert out.startswith("x" * 200)
    assert out.endswith("…[truncated]")


def test_sanitize_prompt_var_default_max_len_is_100k() -> None:
    text = "x" * 100_001
    out = sanitize_prompt_var(text)
    assert len(out) == 100_000 + len("…[truncated]")


def test_llm_client_is_never_configured_yet() -> None:
    client = LLMClient()
    assert client.is_configured is False


def test_llm_client_complete_falls_back_and_labels_output() -> None:
    client = LLMClient()
    request = LLMRequest(system="s", user=" do the thing ")
    out = client.complete(request)
    assert out == "[llm-fallback] do the thing"


def test_llm_client_complete_logs_fallback_warning_once(monkeypatch, caplog) -> None:
    monkeypatch.setattr(LLMClient, "_warned_fallback", False)
    client = LLMClient()
    request = LLMRequest(system="s", user="u")

    with caplog.at_level("WARNING"):
        client.complete(request)
        client.complete(request)

    warnings = [r for r in caplog.records if "no LLM provider wired yet" in r.message]
    assert len(warnings) == 1
