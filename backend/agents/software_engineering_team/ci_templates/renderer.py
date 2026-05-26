"""Render CI workflow templates from Jinja2 to valid GitHub Actions YAML.

Preconditions:
    Template files exist under the ``templates/`` subdirectory.
Postconditions:
    Returned string is valid YAML parseable by ``yaml.safe_load``.
Invariants:
    Rendering is deterministic — same params always produce the same output.
"""

from __future__ import annotations

import logging
from pathlib import Path

import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined

from .models import BackendCIParams, FrontendCIParams

logger = logging.getLogger(__name__)

_TEMPLATES_DIR = Path(__file__).parent / "templates"


def _yaml_escape(value: str) -> str:
    """Escape a string for use inside a YAML double-quoted scalar."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(_TEMPLATES_DIR)),
        undefined=StrictUndefined,
        keep_trailing_newline=True,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["yaml_escape"] = _yaml_escape
    return env


def render_backend_ci(params: BackendCIParams | None = None) -> str:
    """Render the backend CI workflow.

    Preconditions:
        ``params`` is a valid ``BackendCIParams`` (or None for defaults).
    Postconditions:
        Return value is valid YAML containing at least ``lint`` and ``test`` jobs.
    """
    params = params or BackendCIParams()
    template = _env().get_template("backend_ci.yml.j2")
    rendered = template.render(**params.model_dump())
    yaml.safe_load(rendered)
    return rendered


def render_frontend_ci(params: FrontendCIParams | None = None) -> str:
    """Render the frontend CI workflow.

    Preconditions:
        ``params`` is a valid ``FrontendCIParams`` (or None for defaults).
    Postconditions:
        Return value is valid YAML containing at least ``lint`` and ``test`` jobs.
    """
    params = params or FrontendCIParams()
    template = _env().get_template("frontend_ci.yml.j2")
    rendered = template.render(**params.model_dump())
    yaml.safe_load(rendered)
    return rendered
