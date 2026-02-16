"""
Command runner utility for executing build/test/serve commands.

Provides a safe way for the orchestrator to run commands like `ng build`,
`ng serve`, `python -m pytest`, etc. and capture their output for feedback
to coding agents.
"""

from __future__ import annotations

import logging
import os
import shlex
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

# Node version required by Angular CLI (v20.19+ or v22.12+). NVM installs and uses this for frontend commands.
ANGULAR_NODE_VERSION = "22.12"
# Fallback Node version if ANGULAR_NODE_VERSION install fails (e.g. 22 = latest v22).
NVM_NODE_FALLBACK_VERSION = "22"


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


def ensure_nvm_installed() -> NvmInstallResult:
    """
    Ensure NVM is installed. If _get_nvm_script_prefix() already finds NVM, return success.
    Otherwise run the official NVM install script in a subprocess (non-interactive,
    timeout 120s). After the run, check again for ~/.nvm/nvm.sh and return success or
    failure with stderr for logging.
    """
    if _get_nvm_script_prefix() is not None:
        return NvmInstallResult(success=True)

    logger.info("NVM not found; attempting to install via official install script")
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
        logger.warning("NVM install script failed: exit_code=%s stderr=%s", result.returncode, stderr)
        return NvmInstallResult(success=False, stderr=stderr)

    if _get_nvm_script_prefix() is not None:
        logger.info("NVM installed successfully")
        return NvmInstallResult(success=True)
    return NvmInstallResult(
        success=False,
        stderr=stderr or "NVM install script completed but ~/.nvm/nvm.sh not found",
    )


