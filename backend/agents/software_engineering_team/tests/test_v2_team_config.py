"""
Unit tests for :class:`V2TeamConfig` — construction, immutability, and
parity against each code-v2 team's real ``ToolAgentKind``/``PROFILE``/
accessibility-clause values, at the dataclass level rather than through the
orchestrator (orchestrator-level property/consumption tests live in
``test_v2_config_orchestrator.py``). ``StackProfile``'s own
construction/invariant/frozen behavior is covered in full by
``test_stack_profile.py``; this module includes one targeted test showing
that the ``"_default"`` invariant is enforced by ``StackProfile`` before
``V2TeamConfig`` ever sees it — ``V2TeamConfig`` composes ``StackProfile``
rather than duplicating its fields or its invariant.
"""

from __future__ import annotations

import dataclasses

import pytest

from software_engineering_team.shared.v2_team_config import V2TeamConfig

from ._v2_config_fixtures import make_stack_profile as _make_stack_profile

_CONFIG_KWARGS = dict(
    stack_profile=_make_stack_profile(),
    tool_agent_kinds=frozenset({"security", "testing_qa"}),
    extra_review_clause="",
    output_template_path_prefixes=("backend/", "./backend/"),
    output_template_allowed_languages=("python", "java"),
    output_template_coerce_unknown=True,
)


def test_construction_round_trips_all_fields():
    """Every constructor argument is readable back off the instance unchanged."""
    config = V2TeamConfig(**_CONFIG_KWARGS)
    assert config.stack_profile is _CONFIG_KWARGS["stack_profile"]
    assert config.stack_profile.default_language == "python"
    assert config.stack_profile.conventions_by_language == {"_default": "PY"}
    assert config.tool_agent_kinds == frozenset({"security", "testing_qa"})
    assert config.extra_review_clause == ""
    assert config.output_template_path_prefixes == ("backend/", "./backend/")
    assert config.output_template_allowed_languages == ("python", "java")
    assert config.output_template_coerce_unknown is True


def test_frozen_instance_rejects_attribute_assignment():
    """Frozen dataclass: assigning to a field raises instead of mutating."""
    config = V2TeamConfig(**_CONFIG_KWARGS)
    with pytest.raises(dataclasses.FrozenInstanceError):
        config.extra_review_clause = "changed"


def test_composed_stack_profile_carries_its_own_default_key_invariant():
    """``V2TeamConfig`` enforces no invariant of its own; a ``StackProfile``
    missing ``"_default"`` fails when *it* is constructed, before
    ``V2TeamConfig`` ever sees it."""
    with pytest.raises(ValueError, match="_default"):
        _make_stack_profile(conventions_by_language={"java": "JAVA"})


def test_empty_tool_agent_kinds_and_review_clause_construct_cleanly():
    """A team with no tool agents and no extra review clause is a valid config."""
    kwargs = dict(_CONFIG_KWARGS, tool_agent_kinds=frozenset(), extra_review_clause="")
    config = V2TeamConfig(**kwargs)
    assert config.tool_agent_kinds == frozenset()
    assert config.extra_review_clause == ""


def test_extra_review_clause_preserves_non_empty_text():
    """A non-empty extra review clause is preserved unchanged on the instance."""
    kwargs = dict(_CONFIG_KWARGS, extra_review_clause="Also verify accessibility.")
    config = V2TeamConfig(**kwargs)
    assert config.extra_review_clause == "Also verify accessibility."


