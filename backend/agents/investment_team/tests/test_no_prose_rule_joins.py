"""Regression guard for issue #553.

After the #537 structured-`StrategySpec` DSL migration landed in PR #563
(commit ``f382a66``), every agent-prompt formatter renders rule lists via
``format_rules_for_prompt(...)`` / ``format_sizing_rule(...)`` from
``backend/agents/investment_team/strategy_lab/spec_dsl.py``. Reintroducing
``", ".join(spec.entry_rules)`` (or ``strategy.entry_rules``, or the ``exit``
or ``sizing`` variants) would silently call ``str()`` on Pydantic model
instances and produce garbage prompts. This test asserts no such call exists
in the team's product source.
"""

from __future__ import annotations

import re
from pathlib import Path

_INVESTMENT_ROOT = Path(__file__).resolve().parent.parent

_FORBIDDEN_RE = re.compile(r"\.join\(\s*(?:spec|strategy)\.(?:entry|exit|sizing)_rules\b")

_EXCLUDE_DIRS = {"tests", "__pycache__"}
_EXCLUDE_FILES = {"spec_dsl.py"}  # docstring documents the anti-pattern


def _iter_source_files() -> list[Path]:
    return [
        p
        for p in _INVESTMENT_ROOT.rglob("*.py")
        if not (set(p.relative_to(_INVESTMENT_ROOT).parts) & _EXCLUDE_DIRS)
        and p.name not in _EXCLUDE_FILES
    ]


def test_no_prose_rule_joins_in_source() -> None:
    offenders: list[str] = []
    for path in _iter_source_files():
        for lineno, line in enumerate(path.read_text().splitlines(), start=1):
            if _FORBIDDEN_RE.search(line):
                offenders.append(f"{path.relative_to(_INVESTMENT_ROOT)}:{lineno}: {line.strip()}")

    assert not offenders, (
        "Prose-join of structured rule lists reintroduced (issue #553 regression). "
        "Use format_rules_for_prompt(...) / format_sizing_rule(...) from "
        "investment_team.strategy_lab.spec_dsl instead.\n  " + "\n  ".join(offenders)
    )


def test_guard_regex_matches_known_violations() -> None:
    violations = [
        '", ".join(spec.entry_rules)',
        '"; ".join(spec.exit_rules)',
        ", ".join(['"; "']) + ".join(spec.sizing_rules)",
        '", ".join(strategy.entry_rules)',
        '"; ".join(strategy.exit_rules)',
    ]
    for v in violations:
        assert _FORBIDDEN_RE.search(v), f"guard regex failed to match: {v!r}"


def test_guard_regex_ignores_helper_usage() -> None:
    safe_lines = [
        "entry_rules=format_rules_for_prompt(spec.entry_rules)",
        'exit_rules=format_rules_for_prompt(strategy.exit_rules, separator="; ")',
        "sizing_rules=format_sizing_rule(spec.sizing)",
        "rules = spec.entry_rules + spec.exit_rules",
    ]
    for line in safe_lines:
        assert not _FORBIDDEN_RE.search(line), f"guard regex false-positive on: {line!r}"
