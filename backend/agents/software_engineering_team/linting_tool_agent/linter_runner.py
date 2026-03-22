"""Linter detection, execution, and output parsing.

Non-LLM logic used by the LintingToolAgent during Planning and Execution phases.
"""

from __future__ import annotations

import logging
import re
import shutil
from pathlib import Path
from typing import List

from software_engineering_team.shared.command_runner import run_command

from .models import LintExecutionResult, LintIssue, LintPlan


def _is_command_available(cmd: str) -> bool:
    """Check if a command is available on the system PATH."""
    return shutil.which(cmd) is not None

logger = logging.getLogger(__name__)

# Regex for ruff / flake8 output: ``file.py:10:1: E501 Line too long``
_RUFF_FLAKE8_RE = re.compile(
    r"^(?P<file>[^\s:]+):(?P<line>\d+):(?P<col>\d+):\s+(?P<rule>\S+)\s+(?P<msg>.+)$"
)

# Regex for ng lint / eslint output: ``file.ts:10:1: error  rule  message``
# Also handles the common ``/path/file.ts(10,1): error TS123: msg`` pattern from tslint.
_NG_LINT_RE = re.compile(
    r"^(?P<file>[^\s:(]+)[:(](?P<line>\d+)[,:](?P<col>\d+)\)?[:\s]+(?P<sev>error|warning|info)?\s*(?P<rule>\S+)?\s+(?P<msg>.+)$"
)

# Simple eslint format: ``/path/file.ts  10:1  error  msg  rule``
_ESLINT_RE = re.compile(
    r"^\s+(?P<line>\d+):(?P<col>\d+)\s+(?P<sev>error|warning)\s+(?P<msg>.+?)\s{2,}(?P<rule>\S+)\s*$"
)


