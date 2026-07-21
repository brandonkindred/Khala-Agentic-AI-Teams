"""
Frontend dev-server smoke tests.

Starts a frontend dev server (``npm start``/``dev`` or ``ng serve``) briefly
to confirm the app compiles and starts, then kills it — a cheap signal that
doesn't require a full build.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
from pathlib import Path

from shared.command_runner.nvm import _get_nvm_script_prefix
from shared.command_runner.runner import (
    FRONTEND_NODE_VERSION,
    NVM_NODE_FALLBACK_VERSION,
    SERVE_TIMEOUT,
    CommandResult,
    detect_frontend_framework,
)

logger = logging.getLogger(__name__)


def _build_nvm_command(final_cmd: str, fallback_cmd: list[str]) -> list[str]:
    """
    Build a ``bash -c`` command that activates FRONTEND_NODE_VERSION via NVM
    (falling back to NVM_NODE_FALLBACK_VERSION if that install fails) before
    running ``final_cmd``.

    Returns ``fallback_cmd`` unchanged when NVM isn't available on this system.
    """
    nvm_prefix = _get_nvm_script_prefix()
    if nvm_prefix is None:
        return fallback_cmd
    script = (
        f"{nvm_prefix} && "
        f"{{ nvm install {FRONTEND_NODE_VERSION} --no-progress && nvm use {FRONTEND_NODE_VERSION}; }} || "
        f"{{ nvm install {NVM_NODE_FALLBACK_VERSION} --no-progress && nvm use {NVM_NODE_FALLBACK_VERSION}; }} && "
        f"{final_cmd}"
    )
    return ["bash", "-c", script]


def _run_dev_server_process(  # pragma: no cover  # integration-only: starts a real dev server subprocess
    run_cmd: list[str],
    cwd: Path,
    running_log_label: str,
    success_stdout: str,
    not_found_stderr: str,
    failure_log_label: str,
) -> CommandResult:
    """
    Start *run_cmd* as a subprocess and wait up to SERVE_TIMEOUT seconds.

    A process still running when the timeout elapses is treated as smoke-test
    success (the dev server started) and is killed before returning. A process
    that exits within the timeout returns ``success=proc.returncode == 0``.
    ``FileNotFoundError`` and other startup errors are reported as failures.
    """
    try:
        proc = subprocess.Popen(
            run_cmd,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            preexec_fn=os.setsid,
        )

        try:
            stdout, stderr = proc.communicate(timeout=SERVE_TIMEOUT)
            return CommandResult(
                success=proc.returncode == 0,
                exit_code=proc.returncode,
                stdout=stdout or "",
                stderr=stderr or "",
            )
        except subprocess.TimeoutExpired:
            logger.info("%s is running (good) - killing smoke test process", running_log_label)
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                proc.wait(timeout=5)
            return CommandResult(success=True, exit_code=0, stdout=success_stdout, stderr="")
    except FileNotFoundError:
        return CommandResult(success=False, exit_code=-1, stdout="", stderr=not_found_stderr)
    except Exception as e:
        logger.exception("%s failed", failure_log_label)
        return CommandResult(success=False, exit_code=-1, stdout="", stderr=str(e))


def run_frontend_serve_smoke_test(  # pragma: no cover  # integration-only: starts a real dev server subprocess
    project_path: str | Path, port: int = 4299, framework: str = ""
) -> CommandResult:
    """
    Start a frontend dev server briefly to confirm the app compiles and starts.
    Runs for SERVE_TIMEOUT seconds, then kills the process.

    This is a smoke test - it just confirms the app starts without errors.
    Returns CommandResult where success=True means the server started.
    """
    cwd = Path(project_path).resolve()
    detected_framework = framework or detect_frontend_framework(cwd)

    if detected_framework == "angular":
        return run_ng_serve_smoke_test(cwd, port)
    else:
        return run_npm_start_smoke_test(cwd, port)


def run_npm_start_smoke_test(
    project_path: str | Path, port: int = 3000
) -> (
    CommandResult
):  # pragma: no cover  # integration-only: starts `npm start` subprocess with timeout/kill
    """
    Start `npm start` or `npm run dev` briefly to confirm the app starts.
    For React/Vue projects using Vite, CRA, or similar.
    """
    cwd = Path(project_path).resolve()
    logger.info("Starting npm start smoke test on port %s in %s", port, cwd)

    # Try to determine the right start command from package.json
    start_cmd = "start"
    try:
        pkg_data = json.loads((cwd / "package.json").read_text(encoding="utf-8"))
        scripts = pkg_data.get("scripts", {})
        if "dev" in scripts:
            start_cmd = "dev"
        elif "start" in scripts:
            start_cmd = "start"
    except Exception:
        pass

    run_cmd = _build_nvm_command(f"npm run {start_cmd}", ["npm", "run", start_cmd])
    if run_cmd[0] == "bash":
        logger.info("Using NVM (node %s) for npm %s smoke test", FRONTEND_NODE_VERSION, start_cmd)

    return _run_dev_server_process(
        run_cmd,
        cwd,
        running_log_label="Dev server",
        success_stdout="Frontend dev server started successfully (smoke test passed)",
        not_found_stderr="npm not found",
        failure_log_label="npm start smoke test",
    )


def run_ng_serve_smoke_test(
    project_path: str | Path, port: int = 4299
) -> (
    CommandResult
):  # pragma: no cover  # integration-only: starts `ng serve` subprocess with timeout/kill
    """
    Start `ng serve` briefly to confirm the app compiles and starts.
    Runs for SERVE_TIMEOUT seconds, then kills the process.
    When NVM is available, uses FRONTEND_NODE_VERSION so Angular CLI runs in a supported environment.

    This is a smoke test - it just confirms the app starts without errors.
    Returns CommandResult where success=True means the server started.
    """
    cwd = Path(project_path).resolve()
    logger.info("Starting ng serve smoke test on port %s in %s", port, cwd)

    run_cmd = _build_nvm_command(
        f"npx ng serve --port {port} --no-open",
        ["npx", "ng", "serve", "--port", str(port), "--no-open"],
    )
    if run_cmd[0] == "bash":
        logger.info(
            "Using NVM (node %s, fallback %s) for ng serve smoke test",
            FRONTEND_NODE_VERSION,
            NVM_NODE_FALLBACK_VERSION,
        )

    return _run_dev_server_process(
        run_cmd,
        cwd,
        running_log_label="ng serve",
        success_stdout="Angular dev server started successfully (smoke test passed)",
        not_found_stderr="npx/ng not found - Angular CLI may not be installed",
        failure_log_label="ng serve smoke test",
    )
