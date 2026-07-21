"""
Command runner utility for executing build/test/serve commands.

Provides a safe way for the orchestrator to run frontend build commands
(npm run build, ng build, etc.), `python -m pytest`, and capture their
output for feedback to coding agents.

This is the base module of the ``shared.command_runner`` package: subprocess
primitives, frontend framework detection, and backend test/lint/syntax-check
runners. NVM management, Angular-specific repair, project scaffolding, and
frontend serve smoke tests live in sibling modules (``nvm``,
``angular_repair``, ``scaffolding``, ``smoke_test``) that import from here —
this module has no dependency on any of them at module load time.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)


def patch_json_file(path: Path, transform: Callable[[dict], bool], *, indent: int = 2) -> bool:
    """Read JSON from *path*, apply *transform*, and rewrite only if it changed.

    Absorbs the read → mutate → write-if-changed idiom the Angular-repair helpers
    duplicated. ``transform(data)`` mutates ``data`` in place and returns True iff
    it changed something.

    Preconditions: *transform* mutates its dict argument and returns a bool.
    Postconditions: returns True iff the file was rewritten. Best-effort — a
        missing file, or any read/parse/transform/write error, is logged and
        returns False. Never raises.
    """
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        changed = transform(data)
    except Exception as e:  # noqa: BLE001 - repair is best-effort
        logger.warning("Could not read/transform JSON %s: %s", path, e)
        return False
    if not changed:
        return False
    try:
        path.write_text(json.dumps(data, indent=indent), encoding="utf-8")
        return True
    except Exception as e:  # noqa: BLE001 - repair is best-effort
        logger.warning("Could not write JSON %s: %s", path, e)
        return False


def patch_text_file(path: Path, transform: Callable[[str], str]) -> bool:
    """Read text from *path*, apply *transform*, and rewrite only if it changed.

    Text counterpart of :func:`patch_json_file` for the regex/string-edit repairs.

    Preconditions: *transform* maps the file's text to the desired text.
    Postconditions: returns True iff ``transform(text) != text`` and the file was
        rewritten. Best-effort — a missing file or any read/transform/write error
        is logged and returns False. Never raises.
    """
    if not path.exists():
        return False
    try:
        text = path.read_text(encoding="utf-8")
        new_text = transform(text)
    except Exception as e:  # noqa: BLE001 - repair is best-effort
        logger.warning("Could not read/transform %s: %s", path, e)
        return False
    if new_text == text:
        return False
    try:
        path.write_text(new_text, encoding="utf-8")
        return True
    except Exception as e:  # noqa: BLE001 - repair is best-effort
        logger.warning("Could not write %s: %s", path, e)
        return False


# Default timeouts (seconds)
BUILD_TIMEOUT = 120  # frontend build, python -m pytest
SERVE_TIMEOUT = 30  # dev server (just wait for it to start, then kill)
TEST_TIMEOUT = 120  # pytest

# Node version for modern frontend frameworks. NVM installs and uses this for frontend commands.
# Angular CLI v19+ requires Node v20.19+ or v22.12+; React/Vue work with v18+.
# Using "22" (latest v22.x) avoids pinning to a specific patch that may have corrupted
# NVM cache entries on CI runners.
FRONTEND_NODE_VERSION = "22"
# Fallback Node version if FRONTEND_NODE_VERSION install fails (e.g. lts/* = current LTS).
NVM_NODE_FALLBACK_VERSION = "lts/*"

# Angular npm package version pin, shared by angular_repair (dependency repairs)
# and scaffolding (new-project dependency install).
ANGULAR_VERSION = "^19.0.0"


@dataclass
class CommandResult:
    """Result of running a command."""

    success: bool
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False

    @property
    def output(self) -> str:
        """Combined stdout + stderr for feeding back to agents."""
        parts = []
        if self.stdout and self.stdout.strip():
            parts.append(self.stdout.strip())
        if self.stderr and self.stderr.strip():
            parts.append(self.stderr.strip())
        return "\n".join(parts)

    @property
    def error_summary(self) -> str:
        """Full error summary suitable for agent feedback."""
        if self.success:
            return ""
        if self.timed_out:
            return "Command timed out"
        # Prefer stderr for error messages, fall back to stdout
        text = self.stderr.strip() if self.stderr and self.stderr.strip() else self.stdout.strip()
        return text

    def pytest_error_summary(self, max_chars: int = 200_000) -> str:
        """
        For pytest runs: extract the failure/error section so agents see the real
        error (e.g. ImportError, assertion) not the session header (rootdir: ...).
        Falls back to the full combined stdout+stderr (truncated to max_chars) if
        no ERRORS/FAILURES/collection-error marker is found.
        """
        if self.success:
            return ""
        text = (self.stdout or "") + "\n" + (self.stderr or "")
        for marker in ("= ERRORS =", "= FAILURES =", "ERROR collecting", "FAILED "):
            idx = text.find(marker)
            if idx != -1:
                excerpt = text[idx:].strip()
                return excerpt[:max_chars]
        # No marker: return full output
        return text.strip()[:max_chars]

    def parsed_failures(self, command_kind: str = "pytest") -> list:
        """
        Parse stdout/stderr into structured failures for agent consumption.

        command_kind: "pytest" | "ng_build" | "ng"
        Returns list of ParsedFailure objects (empty if success).
        """
        if self.success:
            return []
        try:
            from shared.command_runner.error_parsing import parse_command_failure

            return parse_command_failure(command_kind, self.stdout or "", self.stderr or "")
        except Exception as e:
            logger.debug("Error parsing failures: %s", e)
            return []


def run_command(
    cmd: list[str],
    cwd: str | Path,
    timeout: int = BUILD_TIMEOUT,
    env_override: Optional[dict] = None,
) -> CommandResult:
    """
    Run a command and capture its output.

    Args:
        cmd: Command and arguments (e.g., ["ng", "build"])
        cwd: Working directory
        timeout: Maximum seconds to wait
        env_override: Additional environment variables to set

    Returns:
        CommandResult with success status and output
    """
    cwd = Path(cwd).resolve()
    logger.info("Running command: %s in %s (timeout=%ss)", " ".join(cmd), cwd, timeout)

    env = os.environ.copy()
    if env_override:
        env.update(env_override)

    try:
        result = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
        success = result.returncode == 0
        logger.info(
            "Command %s: exit_code=%s, stdout=%s chars, stderr=%s chars",
            "succeeded" if success else "failed",
            result.returncode,
            len(result.stdout or ""),
            len(result.stderr or ""),
        )
        return CommandResult(
            success=success,
            exit_code=result.returncode,
            stdout=result.stdout or "",
            stderr=result.stderr or "",
        )
    except subprocess.TimeoutExpired as e:
        logger.warning("Command timed out after %ss: %s", timeout, " ".join(cmd))
        stdout_val = ""
        stderr_val = ""
        if hasattr(e, "stdout") and e.stdout:
            stdout_val = (
                e.stdout.decode("utf-8", errors="replace")
                if isinstance(e.stdout, bytes)
                else e.stdout
            )
        if hasattr(e, "stderr") and e.stderr:
            stderr_val = (
                e.stderr.decode("utf-8", errors="replace")
                if isinstance(e.stderr, bytes)
                else e.stderr
            )
        return CommandResult(
            success=False,
            exit_code=-1,
            stdout=stdout_val,
            stderr=stderr_val,
            timed_out=True,
        )
    except FileNotFoundError:
        logger.error("Command not found: %s", cmd[0])
        return CommandResult(
            success=False,
            exit_code=-1,
            stdout="",
            stderr=f"Command not found: {cmd[0]}",
        )
    except Exception as e:
        logger.exception("Unexpected error running command: %s", " ".join(cmd))
        return CommandResult(
            success=False,
            exit_code=-1,
            stdout="",
            stderr=str(e),
        )


def detect_frontend_framework(project_path: str | Path) -> str:
    """
    Detect the frontend framework from project files.

    Returns: "angular", "react", "vue", or "unknown"
    """
    import json

    cwd = Path(project_path).resolve()

    # Check for Angular-specific config
    if (cwd / "angular.json").exists():
        return "angular"

    # Check package.json for framework dependencies
    pkg_path = cwd / "package.json"
    if pkg_path.exists():
        try:
            data = json.loads(pkg_path.read_text(encoding="utf-8"))
            all_deps = {
                **data.get("dependencies", {}),
                **data.get("devDependencies", {}),
            }

            if "@angular/core" in all_deps or "@angular/common" in all_deps:
                return "angular"
            if "react" in all_deps or "react-dom" in all_deps:
                return "react"
            if "vue" in all_deps:
                return "vue"
        except (json.JSONDecodeError, Exception):
            pass

    # Check for Vue-specific files
    if (cwd / "vue.config.js").exists():
        return "vue"
    if any(cwd.rglob("*.vue")):
        return "vue"

    return "unknown"


def run_ng_build(project_path: str | Path) -> CommandResult:  # pragma: no cover
    """
    Run `ng build` in the given Angular project directory.
    Returns CommandResult with compilation status and any errors.
    """
    return run_command(
        ["npx", "ng", "build", "--configuration=development"],
        cwd=project_path,
        timeout=BUILD_TIMEOUT,
    )


def is_ng_build_environment_failure(result: CommandResult) -> bool:
    """
    Return True if the ng build failure is due to environment (e.g. Node version)
    rather than code. Such failures cannot be fixed by the frontend agent.

    Checks stderr for phrases like "Node.js version", "requires a minimum Node",
    "update your Node", etc.
    """
    if result.success:
        return False
    text = (result.stderr + "\n" + result.stdout).lower()
    return (
        "node.js version" in text
        or "requires a minimum node" in text
        or "update your node" in text
        or "update node.js" in text
    )


def run_frontend_build(
    project_path: str | Path, framework: str = ""
) -> CommandResult:  # pragma: no cover
    """
    Run the appropriate build command for the detected or specified frontend framework.

    Args:
        project_path: Path to the frontend project
        framework: Optional framework hint ("angular", "react", "vue"). If not provided,
                   will be auto-detected from project files.

    Returns CommandResult with build status and any errors.
    """
    cwd = Path(project_path).resolve()
    detected_framework = framework or detect_frontend_framework(cwd)

    if detected_framework == "angular":
        from shared.command_runner.angular_repair import run_ng_build_with_nvm_fallback

        return run_ng_build_with_nvm_fallback(cwd)

    from shared.command_runner.nvm import run_npm_build_with_nvm

    return run_npm_build_with_nvm(cwd)


_cached_python: Optional[str] = None


def _find_python() -> str:  # pragma: no cover
    """Return the name of an available Python interpreter, preferring 'python' then 'python3'.

    The result is cached so that discovery only runs once per process.
    """
    global _cached_python
    if _cached_python is not None:
        return _cached_python
    for candidate in ("python", "python3"):
        try:
            subprocess.run(
                [candidate, "--version"],
                capture_output=True,
                timeout=5,
            )
            logger.info("Using Python interpreter: %s", candidate)
            _cached_python = candidate
            return candidate
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
    logger.warning("Neither 'python' nor 'python3' found on PATH; defaulting to 'python3'")
    _cached_python = "python3"
    return _cached_python


def run_pytest(  # pragma: no cover  # integration-only: spawns real pytest subprocess against generated repo
    project_path: str | Path,
    test_path: str = "",
    python_exe: Optional[str] = None,
) -> CommandResult:  # pragma: no cover
    """
    Run `python -m pytest` in the given project directory.
    Returns CommandResult with test results.
    Uses --rootdir so pytest uses the project dir as root even when there is
    no pytest.ini/pyproject.toml (avoids rootdir falling back to /home/).
    Sets PYTHONPATH to the project root so `import app` works in agent-generated
    backends with app/ and tests/ at the same level.

    When python_exe is provided (e.g. sys.executable), use it instead of
    _find_python() so the same interpreter that ran pip install runs pytest.
    """
    root = str(Path(project_path).resolve())
    python = python_exe if python_exe else _find_python()
    cmd = [python, "-m", "pytest", "-v", "--tb=short", "--rootdir", root]
    if test_path:
        cmd.append(test_path)
    existing = os.environ.get("PYTHONPATH", "")
    pythonpath = root if not existing else f"{root}:{existing}"
    return run_command(
        cmd, cwd=project_path, timeout=TEST_TIMEOUT, env_override={"PYTHONPATH": pythonpath}
    )


def run_python_syntax_check(project_path: str | Path) -> CommandResult:  # pragma: no cover
    """
    Run a quick syntax check on all Python files in the project.
    Uses `python -m py_compile` on each .py file.
    """
    cwd = Path(project_path).resolve()
    py_files = list(cwd.rglob("*.py"))
    if not py_files:
        return CommandResult(
            success=True,
            exit_code=0,
            stdout="No Python files found",
            stderr="",
        )

    # Check syntax of all Python files
    errors = []
    for f in py_files:
        result = run_command(
            [_find_python(), "-m", "py_compile", str(f)],
            cwd=cwd,
            timeout=10,
        )
        if not result.success:
            errors.append(f"{f.relative_to(cwd)}: {result.stderr.strip()}")

    if errors:
        return CommandResult(
            success=False,
            exit_code=1,
            stdout="",
            stderr="Syntax errors found:\n" + "\n".join(errors),
        )

    return CommandResult(
        success=True,
        exit_code=0,
        stdout=f"All {len(py_files)} Python files pass syntax check",
        stderr="",
    )


def run_linter(project_path: str | Path, agent_type: str) -> CommandResult:  # pragma: no cover
    """Run the project linter and return the result.

    For backend (Python): runs ``ruff check .`` by default, falling back to
    ``flake8 .`` when the project's config (``.flake8``, ``setup.cfg``'s
    ``[flake8]`` section, or ``pyproject.toml`` lacking a ``[tool.ruff]``
    section) points at flake8 instead.
    For frontend: runs ``npx ng lint`` (Angular) or ``npx eslint .``.
    Returns a ``CommandResult`` whose ``success`` is True when there are zero violations.
    """
    cwd = Path(project_path).resolve()

    if agent_type == "backend":
        linter = "ruff"
        ruff_toml = cwd / "ruff.toml"
        pyproject = cwd / "pyproject.toml"
        flake8_cfg = cwd / ".flake8"
        setup_cfg = cwd / "setup.cfg"
        if ruff_toml.exists():
            linter = "ruff"
        elif pyproject.exists():
            try:
                text = pyproject.read_text(encoding="utf-8", errors="replace")
                if "[tool.ruff]" in text:
                    linter = "ruff"
                elif flake8_cfg.exists():
                    linter = "flake8"
                elif setup_cfg.exists():
                    setup_text = setup_cfg.read_text(encoding="utf-8", errors="replace")
                    if "[flake8]" in setup_text:
                        linter = "flake8"
            except Exception:
                pass
        elif flake8_cfg.exists():
            linter = "flake8"
        elif setup_cfg.exists():
            try:
                setup_text = setup_cfg.read_text(encoding="utf-8", errors="replace")
                if "[flake8]" in setup_text:
                    linter = "flake8"
            except Exception:
                pass
        cmd = [linter, "check", "."] if linter == "ruff" else [linter, "."]
        return run_command(cmd, cwd=cwd, timeout=120)

    # Frontend
    from shared.command_runner.nvm import run_command_with_nvm

    angular_json = cwd / "angular.json"
    if angular_json.exists():
        return run_command_with_nvm(["npx", "ng", "lint"], cwd=cwd)
    return run_command_with_nvm(["npx", "eslint", "."], cwd=cwd)
