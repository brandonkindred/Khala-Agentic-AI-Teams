"""Regression guard: rendered brand spec must not leak PII from prior versions.

This test loads the brand spec template and the writing guidelines through their
normal loaders (using the bundled example author profile) and asserts that:
  1. No historical hardcoded PII strings appear in the rendered output.
  2. No unresolved Jinja2 placeholders remain.
  3. The rendered output reflects the example profile's name.

If anyone re-introduces personal info into the templates, this test fires.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from author_profile import EXAMPLE_PROFILE_PATH, AuthorProfile, load_author_profile
from shared.brand_spec import load_brand_spec_prompt
from shared.style_loader import load_style_file

_BLOGGING_ROOT = Path(__file__).resolve().parent.parent
_BRAND_SPEC = _BLOGGING_ROOT / "docs" / "brand_spec_prompt.md"
_WRITING_GUIDE = _BLOGGING_ROOT / "docs" / "writing_guidelines.md"

# Historical PII strings that must NEVER appear in any rendered prompt.
_BANNED = (
    "Brandon",
    "Kindred",
    "Look-see",
    "Look see",
    "Qanairy",
    "deepthought42",
    "brandonkindred",
)


@pytest.fixture(autouse=True)
def _force_example_profile(monkeypatch):
    monkeypatch.setenv("AUTHOR_PROFILE_PATH", str(EXAMPLE_PROFILE_PATH))
    from author_profile import loader as loader_mod

    loader_mod.clear_cache()
    yield
    loader_mod.clear_cache()


def _assert_clean(rendered: str, label: str) -> None:
    for needle in _BANNED:
        assert needle not in rendered, f"{label}: leaked PII string {needle!r}"
    assert "{{" not in rendered, f"{label}: unresolved Jinja2 placeholder"
    assert "{%" not in rendered, f"{label}: unresolved Jinja2 statement"


def test_brand_spec_renders_without_pii():
    rendered = load_brand_spec_prompt(_BRAND_SPEC)
    _assert_clean(rendered, "brand_spec_prompt.md")
    assert "Example Author" in rendered


def test_writing_guidelines_renders_without_pii():
    rendered = load_style_file(_WRITING_GUIDE, "writing style guide")
    _assert_clean(rendered, "writing_guidelines.md")
    assert "Example Author" in rendered


def test_profile_round_trips():
    profile = AuthorProfile.from_yaml_file(EXAMPLE_PROFILE_PATH)
    dumped = profile.model_dump()
    rebuilt = AuthorProfile.model_validate(dumped)
    assert rebuilt == profile


def test_load_author_profile_returns_example_when_pointed():
    profile = load_author_profile()
    assert profile.identity.full_name == "Example Author"
