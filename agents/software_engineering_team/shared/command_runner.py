"""
Command runner utility for executing build/test/serve commands.

Provides a safe way for the orchestrator to run commands like `ng build`,
`ng serve`, `python -m pytest`, etc. and capture their output for feedback
to coding agents.
"""

from __future__ import annotations

import logging
import os
import re
import sys
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

    def pytest_error_summary(self, max_chars: int = 2500) -> str:
        """
        For pytest runs: extract the failure/error section so agents see the real
        error (e.g. ImportError, assertion) not the session header (rootdir: ...).
        Falls back to error_summary if no ERRORS/FAILURES section found.
        """
        if self.success:
            return ""
        text = (self.stdout or "") + "\n" + (self.stderr or "")
        for marker in ("= ERRORS =", "= FAILURES =", "ERROR collecting", "FAILED "):
            idx = text.find(marker)
            if idx != -1:
                excerpt = text[idx:].strip()
                if len(excerpt) > max_chars:
                    excerpt = excerpt[:max_chars] + "\n... [truncated]"
                return excerpt
        # No marker: return tail of output (where pytest usually puts the failure)
        text = text.strip()
        if len(text) > max_chars:
            text = "...\n" + text[-max_chars:]
        return text

    def parsed_failures(self, command_kind: str = "pytest") -> list:
        """
        Parse stdout/stderr into structured failures for agent consumption.

        command_kind: "pytest" | "ng_build" | "ng"
        Returns list of ParsedFailure objects (empty if success).
        """
        if self.success:
            return []
        try:
            from shared.error_parsing import parse_command_failure
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


