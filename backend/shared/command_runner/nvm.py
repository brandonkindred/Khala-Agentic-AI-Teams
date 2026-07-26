"""
NVM (Node Version Manager) management for frontend build/serve commands.

Locates or installs NVM and runs commands under a specific Node version, so
modern frontend frameworks (Angular CLI, Vite, etc.) run in a supported
environment regardless of the system's default Node version.
"""

from __future__ import annotations

import logging
import os
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from shared.command_runner.executor import (
    BUILD_TIMEOUT,
    FRONTEND_NODE_VERSION,
    NVM_NODE_FALLBACK_VERSION,
    CommandResult,
    run_command,
)

logger = logging.getLogger(__name__)


def _get_nvm_script_prefix() -> Optional[str]:
    """
    Return a shell fragment that sources NVM (e.g. 'source "/home/user/.nvm/nvm.sh"'),
    or None if NVM is not found.
    """
    nvm_dir = os.environ.get("NVM_DIR") or str(Path.home() / ".nvm")
    nvm_sh = Path(nvm_dir) / "nvm.sh"
    if not nvm_sh.exists():
        return None
    return f'source "{nvm_sh}"'


NVM_INSTALL_SCRIPT_URL = "https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.4/install.sh"
NVM_INSTALL_TIMEOUT = 120


@dataclass
class NvmInstallResult:
    """Result of ensure_nvm_installed()."""

    success: bool
    stderr: str = ""


def ensure_nvm_installed() -> NvmInstallResult:  # pragma: no cover
    """
    Ensure NVM is installed. If _get_nvm_script_prefix() already finds NVM, return success.
    Otherwise run the official NVM install script in a subprocess (non-interactive,
    timeout 120s). After the run, check again for ~/.nvm/nvm.sh and return success or
    failure with stderr for logging.
    """
    if _get_nvm_script_prefix() is not None:
        return NvmInstallResult(success=True)

    logger.info("NVM not found. Next step -> Attempting to install via official install script")
    env = os.environ.copy()
    env["PROFILE"] = "/dev/null"

    def run_install(script_cmd: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", "-c", script_cmd],
            cwd=Path.home(),
            capture_output=True,
            text=True,
            timeout=NVM_INSTALL_TIMEOUT,
            env=env,
        )

    try:
        result = run_install(f"curl -o- {NVM_INSTALL_SCRIPT_URL} | bash")
        if result.returncode != 0:
            result = run_install(f"wget -qO- {NVM_INSTALL_SCRIPT_URL} | bash")
    except subprocess.TimeoutExpired as e:
        stderr = (e.stderr or "") if hasattr(e, "stderr") and e.stderr else "NVM install timed out"
        logger.warning("NVM install timed out after %ss", NVM_INSTALL_TIMEOUT)
        return NvmInstallResult(success=False, stderr=stderr)

    stderr = result.stderr or ""
    if result.returncode != 0:
        logger.warning(
            "NVM install failed. Recovery summary: 1) Tried curl, 2) Tried wget, "
            "both failed. exit_code=%s stderr=%s",
            result.returncode,
            stderr,
        )
        return NvmInstallResult(success=False, stderr=stderr)

    if _get_nvm_script_prefix() is not None:
        logger.info("NVM installed successfully")
        return NvmInstallResult(success=True)
    return NvmInstallResult(
        success=False,
        stderr=stderr or "NVM install script completed but ~/.nvm/nvm.sh not found",
    )


