"""
Angular-specific repair helpers.

Best-effort fixes applied to a generated/existing Angular project before a
build, so common agent-generated omissions (a missing @angular/common dep,
stale tsconfig moduleResolution, a missing Material theme import, etc.)
don't fail the build outright. Repair functions:
``_ensure_angular_common_in_package_json``, ``_ensure_angular_material_in_package_json``,
``_ensure_tsconfig_module_resolution``, ``_ensure_material_theme_in_styles``,
``_ensure_provide_animations_in_config``, ``_ensure_app_config_di_token_imports``,
``_ensure_reactive_forms_module_in_components``, ``_normalize_double_at_angular``.
"""

from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path

from shared.command_runner.nvm import _get_nvm_script_prefix, run_command_with_nvm
from shared.command_runner.runner import (
    ANGULAR_VERSION,
    BUILD_TIMEOUT,
    FRONTEND_NODE_VERSION,
    CommandResult,
    detect_frontend_framework,
    patch_json_file,
    patch_text_file,
    run_command,
)

logger = logging.getLogger(__name__)


def _insert_after_last_import(lines: list[str], new_line: str) -> None:
    """Insert *new_line* right after the last leading `import ` line in *lines*, in place.

    Blank lines and `//` comment lines interspersed among the leading imports
    don't end the scan early — only a real code line does.

    Preconditions: *lines* is a list of source lines (no trailing newlines).
    Postconditions: *lines* has *new_line* inserted after the last contiguous
        leading import line, or at index 0 if there are none.
    """
    insert_idx = 0
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("import "):
            insert_idx = i + 1
        elif insert_idx > 0 and stripped and not stripped.startswith("//"):
            break
    lines.insert(insert_idx, new_line.rstrip())