def run_command_with_nvm(
    cmd: list[str],
    cwd: str | Path,
    node_version: str = ANGULAR_NODE_VERSION,
    timeout: int = BUILD_TIMEOUT,
) -> CommandResult:
    """
    Run a command in a bash shell with NVM loaded and the given Node version active.
    NVM will install the version if not present, then use it. For frontend (Angular)
    commands, pass ANGULAR_NODE_VERSION so Angular CLI runs in a supported environment.
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
    # Version check: fail fast if Node is below Angular CLI minimum (v20.19 or v22.12)
    version_check = (
        "node -e 'var v=process.versions.node.split(\".\").map(Number);"
        "var maj=v[0],min=v[1];"
        "if(maj>22)process.exit(0);"
        "if(maj===22&&min>=12)process.exit(0);"
        "if(maj===20&&min>=19)process.exit(0);"
        "console.error(\"Node \"+process.version+\" is below Angular CLI minimum v20.19/v22.12\");"
        "process.exit(1);'"
    )
    script = (
        f"{nvm_prefix} && "
        f"{{ nvm install {node_version} --no-progress && nvm use {node_version}; }} || "
        f"{{ nvm install {NVM_NODE_FALLBACK_VERSION} --no-progress && nvm use {NVM_NODE_FALLBACK_VERSION}; }} && "
        f"{version_check} && "
        f"{shlex.join(cmd)}"
    )
    logger.info(
        "Running command with NVM (node %s, fallback %s): %s in %s (timeout=%ss)",
        node_version,
        NVM_NODE_FALLBACK_VERSION,
        " ".join(cmd),
        cwd,
        timeout,
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
        logger.warning("Command with NVM timed out after %ss: %s", timeout, " ".join(cmd))
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


def run_ng_build_with_nvm_fallback(project_path: str | Path) -> CommandResult:
    """
    Run ng build with Node version Angular CLI needs. When NVM is available, use
    NVM to install and use ANGULAR_NODE_VERSION (22.12) first. When NVM is not
    found, return an explicit failure instead of using system Node (which is
    often too old).
    """
    if _get_nvm_script_prefix() is not None:
        logger.info("Running ng build with NVM (node %s)", ANGULAR_NODE_VERSION)
        return run_command_with_nvm(
            ["npx", "ng", "build", "--configuration=development"],
            cwd=project_path,
            node_version=ANGULAR_NODE_VERSION,
            timeout=BUILD_TIMEOUT,
        )
    msg = (
        "NVM not found. Angular CLI requires Node v20.19+ or v22.12+. "
        "Install NVM (https://github.com/nvm-sh/nvm) and run: nvm install 22.12"
    )
    try:
        r = subprocess.run(
            ["node", "--version"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if r.returncode == 0 and (r.stdout or r.stderr or "").strip():
            ver = (r.stdout or r.stderr or "").strip()
            msg += f" System Node is {ver}."
    except (FileNotFoundError, subprocess.TimeoutExpired, Exception):
        pass
    return CommandResult(success=False, exit_code=-1, stdout="", stderr=msg)


def ensure_frontend_dependencies_installed(project_path: str | Path) -> CommandResult:
    """
    Run npm install so dependencies are installed before the frontend agent runs.
    Uses NVM when available for consistent Node version. If package.json does not
    exist, returns success (no-op) so callers do not block.
    """
    cwd = Path(project_path).resolve()
    if not (cwd / "package.json").exists():
        return CommandResult(success=True, exit_code=0, stdout="", stderr="")
    if _get_nvm_script_prefix() is not None:
        return run_command_with_nvm(
            ["npm", "install"],
            cwd=cwd,
            node_version=ANGULAR_NODE_VERSION,
            timeout=BUILD_TIMEOUT,
        )
    return run_command(["npm", "install"], cwd=cwd, timeout=BUILD_TIMEOUT)


def run_ng_serve_smoke_test(project_path: str | Path, port: int = 4299) -> CommandResult:
    """
    Start `ng serve` briefly to confirm the app compiles and starts.
    Runs for SERVE_TIMEOUT seconds, then kills the process.
    When NVM is available, uses ANGULAR_NODE_VERSION so Angular CLI runs in a supported environment.

    This is a smoke test - it just confirms the app starts without errors.
    Returns CommandResult where success=True means the server started.
    """
    cwd = Path(project_path).resolve()
    logger.info("Starting ng serve smoke test on port %s in %s", port, cwd)

    nvm_prefix = _get_nvm_script_prefix()
    if nvm_prefix is not None:
        script = (
            f"{nvm_prefix} && "
            f"{{ nvm install {ANGULAR_NODE_VERSION} --no-progress && nvm use {ANGULAR_NODE_VERSION}; }} || "
            f"{{ nvm install {NVM_NODE_FALLBACK_VERSION} --no-progress && nvm use {NVM_NODE_FALLBACK_VERSION}; }} && "
            f"npx ng serve --port {port} --no-open"
        )
        run_cmd: list[str] = ["bash", "-c", script]
        logger.info("Using NVM (node %s, fallback %s) for ng serve smoke test", ANGULAR_NODE_VERSION, NVM_NODE_FALLBACK_VERSION)
    else:
        run_cmd = ["npx", "ng", "serve", "--port", str(port), "--no-open"]

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


_cached_python: Optional[str] = None


def _find_python() -> str:
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


def run_pytest(project_path: str | Path, test_path: str = "") -> CommandResult:
    """
    Run `python -m pytest` in the given project directory.
    Returns CommandResult with test results.
    """
    cmd = [_find_python(), "-m", "pytest", "-v", "--tb=short"]
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


# ---------------------------------------------------------------------------
# Angular / npm project initialization
# ---------------------------------------------------------------------------

# Minimal angular.json for a standalone Angular project
_MINIMAL_ANGULAR_JSON = """\
{
  "$schema": "./node_modules/@angular/cli/lib/config/schema.json",
  "version": 1,
  "newProjectRoot": "projects",
  "projects": {
    "app": {
      "projectType": "application",
      "root": "",
      "sourceRoot": "src",
      "prefix": "app",
      "architect": {
        "build": {
          "builder": "@angular/build:application",
          "options": {
            "outputPath": "dist/app",
            "index": "src/index.html",
            "browser": "src/main.ts",
            "tsConfig": "tsconfig.json",
            "styles": ["src/styles.scss"],
            "scripts": []
          },
          "configurations": {
            "development": {
              "optimization": false,
              "extractLicenses": false,
              "sourceMap": true
            },
            "production": {
              "optimization": true,
              "extractLicenses": true,
              "sourceMap": false
            }
          },
          "defaultConfiguration": "development"
        },
        "serve": {
          "builder": "@angular/build:dev-server",
          "configurations": {
            "development": { "buildTarget": "app:build:development" },
            "production": { "buildTarget": "app:build:production" }
          },
          "defaultConfiguration": "development"
        }
      }
    }
  }
}
"""

_MINIMAL_TSCONFIG = """\
{
  "compileOnSave": false,
  "compilerOptions": {
    "outDir": "./dist/out-tsc",
    "strict": true,
    "noImplicitOverride": true,
    "noPropertyAccessFromIndexSignature": true,
    "noImplicitReturns": true,
    "noFallthroughCasesInSwitch": true,
    "sourceMap": true,
    "declaration": false,
    "downlevelIteration": true,
    "experimentalDecorators": true,
    "moduleResolution": "node",
    "importHelpers": true,
    "target": "ES2022",
    "module": "ES2022",
    "lib": ["ES2022", "dom"],
    "skipLibCheck": true,
    "esModuleInterop": true,
    "forceConsistentCasingInFileNames": true,
    "baseUrl": "./"
  },
  "angularCompilerOptions": {
    "enableI18nLegacyMessageIdFormat": false,
    "strictInjectionParameters": true,
    "strictInputAccessModifiers": true,
    "strictTemplates": true
  }
}
"""

_MINIMAL_INDEX_HTML = """\
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>App</title>
  <base href="/">
  <meta name="viewport" content="width=device-width, initial-scale=1">
