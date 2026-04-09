"""Render Jinja2 templates against an :class:`AuthorProfile`.

Uses ``StrictUndefined`` so any reference to a missing field raises immediately
rather than producing silently broken prompts. Templates address the profile
via the top-level ``author`` variable, e.g. ``{{ author.identity.full_name }}``.
"""

from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, StrictUndefined

from .model import AuthorProfile

_env = Environment(
    undefined=StrictUndefined,
    autoescape=False,
    keep_trailing_newline=True,
    trim_blocks=False,
    lstrip_blocks=False,
)


def render_template(template_str: str, profile: AuthorProfile) -> str:
    """Render a Jinja2 template string against ``profile`` (exposed as ``author``)."""
    template = _env.from_string(template_str)
    return template.render(author=profile)


def render_template_file(path: Path | str, profile: AuthorProfile) -> str:
    """Read and render a template file from disk."""
    text = Path(path).read_text(encoding="utf-8")
    return render_template(text, profile)
