"""Unit tests for Slack user state management."""

import json
import os
import tempfile
from pathlib import Path
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


@_with_temp_cache
def test_read_all_returns_empty_dict_for_empty_file() -> None:
    path = slack_user_state._get_state_path()
    path.write_text("   ", encoding="utf-8")
    assert slack_user_state._read_all() == {}


@_with_temp_cache
def test_read_all_returns_empty_dict_and_warns_on_corrupt_json() -> None:
    path = slack_user_state._get_state_path()
    path.write_text("{not valid json", encoding="utf-8")
    with patch.object(slack_user_state.logger, "warning") as mock_warn:
        result = slack_user_state._read_all()
    assert result == {}
    mock_warn.assert_called_once()


@_with_temp_cache
def test_write_all_logs_and_cleans_up_tmp_file_on_oserror() -> None:
    with (
        patch.object(Path, "write_text", side_effect=OSError("disk full")),
        patch.object(Path, "exists", return_value=True),
        patch.object(Path, "unlink") as mock_unlink,
        patch.object(slack_user_state.logger, "warning") as mock_warn,
    ):
        slack_user_state._write_all({"U001": {"current_team": "blogging", "conversations": {}}})
    mock_warn.assert_called_once()
    mock_unlink.assert_called_once()


@_with_temp_cache
def test_get_user_fills_in_missing_current_team_key() -> None:
    path = slack_user_state._get_state_path()
    path.write_text(json.dumps({"U001": {"conversations": {}}}), encoding="utf-8")
    # set_conversation_id() routes through _get_user(), which must backfill the
    # missing "current_team" default rather than raising a KeyError.
    slack_user_state.set_conversation_id("U001", "blogging", "conv-1")
    assert slack_user_state.get_user_team("U001") == "personal_assistant"
    assert slack_user_state.get_conversation_id("U001", "blogging") == "conv-1"


@_with_temp_cache
def test_get_user_fills_in_missing_conversations_key() -> None:
    path = slack_user_state._get_state_path()
    path.write_text(json.dumps({"U001": {"current_team": "blogging"}}), encoding="utf-8")
    # _get_user() must backfill a missing "conversations" dict rather than raising.
    slack_user_state.set_conversation_id("U001", "blogging", "conv-1")
    assert slack_user_state.get_conversation_id("U001", "blogging") == "conv-1"