def run_command_with_nvm(  # pragma: no cover
    cmd: list[str],
    cwd: str | Path,
    node_version: str = FRONTEND_NODE_VERSION,
    timeout: int = BUILD_TIMEOUT,
) -> CommandResult:
    """
    Run a command in a bash shell with NVM loaded and the given Node version active.
    NVM will install the version if not present, then use it. For frontend commands,
    pass FRONTEND_NODE_VERSION so modern frameworks run in a supported environment.
    """
    cwd = Path(cwd).resolve()
    nvm_prefix = _get_nvm_script_prefix()
    if nvm_prefix is None:
        logger.warning("NVM not found (NVM_DIR or ~/.nvm/nvm.sh); cannot run command with NVM")
        return CommandResult(
            success=False,
            exit_code=-1,
            stdout="",
            stderr="NVM not found; cannot switch Node version",
        )
    # Version check: fail fast if Node is below modern frontend minimum (v18+)
    version_check = (
        'node -e \'var v=process.versions.node.split(".").map(Number);'
        "var maj=v[0];"
        "if(maj>=18)process.exit(0);"
        'console.error("Node "+process.version+" is below minimum v18 for modern frontend frameworks");'
        "process.exit(1);'"
    )
    # Try all instant `nvm use` options before any slow `nvm install`.
    # Each `nvm use` is validated with `npm --version` so we skip versions
    # where node works but npm is corrupted (broken tar extraction in cache).
    # In CI, setup-node puts Node on the system PATH so `nvm use system`
    # succeeds immediately, avoiding corrupted-cache source compilations.
    npm_ok = "npm --version >/dev/null 2>&1"
    script = (
        f"{nvm_prefix} && "
        f"{{ {{ nvm use {node_version} 2>/dev/null && {npm_ok}; }} || "
        f"{{ nvm use {NVM_NODE_FALLBACK_VERSION} 2>/dev/null && {npm_ok}; }} || "
        f"{{ nvm use system 2>/dev/null && {npm_ok}; }} || "
        f"{{ nvm install {node_version} --no-progress && nvm use {node_version}; }} || "
        f"{{ nvm install {NVM_NODE_FALLBACK_VERSION} --no-progress && nvm use {NVM_NODE_FALLBACK_VERSION}; }}; }} && "
        f"{version_check} && "
        f"{shlex.join(cmd)}"
    )
    logger.info(
        "Running command with NVM (node %s, fallback %s): %s in %s (timeout=%ss). "
        "Next step -> Attempting primary Node version, falling back to %s if unavailable",
        node_version,
        NVM_NODE_FALLBACK_VERSION,
        " ".join(cmd),
        cwd,
        timeout,
        NVM_NODE_FALLBACK_VERSION,
    )
    try:
        result = subprocess.run(
            ["bash", "-c", script],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=os.environ.copy(),
        )
        success = result.returncode == 0
        logger.info(
            "Command with NVM %s: exit_code=%s, stdout=%s chars, stderr=%s chars",
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
        logger.warning(
            "Command with NVM timed out after %ss: %s. Recovery summary: "
            "1) Attempted Node %s, 2) Fallback to Node %s, 3) Command execution timeout",
            timeout,
            " ".join(cmd),
            node_version,
            NVM_NODE_FALLBACK_VERSION,
        )
        return CommandResult(
            success=False,
            exit_code=-1,
            stdout=e.stdout or "" if hasattr(e, "stdout") and e.stdout else "",
            stderr=e.stderr or "" if hasattr(e, "stderr") and e.stderr else "",
            timed_out=True,
        )
    except Exception as e:
        logger.exception("Unexpected error running command with NVM: %s", " ".join(cmd))
        return CommandResult(
            success=False,
            exit_code=-1,
            stdout="",
            stderr=str(e),
        )


def run_npm_build_with_nvm(project_path: str | Path) -> CommandResult:  # pragma: no cover
    """
    Run `npm run build` for React/Vue/generic frontend projects.
    Uses NVM when available for consistent Node version.
    """
    cwd = Path(project_path).resolve()

    if _get_nvm_script_prefix() is not None:
        logger.info("Running npm run build with NVM (node %s)", FRONTEND_NODE_VERSION)
        return run_command_with_nvm(
            ["npm", "run", "build"],
            cwd=cwd,
            node_version=FRONTEND_NODE_VERSION,
            timeout=BUILD_TIMEOUT,
        )

    # Try without NVM
    return run_command(["npm", "run", "build"], cwd=cwd, timeout=BUILD_TIMEOUT)
