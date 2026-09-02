"""Tests for is_web_search_configured (API precondition / health gating)."""

from __future__ import annotations

from agents.blogging.blog_research_agent.tools.web_search import is_web_search_configured


def test_web_search_configured_true_when_key_set(monkeypatch) -> None:
    """is_web_search_configured() returns True when OLLAMA_API_KEY is set to a non-blank value."""
    monkeypatch.setenv("OLLAMA_API_KEY", "some-key")
    assert is_web_search_configured() is True


def test_web_search_configured_false_when_key_unset(monkeypatch) -> None:
    """is_web_search_configured() returns False when OLLAMA_API_KEY is not set at all."""
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    assert is_web_search_configured() is False


def test_web_search_configured_false_when_key_blank(monkeypatch) -> None:
    """is_web_search_configured() returns False when OLLAMA_API_KEY is set to an empty string."""
    monkeypatch.setenv("OLLAMA_API_KEY", "")
    assert is_web_search_configured() is False


def test_web_search_configured_false_when_key_whitespace_only(monkeypatch) -> None:
    """is_web_search_configured() returns False when OLLAMA_API_KEY is set to whitespace only."""
    monkeypatch.setenv("OLLAMA_API_KEY", "   ")
    assert is_web_search_configured() is False