</head>
<body>
  <app-root></app-root>
</body>
</html>
"""

_MINIMAL_MAIN_TS = """\
import { bootstrapApplication } from '@angular/platform-browser';
import { AppComponent } from './app/app.component';
import { appConfig } from './app/app.config';

bootstrapApplication(AppComponent, appConfig)
  .catch((err) => console.error(err));
"""

_MINIMAL_APP_COMPONENT_TS = """\
import { Component } from '@angular/core';
import { RouterOutlet } from '@angular/router';

@Component({
  selector: 'app-root',
  standalone: true,
  imports: [RouterOutlet],
  template: '<router-outlet></router-outlet>',
  styleUrl: './app.component.scss',
})
export class AppComponent {
  title = 'app';
}
"""

_MINIMAL_APP_CONFIG_TS = """\
import { ApplicationConfig, provideZoneChangeDetection } from '@angular/core';
import { provideRouter } from '@angular/router';
import { provideHttpClient } from '@angular/common/http';
import { routes } from './app.routes';

export const appConfig: ApplicationConfig = {
  providers: [
    provideZoneChangeDetection({ eventCoalescing: true }),
    provideRouter(routes),
    provideHttpClient(),
  ],
};
"""

_MINIMAL_APP_ROUTES_TS = """\
import { Routes } from '@angular/router';

export const routes: Routes = [];
"""

# Angular runtime + dev dependencies
_ANGULAR_DEPS = [
    "@angular/core",
    "@angular/common",
    "@angular/compiler",
    "@angular/platform-browser",
    "@angular/platform-browser-dynamic",
    "@angular/router",
    "@angular/forms",
    "@angular/animations",
    "rxjs",
    "zone.js",
    "tslib",
]

_ANGULAR_DEV_DEPS = [
    "@angular/cli",
    "@angular/compiler-cli",
    "@angular/build",
    "typescript",
]


def ensure_frontend_project_initialized(project_dir: str | Path) -> CommandResult:
    """Ensure a minimal Angular project exists at *project_dir*.

    If ``package.json`` already exists the function is a no-op.
    Otherwise it:
    1. Creates the directory (if needed)
    2. Runs ``npm init -y``
    3. Installs Angular runtime and dev dependencies
    4. Writes minimal ``angular.json``, ``tsconfig.json``, and scaffold files
       (``src/index.html``, ``src/main.ts``, ``src/styles.scss``, and the
       root ``AppComponent`` + config + routes).

    Returns a :class:`CommandResult` indicating success or the first failure.
    """
    cwd = Path(project_dir).resolve()
    pkg_json = cwd / "package.json"

    if pkg_json.exists():
        logger.info("Frontend project already initialized at %s", cwd)
        return CommandResult(success=True, exit_code=0, stdout="Already initialized", stderr="")

    logger.info("Initializing new Angular project at %s", cwd)
    cwd.mkdir(parents=True, exist_ok=True)

    nvm_result = ensure_nvm_installed()
    if not nvm_result.success:
        logger.warning(
            "NVM install failed or unavailable: %s; frontend may need a specific Node version",
            nvm_result.stderr or "unknown",
        )
    use_nvm = _get_nvm_script_prefix() is not None
    if use_nvm:
        logger.info("Using NVM (node %s) for frontend project init", ANGULAR_NODE_VERSION)

    # Step 1: npm init
    if use_nvm:
        result = run_command_with_nvm(
            ["npm", "init", "-y"], cwd=cwd, node_version=ANGULAR_NODE_VERSION, timeout=30
        )
    else:
        result = run_command(["npm", "init", "-y"], cwd=cwd, timeout=30)
    if not result.success:
        return result

    # Step 2: Install runtime dependencies
    install_cmd = ["npm", "install", "--save"] + _ANGULAR_DEPS
    if use_nvm:
        result = run_command_with_nvm(
            install_cmd, cwd=cwd, node_version=ANGULAR_NODE_VERSION, timeout=BUILD_TIMEOUT
        )
    else:
        result = run_command(install_cmd, cwd=cwd, timeout=BUILD_TIMEOUT)
    if not result.success:
        return result

    # Step 3: Install dev dependencies
    dev_install_cmd = ["npm", "install", "--save-dev"] + _ANGULAR_DEV_DEPS
    if use_nvm:
        result = run_command_with_nvm(
            dev_install_cmd, cwd=cwd, node_version=ANGULAR_NODE_VERSION, timeout=BUILD_TIMEOUT
        )
    else:
        result = run_command(dev_install_cmd, cwd=cwd, timeout=BUILD_TIMEOUT)
    if not result.success:
        return result

    # Step 4: Write config files (only if they don't already exist)
    _write_if_missing(cwd / "angular.json", _MINIMAL_ANGULAR_JSON)
    _write_if_missing(cwd / "tsconfig.json", _MINIMAL_TSCONFIG)

    # Step 5: Write minimal scaffold files
    src = cwd / "src"
    src.mkdir(parents=True, exist_ok=True)
    app = src / "app"
    app.mkdir(parents=True, exist_ok=True)

    _write_if_missing(src / "index.html", _MINIMAL_INDEX_HTML)
    _write_if_missing(src / "main.ts", _MINIMAL_MAIN_TS)
    _write_if_missing(src / "styles.scss", "/* Global styles */\n")
    _write_if_missing(app / "app.component.ts", _MINIMAL_APP_COMPONENT_TS)
    _write_if_missing(app / "app.component.scss", "/* App root styles */\n")
    _write_if_missing(app / "app.config.ts", _MINIMAL_APP_CONFIG_TS)
    _write_if_missing(app / "app.routes.ts", _MINIMAL_APP_ROUTES_TS)

    # Step 6: Pin Node version for nvm use
    _write_if_missing(cwd / ".nvmrc", ANGULAR_NODE_VERSION + "\n")

    logger.info("Frontend project initialized successfully at %s", cwd)
    return CommandResult(
        success=True,
        exit_code=0,
        stdout=f"Angular project initialized at {cwd}",
        stderr="",
    )


def _write_if_missing(filepath: Path, content: str) -> None:
    """Write *content* to *filepath* only if the file does not yet exist."""
    if not filepath.exists():
        filepath.parent.mkdir(parents=True, exist_ok=True)
        filepath.write_text(content, encoding="utf-8")
        logger.info("Created %s", filepath)


# Minimal FastAPI backend skeleton for ensure_backend_project_initialized
_MINIMAL_REQUIREMENTS_TXT = """fastapi>=0.115,<1.0
uvicorn[standard]>=0.32,<1.0
"""

_MINIMAL_APP_MAIN_PY = """\"\"\"FastAPI application entry point.\"\"\"
from fastapi import FastAPI