def _has_toml_section(path: Path, section: str) -> bool:
    """Check whether a TOML file contains a given ``[section]`` header."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        return f"[{section}]" in text
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Phase 1: Planning
# ---------------------------------------------------------------------------


def detect_linter(repo_path: Path, agent_type: str) -> LintPlan:
    """Inspect project root and return a plan describing which linter to run.

    Preconditions:
        - ``repo_path`` exists and is a directory.
    Postconditions:
        - Returns a ``LintPlan`` with a valid ``linter_command``.
    """
    if agent_type == "backend":
        return _detect_python_linter(repo_path)
    return _detect_frontend_linter(repo_path)


def _detect_python_linter(repo_path: Path) -> LintPlan:
    """Detect the Python linter for the project.
    
    Checks for config files first, then verifies the linter is installed.
    Falls back to available linters if the preferred one is not installed.
    """
    ruff_toml = repo_path / "ruff.toml"
    pyproject = repo_path / "pyproject.toml"
    flake8_cfg = repo_path / ".flake8"
    setup_cfg = repo_path / "setup.cfg"

    has_ruff = _is_command_available("ruff")
    has_flake8 = _is_command_available("flake8")

    # Check for ruff config
    if ruff_toml.exists() and has_ruff:
        return LintPlan(
            linter_name="ruff",
            linter_command=["ruff", "check", "."],
            config_file=str(ruff_toml.relative_to(repo_path)),
        )

    if pyproject.exists() and _has_toml_section(pyproject, "tool.ruff") and has_ruff:
        return LintPlan(
            linter_name="ruff",
            linter_command=["ruff", "check", "."],
            config_file="pyproject.toml",
        )

    # Check for flake8 config
    if flake8_cfg.exists() and has_flake8:
        return LintPlan(
            linter_name="flake8",
            linter_command=["flake8", "."],
            config_file=".flake8",
        )

    if setup_cfg.exists() and _has_toml_section(setup_cfg, "flake8") and has_flake8:
        return LintPlan(
            linter_name="flake8",
            linter_command=["flake8", "."],
            config_file="setup.cfg",
        )

    # No config found - use whichever linter is available
    if has_ruff:
        return LintPlan(
            linter_name="ruff",
            linter_command=["ruff", "check", "."],
        )

    if has_flake8:
        return LintPlan(
            linter_name="flake8",
            linter_command=["flake8", "."],
        )

    # No linter available - return a skip plan
    logger.warning("No Python linter available (ruff or flake8). Lint check will be skipped.")
    return LintPlan(
        linter_name="none",
        linter_command=[],
    )


def _detect_frontend_linter(repo_path: Path) -> LintPlan:
    """Detect the frontend linter for the project."""
    angular_json = repo_path / "angular.json"
    if angular_json.exists():
        return LintPlan(
            linter_name="ng_lint",
            linter_command=["npx", "ng", "lint"],
            config_file="angular.json",
        )

    for pattern in ("eslint.config.*", ".eslintrc*", ".eslintrc.json", ".eslintrc.js"):
        if list(repo_path.glob(pattern)):
            return LintPlan(
                linter_name="eslint",
                linter_command=["npx", "eslint", "."],
                config_file=str(next(repo_path.glob(pattern)).relative_to(repo_path)),
            )

    # Default for frontend: eslint
    return LintPlan(
        linter_name="eslint",
        linter_command=["npx", "eslint", "."],
    )


# ---------------------------------------------------------------------------
# Phase 2: Execution
# ---------------------------------------------------------------------------


def execute_linter(plan: LintPlan, repo_path: Path, agent_type: str) -> LintExecutionResult:
    """Run the linter subprocess and return parsed results.

    Preconditions:
        - ``plan`` is a valid ``LintPlan`` produced by ``detect_linter``.
        - ``repo_path`` exists and is a directory.
    Postconditions:
        - Returns ``LintExecutionResult`` with ``success=True`` when no violations.
    """
    # Handle skip case when no linter is available
    if plan.linter_name == "none" or not plan.linter_command:
        logger.info("No linter configured or available; skipping lint check.")
        return LintExecutionResult(success=True, raw_output="Lint check skipped: no linter available.")

    if agent_type == "frontend" and plan.linter_name == "ng_lint":
        try:
            from software_engineering_team.shared.command_runner import run_command_with_nvm
            cmd_result = run_command_with_nvm(plan.linter_command, cwd=repo_path)
        except ImportError:
            cmd_result = run_command(plan.linter_command, cwd=repo_path)
    else:
        cmd_result = run_command(plan.linter_command, cwd=repo_path, timeout=120)

    raw_output = cmd_result.output
    issues = parse_lint_output(raw_output, plan.linter_name)

    if cmd_result.success and not issues:
        return LintExecutionResult(success=True, raw_output=raw_output)

    return LintExecutionResult(
        success=False,
        issues=issues,
        raw_output=raw_output,
        issue_count=len(issues),
    )


def parse_lint_output(raw_output: str, linter_name: str) -> List[LintIssue]:
    """Parse linter stdout/stderr into structured ``LintIssue`` objects.

    Supports ruff, flake8, ng_lint, and eslint output formats.
    """
    if linter_name in ("ruff", "flake8"):
        return _parse_ruff_flake8(raw_output)
    if linter_name == "ng_lint":
        return _parse_ng_lint(raw_output)
    if linter_name == "eslint":
        return _parse_eslint(raw_output)
    return _parse_ruff_flake8(raw_output)


def _parse_ruff_flake8(raw: str) -> List[LintIssue]:
    issues: List[LintIssue] = []
    for line in raw.splitlines():
        m = _RUFF_FLAKE8_RE.match(line.strip())
        if m:
            rule = m.group("rule")
            severity = "error" if rule.startswith(("E", "F")) else "warning"
            issues.append(LintIssue(
                file_path=m.group("file"),
                line=int(m.group("line")),
                column=int(m.group("col")),
                rule=rule,
                message=m.group("msg").strip(),
                severity=severity,
            ))
    return issues


def _parse_ng_lint(raw: str) -> List[LintIssue]:
    issues: List[LintIssue] = []
    current_file = ""
    for line in raw.splitlines():
        stripped = line.strip()
        # ng lint often prints file path on its own line
        if stripped.endswith(".ts") or stripped.endswith(".html"):
            current_file = stripped
            continue
        m = _NG_LINT_RE.match(stripped)
        if m:
            issues.append(LintIssue(
                file_path=m.group("file") or current_file,
                line=int(m.group("line")),
                column=int(m.group("col")),
                rule=m.group("rule") or "",
                message=m.group("msg").strip(),
                severity=m.group("sev") or "warning",
            ))
            continue
        # Eslint-style lines within ng lint output
        m2 = _ESLINT_RE.match(line)
        if m2 and current_file:
            issues.append(LintIssue(
                file_path=current_file,
                line=int(m2.group("line")),
                column=int(m2.group("col")),
                rule=m2.group("rule") or "",
                message=m2.group("msg").strip(),
                severity=m2.group("sev") or "warning",
            ))
    return issues


def _parse_eslint(raw: str) -> List[LintIssue]:
    issues: List[LintIssue] = []
    current_file = ""
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("✖", "✓", "warning", "error")):
            continue
        # ESLint prints the absolute file path as a header line (no leading whitespace)
        if not line.startswith(" ") and not line.startswith("\t") and ("/" in stripped or "\\" in stripped):
            current_file = stripped
            continue
        m = _ESLINT_RE.match(line)
        if m and current_file:
            issues.append(LintIssue(
                file_path=current_file,
                line=int(m.group("line")),
                column=int(m.group("col")),
                rule=m.group("rule") or "",
                message=m.group("msg").strip(),
                severity=m.group("sev") or "warning",
            ))
    return issues
