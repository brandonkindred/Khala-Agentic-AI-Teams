"""Shared line-oriented text utilities for best-effort config probing.

Kept out of the team orchestrators so the orchestrator modules stay focused on
workflow orchestration rather than carrying general-purpose text helpers.
"""

from __future__ import annotations

# A real TOML parser when the runtime offers one: stdlib ``tomllib`` on
# Python 3.11+, else the ``tomli`` backport if it is installed. ``None`` on
# Python 3.10 without ``tomli`` — callers then fall back to the line-anchored
# text scan. No hard dependency is added: the 3.11+ stdlib covers the real
# runtime, and 3.10 simply keeps the prior best-effort text probe.
try:
    import tomllib as _toml  # Python 3.11+ stdlib
except ModuleNotFoundError:  # pragma: no cover - 3.10 fallback path
    try:
        import tomli as _toml  # optional backport
    except ModuleNotFoundError:  # pragma: no cover - 3.10 without tomli
        _toml = None  # type: ignore[assignment]


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


def toml_has_section(text: str, header: str) -> bool:
    """True if a TOML table whose header line is ``header`` is really defined.

    ``header`` is the section-header probe the line-anchored scan would use:
    ``"[tool.ruff]"`` for an exact table, or ``"[tool.pytest"`` as a prefix
    covering ``[tool.pytest.ini_options]``.

    Uses a real TOML parser when one is available (stdlib ``tomllib`` on
    Python 3.11+, the ``tomli`` backport if installed) so a header appearing
    inside a *multi-line* string value — the one residual ``has_section_header``
    cannot reject — no longer produces a false positive. Falls back to
    ``has_section_header(text, header)`` when no parser is present (Python 3.10
    without ``tomli``) or the text is not valid TOML, preserving the prior
    best-effort text-scan behaviour. No hard dependency is added.

    Preconditions: ``text`` is a ``str`` (may be empty or invalid TOML);
      ``header`` is a non-empty ``str`` beginning with ``[``.
    Postconditions: returns a ``bool``; never raises — a TOML decode error
      falls back to the line-anchored text scan rather than propagating.
    """
    if _toml is None:
        return has_section_header(text, header)
    dotted = header[1:].rstrip("]")
    try:
        doc = _toml.loads(text)
    except _toml.TOMLDecodeError:
        return has_section_header(text, header)
    node = doc
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return False
        node = node[part]
    return True
