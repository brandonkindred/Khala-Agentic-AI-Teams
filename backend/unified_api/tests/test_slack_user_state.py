"""Unit tests for Slack user state management."""

import os
import tempfile
from unittest.mock import patch

from unified_api import slack_user_state


def _with_temp_cache(fn):
    """Run test with a temporary AGENT_CACHE directory."""

    def wrapper():
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(os.environ, {"AGENT_CACHE": tmpdir}):
            # Reset internal lock state is fine — threading.Lock is reentrant-safe
            fn()

    return wrapper


@_with_temp_cache
def test_get_user_team_returns_default() -> None:
    assert slack_user_state.get_user_team("U999") == "personal_assistant"


@_with_temp_cache
def test_set_and_get_user_team() -> None:
    slack_user_state.set_user_team("U001", "blogging")
    assert slack_user_state.get_user_team("U001") == "blogging"


@_with_temp_cache
def test_get_conversation_id_returns_none_initially() -> None:
    assert slack_user_state.get_conversation_id("U001", "blogging") is None


@_with_temp_cache
def test_set_and_get_conversation_id() -> None:
    slack_user_state.set_conversation_id("U001", "blogging", "conv-abc")
    assert slack_user_state.get_conversation_id("U001", "blogging") == "conv-abc"


@_with_temp_cache
def test_reset_conversation_clears_id() -> None:
    slack_user_state.set_conversation_id("U001", "blogging", "conv-abc")
    slack_user_state.reset_conversation("U001", "blogging")
    assert slack_user_state.get_conversation_id("U001", "blogging") is None


@_with_temp_cache
def test_multiple_users_independent() -> None:
    slack_user_state.set_user_team("U001", "blogging")
    slack_user_state.set_user_team("U002", "sales_team")
    assert slack_user_state.get_user_team("U001") == "blogging"
    assert slack_user_state.get_user_team("U002") == "sales_team"


@_with_temp_cache
def test_multiple_teams_per_user() -> None:
    slack_user_state.set_conversation_id("U001", "blogging", "conv-1")
    slack_user_state.set_conversation_id("U001", "sales_team", "conv-2")
    assert slack_user_state.get_conversation_id("U001", "blogging") == "conv-1"
    assert slack_user_state.get_conversation_id("U001", "sales_team") == "conv-2"


@_with_temp_cache
def test_reset_only_affects_target_team() -> None:
    slack_user_state.set_conversation_id("U001", "blogging", "conv-1")
    slack_user_state.set_conversation_id("U001", "sales_team", "conv-2")
    slack_user_state.reset_conversation("U001", "blogging")
    assert slack_user_state.get_conversation_id("U001", "blogging") is None
    assert slack_user_state.get_conversation_id("U001", "sales_team") == "conv-2"
