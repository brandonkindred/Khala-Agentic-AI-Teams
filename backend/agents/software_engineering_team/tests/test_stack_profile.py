"""
Unit tests for :class:`StackProfile` itself — construction, immutability, and
the ``__post_init__`` invariant — independent of the phase implementations
that consume a profile (see ``test_v2_shared_phases.py`` for those, and for
the ``conventions_for`` lookup tests).
"""

from __future__ import annotations

import dataclasses

import pytest

from software_engineering_team.shared.stack_profile import StackProfile

_PROFILE_KWARGS = dict(
    name="backend",
    default_language="python",
    planning_language_label="Language",
    planning_progress_label="language",
    conventions_by_language={"_default": "PY"},
    has_language_conventions=True,
    build_verify_label="backend_code_v2",
    detect_language=lambda _p, _t: "python",
    repo_extensions=frozenset({".py"}),
    repo_exclude_dirs=frozenset({".git"}),
    repo_max_chars=1000,
    detect_tooling=lambda _p: (True, True),
)


def test_construction_round_trips_all_fields():
    """Every constructor argument is readable back off the instance unchanged."""
    profile = StackProfile(**_PROFILE_KWARGS)
    assert profile.name == "backend"
    assert profile.default_language == "python"
    assert profile.planning_language_label == "Language"
    assert profile.planning_progress_label == "language"
    assert profile.conventions_by_language == {"_default": "PY"}
    assert profile.has_language_conventions is True
    assert profile.build_verify_label == "backend_code_v2"
    assert profile.detect_language(None, None) == "python"
    assert profile.repo_extensions == frozenset({".py"})
    assert profile.repo_exclude_dirs == frozenset({".git"})
    assert profile.repo_max_chars == 1000
    assert profile.detect_tooling(None) == (True, True)


def test_frozen_instance_rejects_attribute_assignment():
    """Frozen dataclass: assigning to a field raises instead of mutating."""
    profile = StackProfile(**_PROFILE_KWARGS)
    with pytest.raises(dataclasses.FrozenInstanceError):
        profile.build_verify_label = "other"


def test_missing_default_key_raises():
    """``conventions_by_language`` without a ``"_default"`` key is a construction error."""
    kwargs = dict(_PROFILE_KWARGS, conventions_by_language={"java": "JAVA"})
    with pytest.raises(ValueError, match="_default"):
        StackProfile(**kwargs)
