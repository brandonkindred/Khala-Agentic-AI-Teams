"""
Unit tests for :class:`V2TeamConfig` — construction, immutability, the
``__post_init__`` invariant, and parity against each code-v2 team's real
``ToolAgentKind``/``PROFILE``/accessibility-clause values — independent of
any orchestrator consumption (none exists yet; see the class docstring).
"""

from __future__ import annotations

import dataclasses

import pytest

from software_engineering_team.shared.v2_team_config import V2TeamConfig

_CONFIG_KWARGS = dict(
    default_language="python",
    tool_agent_kinds=frozenset({"security", "testing_qa"}),
    extra_review_clause="",
    conventions_by_language={"_default": "PY"},
)


def test_construction_round_trips_all_fields():
    """Every constructor argument is readable back off the instance unchanged."""
    config = V2TeamConfig(**_CONFIG_KWARGS)
    assert config.default_language == "python"
    assert config.tool_agent_kinds == frozenset({"security", "testing_qa"})
    assert config.extra_review_clause == ""
    assert config.conventions_by_language == {"_default": "PY"}


def test_frozen_instance_rejects_attribute_assignment():
    """Frozen dataclass: assigning to a field raises instead of mutating."""
    config = V2TeamConfig(**_CONFIG_KWARGS)
    with pytest.raises(dataclasses.FrozenInstanceError):
        config.default_language = "java"


def test_missing_default_key_raises():
    """``conventions_by_language`` without a ``"_default"`` key is a construction error."""
    kwargs = dict(_CONFIG_KWARGS, conventions_by_language={"java": "JAVA"})
    with pytest.raises(ValueError, match="_default"):
        V2TeamConfig(**kwargs)


def test_empty_tool_agent_kinds_and_review_clause_construct_cleanly():
    """A team with no tool agents and no extra review clause is a valid config."""
    kwargs = dict(_CONFIG_KWARGS, tool_agent_kinds=frozenset(), extra_review_clause="")
    config = V2TeamConfig(**kwargs)
    assert config.tool_agent_kinds == frozenset()
    assert config.extra_review_clause == ""


def test_extra_review_clause_is_settable_to_non_empty_text():
    kwargs = dict(_CONFIG_KWARGS, extra_review_clause="Also verify accessibility.")
    config = V2TeamConfig(**kwargs)
    assert config.extra_review_clause == "Also verify accessibility."


class TestBackendParity:
    """Prove V2TeamConfig can faithfully hold backend_code_v2_team's real values."""

    def _build(self) -> V2TeamConfig:
        from software_engineering_team.backend_code_v2_team.models import ToolAgentKind
        from software_engineering_team.backend_code_v2_team.phases._profile import PROFILE

        return V2TeamConfig(
            default_language=PROFILE.default_language,
            tool_agent_kinds=frozenset(k.value for k in ToolAgentKind),
            extra_review_clause="",
            conventions_by_language=PROFILE.conventions_by_language,
        )

    def test_default_language_matches_profile(self):
        assert self._build().default_language == "python"

    def test_tool_agent_kinds_match_enum_members(self):
        from software_engineering_team.backend_code_v2_team.models import ToolAgentKind

        config = self._build()
        assert config.tool_agent_kinds == frozenset(k.value for k in ToolAgentKind)
        assert len(config.tool_agent_kinds) == 9
        assert "data_engineering" in config.tool_agent_kinds

    def test_conventions_by_language_matches_profile(self):
        from software_engineering_team.backend_code_v2_team.phases._profile import PROFILE

        assert self._build().conventions_by_language == PROFILE.conventions_by_language
        assert "java" in self._build().conventions_by_language
        assert "_default" in self._build().conventions_by_language

    def test_no_extra_review_clause(self):
        """Backend's code has no UI to check accessibility on."""
        assert self._build().extra_review_clause == ""


class TestFrontendParity:
    """Prove V2TeamConfig can faithfully hold frontend_code_v2_team's real values."""

    def _build(self) -> V2TeamConfig:
        from software_engineering_team.frontend_code_v2_team.models import ToolAgentKind
        from software_engineering_team.frontend_code_v2_team.phases import review as review_mod
        from software_engineering_team.frontend_code_v2_team.phases._profile import PROFILE

        return V2TeamConfig(
            default_language=PROFILE.default_language,
            tool_agent_kinds=frozenset(k.value for k in ToolAgentKind),
            extra_review_clause=review_mod._ACCESSIBILITY_VERIFY_NOTE,
            conventions_by_language=PROFILE.conventions_by_language,
        )

    def test_default_language_matches_profile(self):
        assert self._build().default_language == "typescript"

    def test_tool_agent_kinds_match_enum_members(self):
        from software_engineering_team.frontend_code_v2_team.models import ToolAgentKind

        config = self._build()
        assert config.tool_agent_kinds == frozenset(k.value for k in ToolAgentKind)
        assert len(config.tool_agent_kinds) == 16
        assert "accessibility" in config.tool_agent_kinds

    def test_conventions_by_language_matches_profile(self):
        from software_engineering_team.frontend_code_v2_team.phases._profile import PROFILE

        config = self._build()
        assert config.conventions_by_language == PROFILE.conventions_by_language
        assert set(config.conventions_by_language.keys()) == {"_default"}

    def test_extra_review_clause_is_accessibility_note(self):
        from software_engineering_team.frontend_code_v2_team.phases import review as review_mod

        config = self._build()
        assert config.extra_review_clause == review_mod._ACCESSIBILITY_VERIFY_NOTE
        assert config.extra_review_clause != ""