def run_ng_build_with_nvm_fallback(project_path: str | Path) -> CommandResult:  # pragma: no cover
    """
    Apply the best-effort Angular repair helpers to *project_path* — package.json
    deps (``@angular/common``, ``@angular/material``/``@angular/cdk``), tsconfig
    moduleResolution, the Material theme import, ``provideAnimations``, app.config.ts
    DI token imports, ``ReactiveFormsModule`` on components using formGroup, and
    ``@@angular`` typo normalization — then run ng build with the Node version
    Angular CLI needs. When NVM is available, use NVM to install and use
    FRONTEND_NODE_VERSION first. When NVM is not found, return an explicit failure
    instead of using system Node (which is often too old for Angular CLI).
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
        logger.info(
            "Running ng build with NVM (node %s). Next step -> Executing Angular build with version management",
            FRONTEND_NODE_VERSION,
        )
        return run_command_with_nvm(
            ["npx", "ng", "build", "--configuration=development"],
            cwd=cwd,
            node_version=FRONTEND_NODE_VERSION,
            timeout=BUILD_TIMEOUT,
        )
    logger.warning(
        "NVM not found. Recovery summary: 1) No NVM available, 2) Cannot guarantee correct Node version. "
        "Angular CLI requires Node v20.19+ or v22.12+."
    )
    msg = (
        "NVM not found. Angular CLI requires Node v20.19+ or v22.12+. "
        "Install NVM (https://github.com/nvm-sh/nvm) and run: nvm install 22"
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


def _ensure_angular_common_in_package_json(
    cwd: Path,
) -> None:  # pragma: no cover  # integration-only: rewrites real Angular package.json
    """
    Ensure package.json has @angular/common (provides @angular/common/http).
    Repairs projects where package.json was overwritten or is missing this dep.
    """

    def _transform(data: dict) -> bool:
        deps = data.setdefault("dependencies", {})
        if "@angular/common" in deps:
            return False
        deps["@angular/common"] = ANGULAR_VERSION
        return True

    if patch_json_file(cwd / "package.json", _transform):
        logger.info("Repaired package.json: added @angular/common for HttpClient support")


def _ensure_angular_material_in_package_json(
    cwd: Path,
) -> None:  # pragma: no cover  # integration-only: rewrites real Angular package.json
    """
    Ensure package.json has @angular/material and @angular/cdk.
    Repairs projects where package.json was overwritten or is missing these deps.
    """

    def _transform(data: dict) -> bool:
        deps = data.setdefault("dependencies", {})
        changed = False
        if "@angular/material" not in deps:
            deps["@angular/material"] = ANGULAR_VERSION
            changed = True
        if "@angular/cdk" not in deps:
            deps["@angular/cdk"] = ANGULAR_VERSION
            changed = True
        return changed

    if patch_json_file(cwd / "package.json", _transform):
        logger.info("Repaired package.json: added @angular/material and @angular/cdk")


def _ensure_tsconfig_module_resolution(
    cwd: Path,
) -> None:  # pragma: no cover  # integration-only: rewrites real tsconfig on disk
    """
    Ensure tsconfig.json uses moduleResolution 'bundler' for Angular 17+.
    Fixes 'Cannot find module @angular/common/http' when resolution was 'node'.
    """

    def _transform(data: dict) -> bool:
        opts = data.get("compilerOptions") or {}
        if opts.get("moduleResolution") != "node":
            return False
        opts["moduleResolution"] = "bundler"
        data["compilerOptions"] = opts
        return True

    if patch_json_file(cwd / "tsconfig.json", _transform):
        logger.info(
            "Repaired tsconfig.json: moduleResolution node -> bundler for Angular compatibility"
        )


def _ensure_material_theme_in_styles(
    cwd: Path,
) -> None:  # pragma: no cover  # integration-only: rewrites real Angular styles on disk
    """
    Ensure styles.scss (or styles.css) has a Material prebuilt theme import.
    Appends it at the top if missing. Required for Angular Material components.
    """

    def _transform(content: str) -> str:
        if "material" in content.lower() and (
            "prebuilt-themes" in content or "indigo-pink" in content
        ):
            return content
        theme_line = "@use '@angular/material/prebuilt-themes/indigo-pink.css';\n"
        return theme_line + content if content.strip() else theme_line

    for name in ("styles.scss", "styles.css"):
        styles_path = cwd / "src" / name
        if not styles_path.exists():
            continue
        if patch_text_file(styles_path, _transform):
            logger.info("Repaired %s: added Material prebuilt theme import", name)
        return


def _ensure_provide_animations_in_config(
    cwd: Path,
) -> None:  # pragma: no cover  # integration-only: rewrites real Angular app.config.ts
    """
    Ensure app.config.ts has provideAnimations in providers.
    Adds import and provider if missing. Required for Angular Material components.
    """

    def _transform(content: str) -> str:
        if "provideAnimations" in content:
            return content
        if "providers:" not in content:  # pragma: no cover
            return content
        import_line = "import { provideAnimations } from '@angular/platform-browser/animations';\n"
        lines = content.split("\n")
        _insert_after_last_import(lines, import_line)
        new_content = "\n".join(lines)
        if "provideAnimations()" not in new_content:
            if "provideHttpClient()," in new_content:
                new_content = new_content.replace(
                    "provideHttpClient(),",
                    "provideHttpClient(),\n    provideAnimations(),",
                )
            elif "provideRouter(routes)," in new_content:
                new_content = new_content.replace(
                    "provideRouter(routes),",
                    "provideRouter(routes),\n    provideAnimations(),",
                )
        # Only write when the provider was actually added; an orphan import with
        # no anchor to attach the provider to is left out (original behavior).
        return new_content if "provideAnimations()" in new_content else content

    config_path = cwd / "src" / "app" / "app.config.ts"
    if patch_text_file(config_path, _transform):
        logger.info("Repaired app.config.ts: added provideAnimations")


# Well-known Angular DI tokens that must be imported when used in app.config.ts
_APP_CONFIG_TOKEN_IMPORTS: dict[str, str] = {
    "HTTP_INTERCEPTORS": "@angular/common/http",
}


def _ensure_app_config_di_token_imports(
    cwd: Path,
) -> None:  # pragma: no cover  # integration-only: rewrites real Angular app.config.ts
    """
    Ensure app.config.ts imports any DI tokens it uses in providers.
    E.g. HTTP_INTERCEPTORS must be imported from @angular/common/http when
    used in { provide: HTTP_INTERCEPTORS, useClass: ..., multi: true }.
    """

    def _transform(content: str) -> str:
        for token, module in _APP_CONFIG_TOKEN_IMPORTS.items():
            if token not in content:
                continue
            # Already imported if token appears in an import from the expected module
            already_imported = re.search(
                r"import\s*\{[^}]*\b"
                + re.escape(token)
                + r"\b[^}]*\}\s*from\s*['\"]"
                + re.escape(module)
                + r"['\"]",
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
            else:
                lines = content.split("\n")
                import_line = f"import {{ {token} }} from '{module}';\n"
                _insert_after_last_import(lines, import_line)
                content = "\n".join(lines)
        return content

    config_path = cwd / "src" / "app" / "app.config.ts"
    if patch_text_file(config_path, _transform):
        logger.info("Repaired app.config.ts: ensured HTTP_INTERCEPTORS import")


def _normalize_double_at_angular(
    cwd: Path,
) -> None:  # pragma: no cover  # integration-only: scans real Angular tree
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
                new_content = content.replace("'@@angular", "'@angular").replace(
                    '"@@angular', '"@angular'
                )
                if new_content != content:
                    path.write_text(new_content, encoding="utf-8")
                    logger.info("Repaired %s: normalized @@angular to @angular", path.name)
            except Exception as e:
                logger.warning("Could not normalize @@angular in %s: %s", path.name, e)


def _ensure_reactive_forms_module_in_components(
    cwd: Path,
) -> None:  # pragma: no cover  # integration-only: rewrites real Angular component .ts files
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
            if (
                "formGroup" not in html_content
                and "formControlName" not in html_content
                and "formArrayName" not in html_content
            ):
                continue
            ts_path = html_path.with_suffix(".ts")
            if not ts_path.exists():
                continue
            ts_content = ts_path.read_text(encoding="utf-8")
            if "ReactiveFormsModule" in ts_content:
                continue
            if "import { ReactiveFormsModule }" not in ts_content:
                lines = ts_content.split("\n")
                _insert_after_last_import(
                    lines, "import { ReactiveFormsModule } from '@angular/forms';"
                )
                ts_content = "\n".join(lines)
            imports_match = re.search(r"imports:\s*\[", ts_content)
            if not imports_match:
                continue
            pos = imports_match.end()
            after_bracket = ts_content[pos : pos + 80]
            if "ReactiveFormsModule" in after_bracket or re.search(
                r"ReactiveFormsModule\s*[,\)]", ts_content[pos : pos + 200]
            ):
                continue
            ts_content = ts_content[:pos] + "ReactiveFormsModule,\n    " + ts_content[pos:]
            ts_path.write_text(ts_content, encoding="utf-8")
            logger.info("Repaired %s: added ReactiveFormsModule for formGroup", ts_path.name)
        except Exception as e:
            logger.warning("Could not repair %s for ReactiveFormsModule: %s", html_path.name, e)


def ensure_frontend_dependencies_installed(  # pragma: no cover  # integration-only: runs real `npm install`
    project_path: str | Path, framework: str = ""
) -> CommandResult:
    """
    Run npm install so dependencies are installed before the frontend agent runs.
    Uses NVM when available for consistent Node version. If package.json does not
    exist, returns success (no-op) so callers do not block.

    For Angular projects, also applies Angular-specific fixes (ensuring @angular/common, etc.).
    """
    cwd = Path(project_path).resolve()
    if not (cwd / "package.json").exists():
        return CommandResult(success=True, exit_code=0, stdout="", stderr="")

    detected_framework = framework or detect_frontend_framework(cwd)

    # Apply Angular-specific fixes only for Angular projects
    if detected_framework == "angular":
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
            node_version=FRONTEND_NODE_VERSION,
            timeout=BUILD_TIMEOUT,
        )
    return run_command(["npm", "install"], cwd=cwd, timeout=BUILD_TIMEOUT)
