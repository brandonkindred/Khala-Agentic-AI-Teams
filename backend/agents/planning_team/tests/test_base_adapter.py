"""Tests for the shared BaseAdapter (base-URL resolution and URL building)."""

import os
import sys
from pathlib import Path
from unittest.mock import patch

_agents_dir = Path(__file__).resolve().parent.parent.parent
if str(_agents_dir) not in sys.path:
    sys.path.insert(0, str(_agents_dir))


def _make_adapter():
    from planning_team.adapters._base import BaseAdapter

    return BaseAdapter(
        env_var="PLANNING_TEST_URL",
        path_prefix="/api/test",
        unconfigured_log="test service",
    )


def test_base_url_prefers_team_specific_env_var():
    adapter = _make_adapter()
    with patch.dict(
        os.environ,
        {"PLANNING_TEST_URL": "http://team-specific", "UNIFIED_API_BASE_URL": "http://fallback"},
    ):
        assert adapter.base_url() == "http://team-specific"


def test_base_url_falls_back_to_unified_api_base_url():
    adapter = _make_adapter()
    with patch.dict(os.environ, {"UNIFIED_API_BASE_URL": "http://fallback"}, clear=True):
        assert adapter.base_url() == "http://fallback"


def test_base_url_none_when_neither_set():
    adapter = _make_adapter()
    with patch.dict(os.environ, {}, clear=True):
        assert adapter.base_url() is None


def test_build_url_joins_base_and_path():
    adapter = _make_adapter()
    with patch.dict(os.environ, {"PLANNING_TEST_URL": "http://test"}, clear=True):
        assert adapter.build_url("/run") == "http://test/api/test/run"


def test_build_url_strips_trailing_slash_on_base():
    adapter = _make_adapter()
    with patch.dict(os.environ, {"PLANNING_TEST_URL": "http://test/"}, clear=True):
        assert adapter.build_url("/run") == "http://test/api/test/run"


def test_build_url_none_when_unconfigured():
    adapter = _make_adapter()
    with patch.dict(os.environ, {}, clear=True):
        assert adapter.build_url("/run") is None
