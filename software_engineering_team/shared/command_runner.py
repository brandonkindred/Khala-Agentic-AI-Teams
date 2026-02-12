"""
Command runner utility for executing build/test/serve commands.

Provides a safe way for the orchestrator to run commands like `ng build`,
`ng serve`, `python -m pytest`, etc. and capture their output for feedback
to coding agents.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Default timeouts (seconds)
BUILD_TIMEOUT = 120  # ng build, python -m pytest
SERVE_TIMEOUT = 30   # ng serve (just wait for it to start, then kill)
TEST_TIMEOUT = 120   # pytest


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
        """Short error summary suitable for agent feedback."""
        if self.success:
            return ""
        if self.timed_out:
            return "Command timed out"
        # Prefer stderr for error messages, fall back to stdout
        text = self.stderr.strip() if self.stderr and self.stderr.strip() else self.stdout.strip()
        # Truncate long output
        if len(text) > 4000:
            text = text[:4000] + "\n... [truncated]"
        return text


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
        return CommandResult(
            success=False,
            exit_code=-1,
            stdout=e.stdout or "" if hasattr(e, "stdout") and e.stdout else "",
            stderr=e.stderr or "" if hasattr(e, "stderr") and e.stderr else "",
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


def run_ng_build(project_path: str | Path) -> CommandResult:
    """
    Run `ng build` in the given Angular project directory.
    Returns CommandResult with compilation status and any errors.
    """
    return run_command(
        ["npx", "ng", "build", "--configuration=development"],
        cwd=project_path,
        timeout=BUILD_TIMEOUT,
    )


def run_ng_serve_smoke_test(project_path: str | Path, port: int = 4299) -> CommandResult:
    """
    Start `ng serve` briefly to confirm the app compiles and starts.
    Runs for SERVE_TIMEOUT seconds, then kills the process.

    This is a smoke test - it just confirms the app starts without errors.
    Returns CommandResult where success=True means the server started.
    """
    cwd = Path(project_path).resolve()
    logger.info("Starting ng serve smoke test on port %s in %s", port, cwd)

    try:
        proc = subprocess.Popen(
            ["npx", "ng", "serve", "--port", str(port), "--no-open"],
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            preexec_fn=os.setsid,
        )

        try:
            stdout, stderr = proc.communicate(timeout=SERVE_TIMEOUT)
            # If process exited within timeout, it probably failed
            return CommandResult(
                success=proc.returncode == 0,
                exit_code=proc.returncode,
                stdout=stdout or "",
                stderr=stderr or "",
            )
        except subprocess.TimeoutExpired:
            # Process is still running = server started successfully
            logger.info("ng serve is running (good) - killing smoke test process")
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                proc.wait(timeout=5)
            return CommandResult(
                success=True,
                exit_code=0,
                stdout="Angular dev server started successfully (smoke test passed)",
                stderr="",
            )
    except FileNotFoundError:
        return CommandResult(
            success=False,
            exit_code=-1,
            stdout="",
            stderr="npx/ng not found - Angular CLI may not be installed",
        )
    except Exception as e:
        logger.exception("ng serve smoke test failed")
        return CommandResult(
            success=False,
            exit_code=-1,
            stdout="",
            stderr=str(e),
        )


def run_pytest(project_path: str | Path, test_path: str = "") -> CommandResult:
    """
    Run `python -m pytest` in the given project directory.
    Returns CommandResult with test results.
    """
    cmd = ["python", "-m", "pytest", "-v", "--tb=short"]
    if test_path:
        cmd.append(test_path)
    return run_command(cmd, cwd=project_path, timeout=TEST_TIMEOUT)


def run_python_syntax_check(project_path: str | Path) -> CommandResult:
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
            ["python", "-m", "py_compile", str(f)],
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