def run_command_with_nvm(  # pragma: no cover
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


def run_ng_build_with_nvm_fallback(project_path: str | Path) -> CommandResult:  # pragma: no cover
    """
    Run ng build with Node version Angular CLI needs. When NVM is available, use
    NVM to install and use ANGULAR_NODE_VERSION (22.12) first. When NVM is not
    found, return an explicit failure instead of using system Node (which is
    often too old).
    """
    cwd = Path(project_path).resolve()
    _ensure_angular_common_in_package_json(cwd)
    _ensure_angular_material_in_package_json(cwd)
    _ensure_tsconfig_module_resolution(cwd)
    _ensure_material_theme_in_styles(cwd)
    _ensure_provide_animations_in_config(cwd)
    _ensure_app_config_di_token_imports(cwd)
    _ensure_reactive_forms_module_in_components(cwd)
    _normalize_double_at_angular(cwd)
    if _get_nvm_script_prefix() is not None:
        logger.info("Running ng build with NVM (node %s)", ANGULAR_NODE_VERSION)
        return run_command_with_nvm(
            ["npx", "ng", "build", "--configuration=development"],
            cwd=cwd,
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


def _ensure_angular_common_in_package_json(cwd: Path) -> None:
    """
    Ensure package.json has @angular/common (provides @angular/common/http).
    Repairs projects where package.json was overwritten or is missing this dep.
    """
    import json
    pkg_path = cwd / "package.json"
    if not pkg_path.exists():
        return
    try:
        data = json.loads(pkg_path.read_text(encoding="utf-8"))
        deps = data.setdefault("dependencies", {})
        if "@angular/common" not in deps:
            deps["@angular/common"] = _ANGULAR_VERSION
            pkg_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
            logger.info("Repaired package.json: added @angular/common for HttpClient support")
    except Exception as e:
        logger.warning("Could not repair package.json for @angular/common: %s", e)


def _ensure_angular_material_in_package_json(cwd: Path) -> None:
    """
    Ensure package.json has @angular/material and @angular/cdk.
    Repairs projects where package.json was overwritten or is missing these deps.
    """
    import json
    pkg_path = cwd / "package.json"
    if not pkg_path.exists():
        return
    try:
        data = json.loads(pkg_path.read_text(encoding="utf-8"))
        deps = data.setdefault("dependencies", {})
        changed = False
        if "@angular/material" not in deps:
            deps["@angular/material"] = _ANGULAR_VERSION
            changed = True
        if "@angular/cdk" not in deps:
            deps["@angular/cdk"] = _ANGULAR_VERSION
            changed = True
        if changed:
            pkg_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
            logger.info("Repaired package.json: added @angular/material and @angular/cdk")
    except Exception as e:
        logger.warning("Could not repair package.json for @angular/material: %s", e)


def _ensure_tsconfig_module_resolution(cwd: Path) -> None:
    """
    Ensure tsconfig.json uses moduleResolution 'bundler' for Angular 17+.
    Fixes 'Cannot find module @angular/common/http' when resolution was 'node'.
    """
    import json
    ts_path = cwd / "tsconfig.json"
    if not ts_path.exists():
        return
    try:
        data = json.loads(ts_path.read_text(encoding="utf-8"))
        opts = data.get("compilerOptions") or {}
        if opts.get("moduleResolution") == "node":
            opts["moduleResolution"] = "bundler"
            data["compilerOptions"] = opts
            ts_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
            logger.info("Repaired tsconfig.json: moduleResolution node -> bundler for Angular compatibility")
    except Exception as e:
        logger.warning("Could not repair tsconfig.json: %s", e)


def _ensure_material_theme_in_styles(cwd: Path) -> None:
    """
    Ensure styles.scss (or styles.css) has a Material prebuilt theme import.
    Appends it at the top if missing. Required for Angular Material components.
    """
    for name in ("styles.scss", "styles.css"):
        styles_path = cwd / "src" / name
        if not styles_path.exists():
            continue
        try:
            content = styles_path.read_text(encoding="utf-8")
            if "material" in content.lower() and ("prebuilt-themes" in content or "indigo-pink" in content):
                return
            theme_line = "@use '@angular/material/prebuilt-themes/indigo-pink.css';\n"
            new_content = theme_line + content if content.strip() else theme_line
            styles_path.write_text(new_content, encoding="utf-8")
            logger.info("Repaired %s: added Material prebuilt theme import", name)
        except Exception as e:
            logger.warning("Could not repair %s for Material theme: %s", name, e)
        return


def _ensure_provide_animations_in_config(cwd: Path) -> None:
    """
    Ensure app.config.ts has provideAnimations in providers.
    Adds import and provider if missing. Required for Angular Material components.
    """
    config_path = cwd / "src" / "app" / "app.config.ts"
    if not config_path.exists():
        return
    try:
        content = config_path.read_text(encoding="utf-8")
        if "provideAnimations" in content:
            return
        if "providers:" not in content:  # pragma: no cover
            return
        import_line = "import { provideAnimations } from '@angular/platform-browser/animations';\n"
        lines = content.split("\n")
        insert_idx = 0
        for i, line in enumerate(lines):
            if line.strip().startswith("import "):
                insert_idx = i + 1
            elif insert_idx > 0 and not line.strip().startswith("import "):
                break
        lines.insert(insert_idx, import_line.rstrip())
        content = "\n".join(lines)
        if "provideAnimations()" not in content:
            if "provideHttpClient()," in content:
                content = content.replace(
                    "provideHttpClient(),",
                    "provideHttpClient(),\n    provideAnimations(),",
                )
            elif "provideRouter(routes)," in content:
                content = content.replace(
                    "provideRouter(routes),",
                    "provideRouter(routes),\n    provideAnimations(),",
                )
        if "provideAnimations()" in content:
            config_path.write_text(content, encoding="utf-8")
            logger.info("Repaired app.config.ts: added provideAnimations")
    except Exception as e:  # pragma: no cover
        logger.warning("Could not repair app.config.ts for provideAnimations: %s", e)


# Well-known Angular DI tokens that must be imported when used in app.config.ts
_APP_CONFIG_TOKEN_IMPORTS: dict[str, str] = {
    "HTTP_INTERCEPTORS": "@angular/common/http",
}


def _ensure_app_config_di_token_imports(cwd: Path) -> None:
    """
    Ensure app.config.ts imports any DI tokens it uses in providers.
    E.g. HTTP_INTERCEPTORS must be imported from @angular/common/http when
    used in { provide: HTTP_INTERCEPTORS, useClass: ..., multi: true }.
    """
    import re

    config_path = cwd / "src" / "app" / "app.config.ts"
    if not config_path.exists():
        return
    try:
        content = config_path.read_text(encoding="utf-8")
        changed = False
        for token, module in _APP_CONFIG_TOKEN_IMPORTS.items():
            if token not in content:
                continue
            # Already imported if token appears in an import from the expected module
            already_imported = re.search(
                r"import\s*\{[^}]*\b" + re.escape(token) + r"\b[^}]*\}\s*from\s*['\"]"
                + re.escape(module) + r"['\"]",
                content,
            )
            if already_imported:
                continue
            # Check for existing import from this module
            match = re.search(
                r"import\s*\{([^}]+)\}\s*from\s*['\"]" + re.escape(module) + r"['\"]",
                content,
            )
            if match:
                imports = match.group(1)
                if token in imports:
                    continue
                new_imports = f"{token}, {imports.strip()}" if imports.strip() else token
                new_line = f"import {{ {new_imports} }} from '{module}';"
                old_line = match.group(0)
                content = content.replace(old_line, new_line, 1)
                changed = True
            else:
                lines = content.split("\n")
                insert_idx = 0
                for i, line in enumerate(lines):
                    if line.strip().startswith("import "):
                        insert_idx = i + 1
                    elif insert_idx > 0 and not line.strip().startswith("import "):
                        break
                import_line = f"import {{ {token} }} from '{module}';\n"
                lines.insert(insert_idx, import_line.rstrip())
                content = "\n".join(lines)
                changed = True
        if changed:
            config_path.write_text(content, encoding="utf-8")
            logger.info("Repaired app.config.ts: ensured HTTP_INTERCEPTORS import")
    except Exception as e:  # pragma: no cover
        logger.warning("Could not repair app.config.ts for DI token imports: %s", e)


def _normalize_double_at_angular(cwd: Path) -> None:
    """
    Fix @@angular typo (double @) in frontend .ts and .html files.
    Replaces '@@angular with '@angular and \"@@angular with \"@angular.
    """
    src = cwd / "src"
    if not src.exists():
        return
    for ext in ("*.ts", "*.html"):
        for path in src.rglob(ext):
            try:
                content = path.read_text(encoding="utf-8")
                if "@@angular" not in content:
                    continue
                new_content = content.replace("'@@angular", "'@angular").replace('"@@angular', '"@angular')
                if new_content != content:
                    path.write_text(new_content, encoding="utf-8")
                    logger.info("Repaired %s: normalized @@angular to @angular", path.name)
            except Exception as e:
                logger.warning("Could not normalize @@angular in %s: %s", path.name, e)


def _ensure_reactive_forms_module_in_components(cwd: Path) -> None:
    """
    Ensure components that use formGroup/formControlName/formArrayName in their
    template import ReactiveFormsModule. Scans .component.html files and adds
    the import to the corresponding .component.ts if missing.
    """
    src = cwd / "src"
    if not src.exists():
        return
    for html_path in src.rglob("*.component.html"):
        try:
            html_content = html_path.read_text(encoding="utf-8")
            if "formGroup" not in html_content and "formControlName" not in html_content and "formArrayName" not in html_content:
                continue
            ts_path = html_path.with_suffix(".ts")
            if not ts_path.exists():
                continue
            ts_content = ts_path.read_text(encoding="utf-8")
            if "ReactiveFormsModule" in ts_content:
                continue
            if "import { ReactiveFormsModule }" not in ts_content:
                lines = ts_content.split("\n")
                insert_idx = 0
                for i, line in enumerate(lines):
                    if line.strip().startswith("import "):
                        insert_idx = i + 1
                    elif insert_idx > 0 and not line.strip().startswith("import ") and line.strip() and not line.strip().startswith("//"):
                        break
                lines.insert(insert_idx, "import { ReactiveFormsModule } from '@angular/forms';")
                ts_content = "\n".join(lines)
            imports_match = re.search(r"imports:\s*\[", ts_content)
            if not imports_match:
                continue
            pos = imports_match.end()
            after_bracket = ts_content[pos:pos + 80]
            if "ReactiveFormsModule" in after_bracket or re.search(r"ReactiveFormsModule\s*[,\)]", ts_content[pos:pos + 200]):
                continue
            ts_content = ts_content[:pos] + "ReactiveFormsModule,\n    " + ts_content[pos:]
            ts_path.write_text(ts_content, encoding="utf-8")
            logger.info("Repaired %s: added ReactiveFormsModule for formGroup", ts_path.name)
        except Exception as e:
            logger.warning("Could not repair %s for ReactiveFormsModule: %s", html_path.name, e)


def ensure_frontend_dependencies_installed(project_path: str | Path) -> CommandResult:
    """
    Run npm install so dependencies are installed before the frontend agent runs.
    Uses NVM when available for consistent Node version. If package.json does not
    exist, returns success (no-op) so callers do not block.
    Ensures @angular/common is present (provides @angular/common/http for HttpClient).
    """
    cwd = Path(project_path).resolve()
    if not (cwd / "package.json").exists():
        return CommandResult(success=True, exit_code=0, stdout="", stderr="")
    _ensure_angular_common_in_package_json(cwd)
    _ensure_angular_material_in_package_json(cwd)
    _ensure_tsconfig_module_resolution(cwd)
    _ensure_material_theme_in_styles(cwd)
    _ensure_provide_animations_in_config(cwd)
    _ensure_app_config_di_token_imports(cwd)
    _ensure_reactive_forms_module_in_components(cwd)
    _normalize_double_at_angular(cwd)
    if _get_nvm_script_prefix() is not None:  # pragma: no cover
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
    except FileNotFoundError:  # pragma: no cover
        return CommandResult(
            success=False,
            exit_code=-1,
            stdout="",
            stderr="npx/ng not found - Angular CLI may not be installed",
        )
    except Exception as e:  # pragma: no cover
        logger.exception("ng serve smoke test failed")
        return CommandResult(
            success=False,
            exit_code=-1,
            stdout="",
            stderr=str(e),
        )


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


def run_pytest(
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

    For backend (Python): runs ``ruff check .`` (fast, zero-config default).
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
    angular_json = cwd / "angular.json"
    if angular_json.exists():
        return run_command_with_nvm(["npx", "ng", "lint"], cwd=cwd)
    return run_command_with_nvm(["npx", "eslint", "."], cwd=cwd)


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
        },
        "test": {
          "builder": "@angular/build:application",
          "options": {
            "buildTarget": "app:build:development"
          },
          "configurations": {
            "development": {}
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
    "moduleResolution": "bundler",
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
  <link href="https://fonts.googleapis.com/css2?family=Roboto:wght@300;400;500&display=swap" rel="stylesheet">
  <link href="https://fonts.googleapis.com/icon?family=Material+Icons" rel="stylesheet">
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
import { provideAnimations } from '@angular/platform-browser/animations';
import { routes } from './app.routes';

export const appConfig: ApplicationConfig = {
  providers: [
    provideZoneChangeDetection({ eventCoalescing: true }),
    provideRouter(routes),
    provideHttpClient(),
    provideAnimations(),
  ],
};
"""

_MINIMAL_APP_ROUTES_TS = """\
import { Routes } from '@angular/router';

export const routes: Routes = [];
"""

_MINIMAL_ENVIRONMENT_TS = """\
export const environment = {
  production: false,
  apiUrl: 'http://localhost:8000',
};
"""

_MINIMAL_ENVIRONMENT_PROD_TS = """\
export const environment = {
  production: true,
  apiUrl: '/api',
};
"""

# Angular runtime + dev dependencies (pinned to same major for compatibility)
# @angular/common provides @angular/common/http (HttpClient, provideHttpClient)
_ANGULAR_VERSION = "^18.0.0"
_ANGULAR_DEPS = [
    f"@angular/core@{_ANGULAR_VERSION}",
    f"@angular/common@{_ANGULAR_VERSION}",
    f"@angular/compiler@{_ANGULAR_VERSION}",
    f"@angular/platform-browser@{_ANGULAR_VERSION}",
    f"@angular/platform-browser-dynamic@{_ANGULAR_VERSION}",
    f"@angular/router@{_ANGULAR_VERSION}",
    f"@angular/forms@{_ANGULAR_VERSION}",
    f"@angular/animations@{_ANGULAR_VERSION}",
    f"@angular/material@{_ANGULAR_VERSION}",
    f"@angular/cdk@{_ANGULAR_VERSION}",
    "rxjs",
    "zone.js",
    "tslib",
]

_ANGULAR_DEV_DEPS = [
    f"@angular/cli@{_ANGULAR_VERSION}",
    f"@angular/compiler-cli@{_ANGULAR_VERSION}",
    f"@angular/build@{_ANGULAR_VERSION}",
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
    if use_nvm:  # pragma: no cover
        logger.info("Using NVM (node %s) for frontend project init", ANGULAR_NODE_VERSION)

    # Step 1: npm init
    if use_nvm:  # pragma: no cover
        result = run_command_with_nvm(
            ["npm", "init", "-y"], cwd=cwd, node_version=ANGULAR_NODE_VERSION, timeout=30
        )
    else:
        result = run_command(["npm", "init", "-y"], cwd=cwd, timeout=30)
    if not result.success:
        return result

    # Step 2: Install runtime dependencies
    install_cmd = ["npm", "install", "--save"] + _ANGULAR_DEPS
    if use_nvm:  # pragma: no cover
        result = run_command_with_nvm(
            install_cmd, cwd=cwd, node_version=ANGULAR_NODE_VERSION, timeout=BUILD_TIMEOUT
        )
    else:
        result = run_command(install_cmd, cwd=cwd, timeout=BUILD_TIMEOUT)
    if not result.success:
        return result

    # Step 3: Install dev dependencies
    dev_install_cmd = ["npm", "install", "--save-dev"] + _ANGULAR_DEV_DEPS
    if use_nvm:  # pragma: no cover
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
    _write_if_missing(
        src / "styles.scss",
        """@use '@angular/material/prebuilt-themes/indigo-pink.css';

html, body { height: 100%; }
body { margin: 0; font-family: Roboto, "Helvetica Neue", sans-serif; }
""",
    )
    _write_if_missing(app / "app.component.ts", _MINIMAL_APP_COMPONENT_TS)
    _write_if_missing(app / "app.component.scss", "/* App root styles */\n")
    _write_if_missing(app / "app.config.ts", _MINIMAL_APP_CONFIG_TS)
    _write_if_missing(app / "app.routes.ts", _MINIMAL_APP_ROUTES_TS)

    # Step 5b: Environment files for API base URL
    env_dir = src / "environments"
    env_dir.mkdir(parents=True, exist_ok=True)
    _write_if_missing(env_dir / "environment.ts", _MINIMAL_ENVIRONMENT_TS)
    _write_if_missing(env_dir / "environment.prod.ts", _MINIMAL_ENVIRONMENT_PROD_TS)

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


# Python/FastAPI .gitignore for backend projects (no Node patterns)
_PYTHON_GITIGNORE = """# Byte-compiled / optimized / DLL files
__pycache__/
*.py[cod]
*$py.class
*.so
.Python
build/
develop-eggs/
dist/
downloads/
eggs/
.eggs/
lib/
lib64/
parts/
sdist/
var/
wheels/
*.egg-info/
.installed.cfg
*.egg

# Virtual environments
.venv
venv/
ENV/
env/

# Environment and secrets
.env
.env.local
.env.*.local

# Testing and coverage
.pytest_cache/
.mypy_cache/
.coverage
htmlcov/
.tox/
.nox/
*.db
test.db

# IDE
.idea/
.vscode/
*.swp
*.swo
*~

# OS
.DS_Store
Thumbs.db
"""

# Minimal FastAPI backend skeleton for ensure_backend_project_initialized
# httpx>=0.24: Starlette's TestClient passes follow_redirects to httpx; older httpx raises TypeError
# sqlalchemy>=2.0: use String(36) for UUID columns (SQLite used in tests has no native UUID type)
_MINIMAL_REQUIREMENTS_TXT = """fastapi>=0.115,<1.0
uvicorn[standard]>=0.32,<1.0
httpx>=0.24,<0.28
sqlalchemy>=2.0,<3.0
"""

_MINIMAL_APP_MAIN_PY = """\"\"\"FastAPI application entry point.\"\"\"
from fastapi import FastAPI

app = FastAPI(title="API", version="0.1.0")


@app.get("/health")
def health():
    return {"status": "ok"}
"""


def ensure_backend_project_initialized(backend_dir: str | Path) -> CommandResult:  # pragma: no cover
    """Ensure a minimal FastAPI backend project exists at *backend_dir*.

    Creates (if missing):
    - requirements.txt (fastapi, uvicorn)
    - app/__init__.py, app/main.py (minimal FastAPI app with /health)
    - tests/ directory with a trivial test so pytest runs without error
    - .gitignore (Python/FastAPI patterns)
    - README.md, CONTRIBUTORS.md (blank)
    - Git repo with initial commit on main, development branch

    If ``requirements.txt`` and ``app/main.py`` already exist, scaffold creation
    is skipped but repo files (.gitignore, README, CONTRIBUTORS) are still
    ensured and committed on main if missing.

    Returns a :class:`CommandResult` indicating success or the first failure.
    """
    from shared.git_utils import ensure_files_committed_on_main, initialize_new_repo

    cwd = Path(backend_dir).resolve()
    requirements = cwd / "requirements.txt"
    main_py = cwd / "app" / "main.py"

    already_initialized = requirements.exists() and main_py.exists()
    if not already_initialized:
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
    else:
        logger.info("Backend project already initialized at %s", cwd)

    # Always ensure repo files exist
    _write_if_missing(cwd / ".gitignore", _PYTHON_GITIGNORE)
    _write_if_missing(cwd / "README.md", "")
    _write_if_missing(cwd / "CONTRIBUTORS.md", "")

    # Git: init and initial commit if no repo, else ensure files committed on main
    if not (cwd / ".git").exists():
        ok, msg = initialize_new_repo(cwd, gitignore_content=_PYTHON_GITIGNORE)
        if not ok:
            return CommandResult(success=False, exit_code=1, stdout="", stderr=msg)
    else:
        ok, msg = ensure_files_committed_on_main(
            cwd, [".gitignore", "README.md", "CONTRIBUTORS.md"]
        )
        if not ok:
            return CommandResult(success=False, exit_code=1, stdout="", stderr=msg)

    # Optional: install dependencies (non-blocking; CI/containers typically run pip install)
    try:
        result = run_command(
            [sys.executable, "-m", "pip", "install", "-r", "requirements.txt"],
            cwd=cwd,
            timeout=120,
        )
        if not result.success:
            logger.warning(
                "pip install failed (non-blocking): %s", result.error_summary
            )
    except Exception as e:
        logger.warning("pip install failed (non-blocking): %s", e)

    logger.info("Backend project initialized successfully at %s", cwd)
    return CommandResult(
        success=True,
        exit_code=0,
        stdout=f"Backend project initialized at {cwd}",
        stderr="",
    )
