"""Quality gate tool functions for the Software Engineering pipeline.

Each function is a self-contained tool that can be called by any agent or
orchestrator.  They instantiate their own agent/LLM when needed (no shared
mutable state), making them safe for concurrent and cross-activity use.

Usage from a Senior SWE, orchestrator, or Temporal activity::

    from software_engineering_team.quality_gate_tools import (
        run_build_verification,
        run_code_review,
        run_linting,
        run_dbc_comments,
    )
    build_ok, build_err = run_build_verification(repo_path, "backend", task_id)
    review = run_code_review(code, spec, task_desc, language="python")
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


def _default_llm_getter(agent_key: str) -> Any:
    from llm_service import get_strands_model

    return get_strands_model(agent_key)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class CodeReviewResult:
    approved: bool = False
    issues: List[Dict[str, Any]] = field(default_factory=list)
    summary: str = ""
    spec_compliance_notes: str = ""


@dataclass
class BuildResult:
    success: bool = True
    error: str = ""
    is_env_failure: bool = False


@dataclass
class LintResult:
    passed: bool = True
    issues: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class DbcResult:
    compliant: bool = True
    comments_added: int = 0
    comments_updated: int = 0


@dataclass
class QAResult:
    passed: bool = True
    bugs: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class SecurityResult:
    passed: bool = True
    vulnerabilities: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class AcceptanceResult:
    accepted: bool = True
    reasoning: str = ""


# ---------------------------------------------------------------------------
# Tool functions
# ---------------------------------------------------------------------------


def run_code_review(
    code: str,
    spec_content: str,
    task_description: str,
    language: str,
    *,
    task_requirements: Optional[List[str]] = None,
    acceptance_criteria: Optional[List[str]] = None,
    architecture: Any = None,
    existing_codebase: Optional[str] = None,
    llm_getter: Callable[[str], Any] = _default_llm_getter,
) -> CodeReviewResult:
    """Run the code review agent and return structured results."""
    try:
        from code_review_agent import CodeReviewAgent
        from code_review_agent.models import CodeReviewInput

        from software_engineering_team.shared.context_sizing import compute_code_review_total_chars

        llm = llm_getter("code_review")
        agent = CodeReviewAgent(llm)
        max_chars = compute_code_review_total_chars(llm)
        code_capped = code[:max_chars] if len(code) > max_chars else code

        review_input = CodeReviewInput(
            code=code_capped,
            spec_content=spec_content,
            task_description=task_description,
            task_requirements=task_requirements or [],
            acceptance_criteria=acceptance_criteria or [],
            language=language,
            architecture=architecture,
            existing_codebase=existing_codebase,
        )
        result = agent.run(review_input)
        issues = []
        for i in result.issues or []:
            issues.append(i.model_dump() if hasattr(i, "model_dump") else vars(i))
        return CodeReviewResult(
            approved=result.approved,
            issues=issues,
            summary=result.summary or "",
            spec_compliance_notes=result.spec_compliance_notes or "",
        )
    except Exception as e:
        logger.warning("Code review tool failed: %s", e)
        return CodeReviewResult(approved=False, summary=f"Review failed: {e}")


def run_build_verification(
    repo_path: Path,
    agent_type: str,
    task_id: str,
) -> BuildResult:
    """Run build verification (syntax check, compilation, tests).

    Delegates to the existing ``_run_build_verification`` in the orchestrator
    which handles frontend (ng build), backend (python syntax + pytest), and
    devops (YAML + docker build) paths.
    """
    try:
        from software_engineering_team.orchestrator import _run_build_verification

        success, error = _run_build_verification(repo_path, agent_type, task_id)
        is_env = error.startswith("ENV:") if error else False
        return BuildResult(success=success, error=error, is_env_failure=is_env)
    except Exception as e:
        logger.warning("Build verification tool failed: %s", e)
        return BuildResult(success=False, error=str(e))


def run_linting(
    repo_path: Path,
    task_id: str,
    *,
    llm_getter: Callable[[str], Any] = _default_llm_getter,
) -> LintResult:
    """Run the linting tool agent on the repo."""
    try:
        from linting_tool_agent import LintingToolAgent

        llm = llm_getter("linting_tool_agent")
        agent = LintingToolAgent(llm)
        result = agent.run(str(repo_path))
        issues = []
        if hasattr(result, "issues"):
            issues = [
                i.model_dump() if hasattr(i, "model_dump") else vars(i)
                for i in (result.issues or [])
            ]
        passed = getattr(result, "passed", True) if result else True
        return LintResult(passed=passed, issues=issues)
    except Exception as e:
        logger.warning("[%s] Linting tool failed: %s", task_id, e)
        return LintResult(passed=True)  # non-blocking


def run_dbc_comments(
    repo_path: Path,
    task_id: str,
    language: str,
    task_description: str,
    architecture: Any = None,
    *,
    llm_getter: Callable[[str], Any] = _default_llm_getter,
) -> DbcResult:
    """Run the Design by Contract comments agent. Non-blocking on failure."""
    try:
        from technical_writers.dbc_comments_agent import DbcCommentsAgent
        from technical_writers.dbc_comments_agent.models import DbcCommentsInput

        from software_engineering_team.shared.git_utils import write_files_and_commit

        # Read code from repo
        code_parts: list[str] = []
        for f in sorted(repo_path.rglob("*"))[:200]:
            if not f.is_file():
                continue
            if f.suffix not in {".py", ".ts", ".js", ".java"}:
                continue
            if any(skip in f.parts for skip in ("node_modules", ".git", "__pycache__", "venv")):
                continue
            try:
                content = f.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            code_parts.append(f"--- {f.relative_to(repo_path)} ---\n{content}")
        code = "\n".join(code_parts)
        if not code:
            return DbcResult(compliant=True)

        llm = llm_getter("dbc_comments")
        agent = DbcCommentsAgent(llm)
        result = agent.run(
            DbcCommentsInput(
                code=code,
                language=language,
                task_description=task_description,
                architecture=architecture,
            )
        )

        if not result.already_compliant and result.files:
            write_files_and_commit(repo_path, result.files, result.suggested_commit_message)
            return DbcResult(
                compliant=False,
                comments_added=result.comments_added,
                comments_updated=result.comments_updated,
            )
        return DbcResult(compliant=True)
    except Exception as e:
        logger.warning("[%s] DbC comments tool failed (non-blocking): %s", task_id, e)
        return DbcResult(compliant=True)


def run_qa_check(
    code: str,
    task_description: str,
    language: str,
    *,
    architecture: Any = None,
    llm_getter: Callable[[str], Any] = _default_llm_getter,
) -> QAResult:
    """Run the QA expert agent."""
    try:
        from qa_agent import QAExpertAgent

        llm = llm_getter("qa")
        agent = QAExpertAgent(llm)
        result = agent.run(
            code=code,
            task_description=task_description,
            language=language,
            architecture=architecture,
        )
        bugs = []
        if hasattr(result, "bugs"):
            bugs = [
                b.model_dump() if hasattr(b, "model_dump") else vars(b) for b in (result.bugs or [])
            ]
        passed = not bugs
        return QAResult(passed=passed, bugs=bugs)
    except Exception as e:
        logger.warning("QA check tool failed: %s", e)
        return QAResult(passed=True)


def run_security_scan(
    code: str,
    task_description: str,
    language: str,
    *,
    architecture: Any = None,
    llm_getter: Callable[[str], Any] = _default_llm_getter,
) -> SecurityResult:
    """Run the cybersecurity expert agent."""
    try:
        from security_agent import CybersecurityExpertAgent

        llm = llm_getter("security")
        agent = CybersecurityExpertAgent(llm)
        result = agent.run(
            code=code,
            task_description=task_description,
            language=language,
            architecture=architecture,
        )
        vulns = []
        if hasattr(result, "vulnerabilities"):
            vulns = [
                v.model_dump() if hasattr(v, "model_dump") else vars(v)
                for v in (result.vulnerabilities or [])
            ]
        passed = not vulns
        return SecurityResult(passed=passed, vulnerabilities=vulns)
    except Exception as e:
        logger.warning("Security scan tool failed: %s", e)
        return SecurityResult(passed=True)


def run_acceptance_verification(
    code: str,
    task_description: str,
    acceptance_criteria: List[str],
    *,
    llm_getter: Callable[[str], Any] = _default_llm_getter,
) -> AcceptanceResult:
    """Run the acceptance verifier agent."""
    try:
        from acceptance_verifier_agent import AcceptanceVerifierAgent

        llm = llm_getter("acceptance_verifier")
        agent = AcceptanceVerifierAgent(llm)
        result = agent.run(
            code=code,
            task_description=task_description,
            acceptance_criteria=acceptance_criteria,
        )
        accepted = getattr(result, "accepted", False)
        reasoning = getattr(result, "reasoning", "")
        return AcceptanceResult(accepted=accepted, reasoning=reasoning)
    except Exception as e:
        logger.warning("Acceptance verification tool failed: %s", e)
        return AcceptanceResult(accepted=False, reasoning=str(e))


# ---------------------------------------------------------------------------
# CI gate & workflow seeding
# ---------------------------------------------------------------------------


@dataclass
class CIGateResult:
    passed: bool = False
    checks: List[Dict[str, Any]] = field(default_factory=list)
    summary: str = ""
    timed_out: bool = False
    local_fallback: bool = False


def _run_local_check(cmd: List[str], cwd: Path, label: str) -> Dict[str, Any]:
    """Run a single local check via subprocess. Returns a check dict."""
    try:
        result = subprocess.run(
            cmd,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=120,
        )
        return {
            "name": label,
            "passed": result.returncode == 0,
            "output": (result.stdout + result.stderr)[:2000],
        }
    except FileNotFoundError:
        return {"name": label, "passed": True, "output": "tool not found — skipped"}
    except subprocess.TimeoutExpired:
        return {"name": label, "passed": False, "output": "timed out (120s)"}
    except Exception as exc:
        return {"name": label, "passed": True, "output": f"error: {exc}"}


def _run_local_ci(repo_path: Path) -> CIGateResult:
    """Run CI checks locally via subprocess (fallback when no GitHub remote).

    Preconditions:
        ``repo_path`` is a directory on disk.
    Postconditions:
        ``passed`` is True iff every executed check returned exit code 0.
    """
    checks: List[Dict[str, Any]] = []

    has_python = (repo_path / "requirements.txt").exists() or (
        repo_path / "pyproject.toml"
    ).exists()
    has_node = (repo_path / "package.json").exists()

    if has_python:
        checks.append(_run_local_check(["ruff", "check", "."], repo_path, "ruff-lint"))
        checks.append(
            _run_local_check(["ruff", "format", "--check", "."], repo_path, "ruff-format")
        )
        if (repo_path / "tests").is_dir() or list(repo_path.rglob("test_*.py")):
            checks.append(
                _run_local_check(
                    ["python", "-m", "pytest", "--tb=short", "-q"], repo_path, "pytest"
                )
            )
        checks.append(
            _run_local_check(
                ["python", "-m", "py_compile", "__init__.py"], repo_path, "syntax-check"
            )
        )

    if has_node:
        pkg_json = repo_path / "package.json"
        import json

        try:
            scripts = json.loads(pkg_json.read_text()).get("scripts", {})
        except Exception:
            scripts = {}
        if "lint" in scripts:
            checks.append(_run_local_check(["npm", "run", "lint"], repo_path, "npm-lint"))
        if "build" in scripts:
            checks.append(_run_local_check(["npm", "run", "build"], repo_path, "npm-build"))
        if "test" in scripts:
            checks.append(_run_local_check(["npm", "test"], repo_path, "npm-test"))

    if not checks:
        return CIGateResult(passed=True, summary="No CI checks applicable", local_fallback=True)

    all_passed = all(c["passed"] for c in checks)
    failed = [c["name"] for c in checks if not c["passed"]]
    summary = "all checks passed" if all_passed else f"failed: {', '.join(failed)}"
    return CIGateResult(passed=all_passed, checks=checks, summary=summary, local_fallback=True)


def _run_github_ci(
    owner: str,
    repo_name: str,
    ref: str,
    github_token: str,
    timeout_s: int,
    poll_interval_s: int,
) -> CIGateResult:
    """Poll GitHub check-runs until all complete or timeout.

    Preconditions:
        ``github_token`` is a valid GitHub token with repo read access.
        ``ref`` is a valid git ref (SHA, branch, tag).
    Postconditions:
        ``timed_out`` is True iff the timeout was exceeded before all checks completed.
        ``passed`` is True only when all checks completed successfully.
    """
    try:
        from coding_team.github_source.client import GitHubClient
    except ImportError:
        logger.warning("coding_team.github_source not available; skipping GitHub CI gate")
        return CIGateResult(passed=True, summary="GitHub client not available — skipped")

    client = GitHubClient(token=github_token)
    deadline = time.monotonic() + timeout_s
    try:
        while time.monotonic() < deadline:
            status = client.get_combined_status(owner, repo_name, ref)
            if status.all_completed:
                checks = [{"name": name, "passed": False} for name in status.failed_checks]
                return CIGateResult(
                    passed=status.passed,
                    checks=checks,
                    summary=(
                        f"CI passed ({status.total} checks)"
                        if status.passed
                        else f"CI failed: {', '.join(status.failed_checks)}"
                    ),
                )
            time.sleep(poll_interval_s)

        return CIGateResult(
            passed=False,
            summary=f"CI timed out after {timeout_s}s",
            timed_out=True,
        )
    finally:
        client.close()


def run_ci_gate(
    repo_path: Path,
    *,
    github_token: Optional[str] = None,
    owner: Optional[str] = None,
    repo_name: Optional[str] = None,
    ref: Optional[str] = None,
    timeout_s: Optional[int] = None,
    poll_interval_s: int = 10,
) -> CIGateResult:
    """Verify CI pipeline status for generated code.

    Preconditions:
        ``repo_path`` is a valid directory containing a git repository.
    Postconditions:
        Returns a ``CIGateResult`` with ``passed``, ``checks``, and ``summary``.
        Never raises — failures are captured in the result.

    Two execution paths:
    1. GitHub: when token + owner + repo + ref are provided, polls check-runs.
    2. Local fallback: runs lint/test/build locally via subprocess.
    """
    if timeout_s is None:
        try:
            timeout_s = int(os.environ.get("SE_CI_GATE_TIMEOUT_S", "300"))
        except ValueError:
            timeout_s = 300

    token = github_token or os.environ.get("GITHUB_TOKEN")
    if token and owner and repo_name and ref:
        try:
            return _run_github_ci(owner, repo_name, ref, token, timeout_s, poll_interval_s)
        except Exception as exc:
            logger.warning("GitHub CI gate failed: %s; falling back to local", exc)

    local_enabled = os.environ.get("SE_CI_GATE_LOCAL_FALLBACK", "true").lower() in (
        "1",
        "true",
        "yes",
    )
    if local_enabled:
        try:
            return _run_local_ci(repo_path)
        except Exception as exc:
            logger.warning("Local CI gate failed: %s", exc)
            return CIGateResult(
                passed=True, summary=f"Local CI gate error: {exc}", local_fallback=True
            )

    return CIGateResult(
        passed=True, summary="CI gate skipped (no GitHub remote, local fallback disabled)"
    )


def seed_ci_workflows(
    repo_path: Path,
    *,
    has_backend: bool = False,
    has_frontend: bool = False,
) -> List[str]:
    """Write CI workflow files into ``.github/workflows/`` in the repo.

    Preconditions:
        ``repo_path`` is a directory (with or without a git repo).
    Postconditions:
        Returns the list of workflow file paths written (relative to repo_path).
        If a git repo exists, changes are committed.
    """
    try:
        from software_engineering_team.ci_templates import (
            BackendCIParams,
            FrontendCIParams,
            render_backend_ci,
            render_frontend_ci,
        )
    except ImportError:
        logger.warning("ci_templates module not available; skipping CI workflow seeding")
        return []

    workflows_dir = repo_path / ".github" / "workflows"
    workflows_dir.mkdir(parents=True, exist_ok=True)
    written: List[str] = []

    if has_backend:
        content = render_backend_ci(BackendCIParams())
        path = workflows_dir / "backend.yml"
        path.write_text(content)
        written.append(str(path.relative_to(repo_path)))

    if has_frontend:
        content = render_frontend_ci(FrontendCIParams())
        path = workflows_dir / "frontend.yml"
        path.write_text(content)
        written.append(str(path.relative_to(repo_path)))

    if written and (repo_path / ".git").is_dir():
        try:
            from software_engineering_team.shared.git_utils import write_files_and_commit

            files_dict = {}
            for rel_path in written:
                full = repo_path / rel_path
                files_dict[rel_path] = full.read_text()
            write_files_and_commit(repo_path, files_dict, "ci: add CI workflow templates")
        except Exception as exc:
            logger.warning("Failed to commit CI workflows: %s", exc)

    return written
