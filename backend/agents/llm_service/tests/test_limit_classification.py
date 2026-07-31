"""Tests for Ollama Cloud 429 body → limit_kind classification."""

from __future__ import annotations

import pytest

from llm_service.interface import OLLAMA_WEEKLY_LIMIT_MESSAGE
from llm_service.limit_classification import (
    LIMIT_KIND_RATE,
    LIMIT_KIND_SESSION,
    LIMIT_KIND_WEEKLY,
    classify_ollama_limit_kind,
    resolve_limit_kind,
)


@pytest.mark.parametrize(
    "body,expected",
    [
        (
            'you (user) have reached your session usage limit, upgrade for higher limits: https://ollama.com/upgrade',
            LIMIT_KIND_SESSION,
        ),
        (
            '{"error":"you (user) have reached your Session Usage Limit, upgrade"}',
            LIMIT_KIND_SESSION,
        ),
        (
            "YOU HAVE REACHED YOUR SESSION USAGE LIMIT",
            LIMIT_KIND_SESSION,
        ),
        (
            'you (user) have reached your weekly usage limit, upgrade for higher limits: https://ollama.com/upgrade',
            LIMIT_KIND_WEEKLY,
        ),
        (
            '{"error":"weekly usage limit exceeded"}',
            LIMIT_KIND_WEEKLY,
        ),
        ("rate limited", LIMIT_KIND_RATE),
        ("{}", LIMIT_KIND_RATE),
        ("", LIMIT_KIND_RATE),
        ('{"error": 123}', LIMIT_KIND_RATE),
        ("not json {broken", LIMIT_KIND_RATE),
    ],
)
def test_classify_ollama_limit_kind(body: str, expected: str) -> None:
    assert classify_ollama_limit_kind(body) == expected


def test_session_beats_weekly_if_both_present() -> None:
    """If both phrases appear, session wins (checked first)."""
    body = "session usage limit and weekly usage limit"
    assert classify_ollama_limit_kind(body) == LIMIT_KIND_SESSION


@pytest.mark.parametrize(
    "limit_kind,message,expected",
    [
        (LIMIT_KIND_SESSION, "ignored", LIMIT_KIND_SESSION),
        (LIMIT_KIND_WEEKLY, "ignored", LIMIT_KIND_WEEKLY),
        (LIMIT_KIND_RATE, "session usage limit", LIMIT_KIND_RATE),
        (None, OLLAMA_WEEKLY_LIMIT_MESSAGE, LIMIT_KIND_WEEKLY),
        (None, "reached your weekly usage limit", LIMIT_KIND_WEEKLY),
        (None, "reached your session usage limit", LIMIT_KIND_SESSION),
        ("bogus", "too many requests", LIMIT_KIND_RATE),
        (None, "too many requests", LIMIT_KIND_RATE),
    ],
)
def test_resolve_limit_kind(limit_kind, message, expected) -> None:
    assert (
        resolve_limit_kind(
            limit_kind=limit_kind,
            message=message,
            weekly_legacy_message=OLLAMA_WEEKLY_LIMIT_MESSAGE,
        )
        == expected
    )
