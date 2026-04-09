"""Shared author profile: typed, runtime-injectable user identity for agent prompts.

Public API:
    AuthorProfile         — Pydantic v2 model (with nested sub-models).
    load_author_profile() — Resolve + cache a profile from env / AGENT_CACHE / example.
    render_template()     — Render a Jinja2 template string against a profile.
"""

from .loader import EXAMPLE_PROFILE_PATH, load_author_profile
from .model import (
    AuthorProfile,
    Background,
    Identity,
    Professional,
    Social,
    Voice,
)
from .render import render_template, render_template_file

__all__ = [
    "AuthorProfile",
    "Background",
    "EXAMPLE_PROFILE_PATH",
    "Identity",
    "Professional",
    "Social",
    "Voice",
    "load_author_profile",
    "render_template",
    "render_template_file",
]