app = FastAPI(title="API", version="0.1.0")


@app.get("/health")
def health():
    return {"status": "ok"}
"""


def ensure_backend_project_initialized(backend_dir: str | Path) -> CommandResult:
    """Ensure a minimal FastAPI backend project exists at *backend_dir*.

    If ``requirements.txt`` and ``app/main.py`` already exist, the function is a no-op.
    Otherwise it creates:
    - requirements.txt (fastapi, uvicorn)
    - app/__init__.py
    - app/main.py (minimal FastAPI app with /health)
    - tests/ directory with a trivial test so pytest runs without error

    Returns a :class:`CommandResult` indicating success or the first failure.
    """
    cwd = Path(backend_dir).resolve()
    requirements = cwd / "requirements.txt"
    main_py = cwd / "app" / "main.py"

    if requirements.exists() and main_py.exists():
        logger.info("Backend project already initialized at %s", cwd)
        return CommandResult(success=True, exit_code=0, stdout="Already initialized", stderr="")

    logger.info("Initializing new backend project at %s", cwd)
    cwd.mkdir(parents=True, exist_ok=True)

    _write_if_missing(requirements, _MINIMAL_REQUIREMENTS_TXT)
    app_dir = cwd / "app"
    app_dir.mkdir(parents=True, exist_ok=True)
    _write_if_missing(app_dir / "__init__.py", '"""Application package."""\n')
    _write_if_missing(main_py, _MINIMAL_APP_MAIN_PY)

    tests_dir = cwd / "tests"
    tests_dir.mkdir(parents=True, exist_ok=True)
    _write_if_missing(tests_dir / "__init__.py", "")
    _write_if_missing(
        tests_dir / "test_main.py",
        '"""Minimal test so pytest runs."""\n\ndef test_health():\n    assert True\n',
    )

    logger.info("Backend project initialized successfully at %s", cwd)
    return CommandResult(
        success=True,
        exit_code=0,
        stdout=f"Backend project initialized at {cwd}",
        stderr="",
    )
