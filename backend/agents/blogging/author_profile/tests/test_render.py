"""Tests for the Jinja2 template renderer."""

from __future__ import annotations

import pytest
from jinja2.exceptions import UndefinedError

from author_profile import AuthorProfile, Identity, Voice, render_template


def _profile() -> AuthorProfile:
    return AuthorProfile(
        identity=Identity(full_name="Ada Lovelace", short_name="Ada"),
        voice=Voice(tone_words=["precise", "curious"]),
    )


def test_renders_simple_field():
    out = render_template("Hello {{ author.identity.full_name }}", _profile())
    assert out == "Hello Ada Lovelace"


def test_renders_loop():
    out = render_template(
        "{% for w in author.voice.tone_words %}{{ w }},{% endfor %}",
        _profile(),
    )
    assert out == "precise,curious,"


def test_strict_undefined_raises_on_missing_field():
    with pytest.raises(UndefinedError):
        render_template("{{ author.identity.nope }}", _profile())
