"""Unit tests for :mod:`agent_team_studio.assistant_kernel.envelope`."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from agent_team_studio.assistant_kernel.envelope import ConversationMessage


def test_accepts_user_role() -> None:
    msg = ConversationMessage(role="user", content="hello")
    assert msg.role == "user"
    assert msg.content == "hello"
    assert msg.timestamp is None


def test_accepts_assistant_role() -> None:
    msg = ConversationMessage(role="assistant", content="hi there")
    assert msg.role == "assistant"


def test_timestamp_defaults_to_none() -> None:
    assert ConversationMessage(role="user", content="x").timestamp is None


def test_timestamp_may_be_supplied() -> None:
    msg = ConversationMessage(role="user", content="x", timestamp="2026-08-09T00:00:00Z")
    assert msg.timestamp == "2026-08-09T00:00:00Z"


def test_rejects_invalid_role() -> None:
    with pytest.raises(ValidationError):
        ConversationMessage(role="system", content="x")


def test_frozen_rejects_mutation() -> None:
    msg = ConversationMessage(role="user", content="x")
    with pytest.raises(ValidationError):
        msg.content = "y"