class TestBackendParity:
    """Prove V2TeamConfig can faithfully hold backend_code_v2_team's real values."""

    def _build(self) -> V2TeamConfig:
        """Compose the team's real, already-constructed PROFILE — not a copy of its fields."""
        from software_engineering_team.codegen_team.models import ToolAgentKind
        from software_engineering_team.codegen_team.stacks.backend.profile import PROFILE

        return V2TeamConfig(
            stack_profile=PROFILE,
            tool_agent_kinds=frozenset(k.value for k in ToolAgentKind),
            extra_review_clause="",
            output_template_path_prefixes=("backend/", "./backend/"),
            output_template_allowed_languages=("python", "java"),
            output_template_coerce_unknown=True,
        )

    def test_stack_profile_is_the_real_team_profile(self):
        """Composition, not copying: the same PROFILE object, not an equal one."""
        from software_engineering_team.codegen_team.stacks.backend.profile import PROFILE

        assert self._build().stack_profile is PROFILE

    def test_default_language_matches_profile(self):
        """Backend's default language is python."""
        assert self._build().stack_profile.default_language == "python"

    def test_tool_agent_kinds_match_enum_members(self):
        """The tool-agent registry mirrors every ToolAgentKind member backend defines."""
        from software_engineering_team.codegen_team.models import ToolAgentKind

        config = self._build()
        assert config.tool_agent_kinds == frozenset(k.value for k in ToolAgentKind)
        assert len(config.tool_agent_kinds) == 9
        assert "data_engineering" in config.tool_agent_kinds

    def test_conventions_by_language_matches_profile(self):
        """Conventions come from the composed PROFILE, including java + _default."""
        config = self._build()
        assert "java" in config.stack_profile.conventions_by_language
        assert "_default" in config.stack_profile.conventions_by_language

    def test_no_extra_review_clause(self):
        """Backend's code has no UI to check accessibility on."""
        assert self._build().extra_review_clause == ""


class TestFrontendParity:
    """Prove V2TeamConfig can faithfully hold frontend_code_v2_team's real values."""

    def _build(self) -> V2TeamConfig:
        """Compose the team's real, already-constructed PROFILE — not a copy of its fields."""
        from software_engineering_team.codegen_team.models import ToolAgentKind
        from software_engineering_team.codegen_team.stacks.frontend.profile import (
            _ACCESSIBILITY_VERIFY_NOTE,
            PROFILE,
        )

        return V2TeamConfig(
            stack_profile=PROFILE,
            tool_agent_kinds=frozenset(k.value for k in ToolAgentKind),
            extra_review_clause=_ACCESSIBILITY_VERIFY_NOTE,
            output_template_path_prefixes=("frontend/", "./frontend/"),
            output_template_allowed_languages=(
                "angular",
                "react",
                "vue",
                "typescript",
                "javascript",
            ),
            output_template_coerce_unknown=False,
        )

    def test_stack_profile_is_the_real_team_profile(self):
        """Composition, not copying: the same PROFILE object, not an equal one."""
        from software_engineering_team.codegen_team.stacks.frontend.profile import PROFILE

        assert self._build().stack_profile is PROFILE

    def test_default_language_matches_profile(self):
        """Frontend's default language is typescript."""
        assert self._build().stack_profile.default_language == "typescript"

    def test_tool_agent_kinds_match_enum_members(self):
        """The tool-agent registry mirrors every ToolAgentKind member frontend defines."""
        from software_engineering_team.codegen_team.models import ToolAgentKind

        config = self._build()
        assert config.tool_agent_kinds == frozenset(k.value for k in ToolAgentKind)
        assert len(config.tool_agent_kinds) == 16
        assert "accessibility" in config.tool_agent_kinds

    def test_conventions_by_language_matches_profile(self):
        """Frontend's conventions map has exactly one key: _default."""
        config = self._build()
        assert set(config.stack_profile.conventions_by_language.keys()) == {"_default"}

    def test_extra_review_clause_is_accessibility_note(self):
        """Frontend's extra review clause is the real accessibility-verification note."""
        from software_engineering_team.codegen_team.stacks.frontend.profile import (
            _ACCESSIBILITY_VERIFY_NOTE,
        )

        config = self._build()
        assert config.extra_review_clause == _ACCESSIBILITY_VERIFY_NOTE
        assert config.extra_review_clause != ""
