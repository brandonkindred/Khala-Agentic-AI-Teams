"""
Linter tool agent for frontend-code-v2: real ESLint-driven implementation.

Runs ESLint (JSON output) over the repo in ``review``, emits one
``ReviewIssue`` per reported message, and fixes them one at a time via
``problem_solve`` — mirroring the shared build-specialist tool agent's shape
(:mod:`software_engineering_team.shared.tool_agent_build_specialist`): a lint
violation, like a build failure, is not a subjective opinion about someone
else's code, so this agent owns fixing what it finds (opted in via
:class:`~software_engineering_team.shared.tool_agent_base.SingleIssueProblemSolveMixin`).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import List

from strands import Agent  # noqa: F401  (kept so tests can monkeypatch this module's Agent)

from software_engineering_team.codegen_team.stacks.frontend.profile import (
    parse_problem_solving_single_issue_template,
)
from software_engineering_team.codegen_team.stacks.frontend.prompts import (
    PROBLEM_SOLVING_SINGLE_ISSUE_PROMPT,
)
from software_engineering_team.shared.tool_agent_base import (
    BaseReviewToolAgent,
    SingleIssueProblemSolveMixin,
)
from software_engineering_team.shared.v2_models import ReviewIssue

logger = logging.getLogger(__name__)

MAX_RELEVANT_CODE_CHARS = 8_000

# ESLint's own severity levels (1 = warn, 2 = error) mapped into this team's
# ReviewIssue taxonomy. Unknown/missing severities default to "medium".
_ESLINT_SEVERITY = {1: "low", 2: "high"}


def run_frontend_lint_and_parse(repo_path: Path) -> List[ReviewIssue]:
    """Run ``eslint . --format json`` and return one issue per reported message.

    Preconditions: ``repo_path`` is a ``pathlib.Path`` to an existing directory.
    Postconditions: returns a list of :class:`ReviewIssue` (empty when no
    frontend project is present, ESLint is unavailable, or there are no
    findings). Never raises for a missing project or unparsable output.
    """
    frontend_dir = repo_path if (repo_path / "package.json").exists() else repo_path / "frontend"
    if not (frontend_dir / "package.json").exists():
        logger.info("Linter: no frontend project at %s", repo_path)
        return []
    try:
        from shared.command_runner.nvm import run_command_with_nvm
    except ImportError:  # pragma: no cover  # defensive: command_runner is always importable here
        logger.warning("Linter: shared.command_runner not available")
        return []

    result = run_command_with_nvm(["npx", "eslint", ".", "--format", "json"], cwd=frontend_dir)
    raw = (result.stdout or "").strip()
    if not raw:
        return []
    try:
        report = json.loads(raw)
    except (ValueError, TypeError) as exc:
        logger.warning("Linter: could not parse ESLint JSON output: %s", exc)
        return []
    if not isinstance(report, list):
        return []

    issues: List[ReviewIssue] = []
    for file_entry in report:
        if not isinstance(file_entry, dict):
            continue
        file_path = file_entry.get("filePath") or ""
        try:
            rel_path = str(Path(file_path).relative_to(frontend_dir))
        except ValueError:
            rel_path = file_path
        for message in file_entry.get("messages") or []:
            if not isinstance(message, dict):
                continue
            rule_id = message.get("ruleId") or "eslint"
            text = message.get("message") or "Lint violation."
            line = message.get("line")
            location = f" (line {line})" if line else ""
            issues.append(
                ReviewIssue(
                    source="linter",
                    severity=_ESLINT_SEVERITY.get(message.get("severity"), "medium"),
                    description=f"[{rule_id}] {text}{location}",
                    file_path=rel_path,
                    recommendation="Fix the lint violation.",
                )
            )
    return issues


class LinterToolAgent(SingleIssueProblemSolveMixin, BaseReviewToolAgent):
    """Identifies all lint violations in review and fixes them one at a time.

    ``review`` runs ESLint via the shared ``build_runner`` extensibility point
    (the same one the build-specialist tool agent uses for the project build);
    ``problem_solve`` and the other lifecycle methods come from
    :class:`SingleIssueProblemSolveMixin`/:class:`BaseReviewToolAgent`.
    """

    name = "Linter"
    empty_label = "lint issues"
    issue_source = "linter"
    problem_solve_sources = ("linter",)
    max_relevant_code_chars = MAX_RELEVANT_CODE_CHARS
    default_severity = "medium"
    default_recommendation = "Fix the lint violation."
    plan_recommendations = ["Include lint rules and formatting conventions in the plan."]
    plan_summary = "Linter planning."
    build_runner = staticmethod(run_frontend_lint_and_parse)
    build_review_noun = "lint issue(s)"
    problem_solving_prompt = PROBLEM_SOLVING_SINGLE_ISSUE_PROMPT
    _parse_single_issue = staticmethod(parse_problem_solving_single_issue_template)
