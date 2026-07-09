"""Shared line-oriented text utilities for best-effort config probing.

Kept out of the team orchestrators so the orchestrator modules stay focused on
workflow orchestration rather than carrying general-purpose text helpers.
"""

from __future__ import annotations


def has_section_header(text: str, header: str) -> bool:
    """True if ``header`` begins an uncommented line in ``text``.

    Each line is stripped of leading whitespace; comment lines (first non-blank
    char ``#``) and blank lines are skipped, so a literal ``[tool.ruff]`` (or
    ``[tool.pytest`` / ``[flake8]``) sitting inside a commented-out block no
    longer matches. ``header`` is matched as a leading prefix, so
    ``"[tool.pytest"`` covers ``[tool.pytest.ini_options]``.

    Preconditions: ``text`` is a ``str`` (may be empty); ``header`` is a
      non-empty ``str`` beginning with ``[``.
    Postconditions: returns a ``bool``; never raises (reads only ``text``).
      Hardens config-file section probes against commented-out config (and a
      header embedded mid-line in an inline value string) without pulling in a
      TOML/INI parser. The residual false positive is a header that begins a
      line inside a *multi-line* string value — contrived for these section
      headers, and the real build/lint gate catches it downstream.
    """
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith(header):
            return True
    return False
