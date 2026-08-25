"""
Frontend (Angular/React) and backend (FastAPI) project scaffolding.

Writes a minimal, working project skeleton — config files, entrypoints,
lint/test setup — so a freshly created project directory builds, lints, and
tests cleanly before any agent-generated code is added.
``ensure_backend_project_initialized`` writes (when missing) requirements.txt,
app/__init__.py, app/main.py, tests/ (with a trivial test and conftest.py),
pyproject.toml, Makefile, .gitignore, README.md, CONTRIBUTORS.md, and a docs/
folder; it additionally initializes a git repo (or commits any missing repo
files onto an existing one) and attempts a non-blocking
``pip install -r requirements.txt`` after scaffolding the files.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Callable, Optional

from shared.command_runner.executor import (
    ANGULAR_VERSION,
    BUILD_TIMEOUT,
    FRONTEND_NODE_VERSION,
    CommandResult,
    run_command,
)
from shared.command_runner.nvm import (
    _get_nvm_script_prefix,
    ensure_nvm_installed,
    run_command_with_nvm,
)

logger = logging.getLogger(__name__)

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
_ANGULAR_DEPS = [
    f"@angular/core@{ANGULAR_VERSION}",
    f"@angular/common@{ANGULAR_VERSION}",
    f"@angular/compiler@{ANGULAR_VERSION}",
    f"@angular/platform-browser@{ANGULAR_VERSION}",
    f"@angular/platform-browser-dynamic@{ANGULAR_VERSION}",
    f"@angular/router@{ANGULAR_VERSION}",
    f"@angular/forms@{ANGULAR_VERSION}",
    f"@angular/animations@{ANGULAR_VERSION}",
    f"@angular/material@{ANGULAR_VERSION}",
    f"@angular/cdk@{ANGULAR_VERSION}",
    "rxjs",
    "zone.js",
    "tslib",
]

_ANGULAR_DEV_DEPS = [
    f"@angular/cli@{ANGULAR_VERSION}",
    f"@angular/compiler-cli@{ANGULAR_VERSION}",
    f"@angular/build@{ANGULAR_VERSION}",
    "typescript",
    f"angular-eslint@{ANGULAR_VERSION}",
    "eslint@^9.0.0",
    "@eslint/js@^9.0.0",
    "typescript-eslint@^8.0.0",
    "vitest",
    "@vitest/coverage-v8",
    "jsdom",
]

# ---------------------------------------------------------------------------
# React project scaffolding
# ---------------------------------------------------------------------------

_REACT_DEPS = [
    "react",
    "react-dom",
    "react-router-dom",
    "@tanstack/react-query",
    "react-hook-form",
    "zod",
    "@hookform/resolvers",
]

_REACT_DEV_DEPS = [
    "typescript",
    "@types/react",
    "@types/react-dom",
    "vite",
    "@vitejs/plugin-react",
    "vitest",
    "@vitest/coverage-v8",
    "@testing-library/react",
    "@testing-library/jest-dom",
    "jsdom",
    "eslint@^9.0.0",
    "@eslint/js@^9.0.0",
    "typescript-eslint@^8.0.0",
]

_MINIMAL_REACT_INDEX_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>App</title>
</head>
<body>
  <div id="root"></div>
  <script type="module" src="/src/main.tsx"></script>
</body>
</html>
"""

_MINIMAL_REACT_MAIN_TSX = """\
import React from 'react';
import ReactDOM from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import App from './App';
import './index.css';

const queryClient = new QueryClient();

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <App />
      </BrowserRouter>
    </QueryClientProvider>
  </React.StrictMode>
);
"""

_MINIMAL_REACT_APP_TSX = """\
import { Routes, Route } from 'react-router-dom';

function App() {
  return (
    <div className="app">
      <Routes>
        <Route path="/" element={<div>Welcome</div>} />
      </Routes>
    </div>
  );
}

export default App;
"""

_MINIMAL_REACT_INDEX_CSS = """\
* {
  box-sizing: border-box;
  margin: 0;
  padding: 0;
}

body {
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, sans-serif;
  line-height: 1.5;
}

.app {
  min-height: 100vh;
}
"""

_MINIMAL_REACT_ESLINT_CONFIG = """\
import js from '@eslint/js';
import tseslint from 'typescript-eslint';

export default tseslint.config(
  js.configs.recommended,
  ...tseslint.configs.recommended,
  {
    rules: {
      '@typescript-eslint/no-explicit-any': 'warn',
      '@typescript-eslint/no-unused-vars': ['warn', { argsIgnorePattern: '^_' }],
    },
  },
  {
    ignores: ['dist/', 'node_modules/', 'coverage/'],
  },
);
"""

_MINIMAL_REACT_VITEST_CONFIG = """\
import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  test: {
    globals: true,
    environment: 'jsdom',
    include: ['src/**/*.{test,spec}.{ts,tsx}'],
    coverage: {
      provider: 'v8',
      reporter: ['text', 'lcov'],
      thresholds: { lines: 60 },
      exclude: ['**/*.test.*', '**/*.spec.*', 'src/main.tsx'],
    },
  },
});
"""

_MINIMAL_ANGULAR_ESLINT_CONFIG = """\
// @ts-check
const eslint = require("@eslint/js");
const tseslint = require("typescript-eslint");
const angular = require("angular-eslint");

module.exports = tseslint.config(
  {
    files: ["**/*.ts"],
    extends: [
      eslint.configs.recommended,
      ...tseslint.configs.recommended,
      ...tseslint.configs.stylistic,
      ...angular.configs.tsRecommended,
    ],
    processor: angular.processInlineTemplates,
    rules: {
      "@angular-eslint/directive-selector": ["error", { type: "attribute", prefix: "app", style: "camelCase" }],
      "@angular-eslint/component-selector": ["error", { type: "element", prefix: "app", style: "kebab-case" }],
    },
  },
  {
    files: ["**/*.html"],
    extends: [...angular.configs.templateRecommended, ...angular.configs.templateAccessibility],
  },
);
"""

_MINIMAL_ANGULAR_VITEST_CONFIG = """\
import { defineConfig } from 'vitest/config';

export default defineConfig({
  test: {
    globals: true,
    environment: 'jsdom',
    include: ['src/**/*.spec.ts'],
    setupFiles: ['src/test-setup.ts'],
    pool: 'forks',
    coverage: {
      provider: 'v8',
      reporter: ['text', 'lcov'],
      thresholds: { lines: 60 },
      exclude: ['**/*.spec.ts', '**/*.model.ts', 'src/environments/**', 'src/main.ts'],
    },
  },
});
"""

_MINIMAL_ANGULAR_TEST_SETUP = """\
import 'zone.js';
import 'zone.js/testing';
import { getTestBed } from '@angular/core/testing';
import { BrowserDynamicTestingModule, platformBrowserDynamicTesting } from '@angular/platform-browser-dynamic/testing';

getTestBed().initTestEnvironment(BrowserDynamicTestingModule, platformBrowserDynamicTesting());
"""

# Public aliases: codegen_team's frontend-stack Setup phase (a consumer outside
# this package) writes these same lint/test config templates into agent-generated
# projects when they're missing, so they need a non-underscore, importable name.
MINIMAL_REACT_ESLINT_CONFIG = _MINIMAL_REACT_ESLINT_CONFIG
MINIMAL_REACT_VITEST_CONFIG = _MINIMAL_REACT_VITEST_CONFIG
MINIMAL_ANGULAR_ESLINT_CONFIG = _MINIMAL_ANGULAR_ESLINT_CONFIG
MINIMAL_ANGULAR_VITEST_CONFIG = _MINIMAL_ANGULAR_VITEST_CONFIG
MINIMAL_ANGULAR_TEST_SETUP = _MINIMAL_ANGULAR_TEST_SETUP

_MINIMAL_REACT_VITE_CONFIG = """\
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  server: {
    port: 3000,
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
});
"""

_MINIMAL_REACT_TSCONFIG = """\
{
  "compilerOptions": {
    "target": "ES2020",
    "useDefineForClassFields": true,
    "lib": ["ES2020", "DOM", "DOM.Iterable"],
    "module": "ESNext",
    "skipLibCheck": true,
    "moduleResolution": "bundler",
    "allowImportingTsExtensions": true,
    "resolveJsonModule": true,
    "isolatedModules": true,
    "noEmit": true,
    "jsx": "react-jsx",
    "strict": true,
    "noUnusedLocals": true,
    "noUnusedParameters": true,
    "noFallthroughCasesInSwitch": true
  },
  "include": ["src"],
  "references": [{ "path": "./tsconfig.node.json" }]
}
"""

_MINIMAL_REACT_TSCONFIG_NODE = """\
{
  "compilerOptions": {
    "composite": true,
    "skipLibCheck": true,
    "module": "ESNext",
    "moduleResolution": "bundler",
    "allowSyntheticDefaultImports": true
  },
  "include": ["vite.config.ts"]
}
"""

_MINIMAL_REACT_CONFIG_TS = """\
export const config = {
  apiUrl: import.meta.env.VITE_API_URL || 'http://localhost:8000',
  production: import.meta.env.PROD,
};
"""


def ensure_frontend_project_initialized(  # pragma: no cover  # integration-only: scaffolds real Angular/React project via npm
    project_dir: str | Path,
    framework: Optional[str] = None,
) -> CommandResult:
    """Ensure a minimal frontend project exists at *project_dir*.

    If ``package.json`` already exists the function is a no-op.
    Otherwise it:
    1. Creates the directory (if needed)
    2. Runs ``npm init -y``
    3. Installs framework-specific runtime and dev dependencies
    4. Writes minimal config and scaffold files

    Args:
        project_dir: Path to the frontend project directory
        framework: "angular", "react", or None (defaults to "react" for new projects)

    Returns a :class:`CommandResult` indicating success or the first failure.
    """
    cwd = Path(project_dir).resolve()
    pkg_json = cwd / "package.json"

    if pkg_json.exists():
        logger.info("Frontend project already initialized at %s", cwd)
        return CommandResult(success=True, exit_code=0, stdout="Already initialized", stderr="")

    # Default to React for new projects (more commonly requested)
    target_framework = framework or "react"
    logger.info("Initializing new %s project at %s", target_framework, cwd)
    cwd.mkdir(parents=True, exist_ok=True)

    nvm_result = ensure_nvm_installed()
    if not nvm_result.success:
        logger.warning(
            "NVM install failed or unavailable: %s; frontend may need a specific Node version",
            nvm_result.stderr or "unknown",
        )
    use_nvm = _get_nvm_script_prefix() is not None
    if use_nvm:  # pragma: no cover
        logger.info("Using NVM (node %s) for frontend project init", FRONTEND_NODE_VERSION)

    # Step 1: npm init
    if use_nvm:  # pragma: no cover
        result = run_command_with_nvm(
            ["npm", "init", "-y"], cwd=cwd, node_version=FRONTEND_NODE_VERSION, timeout=30
        )
    else:
        result = run_command(["npm", "init", "-y"], cwd=cwd, timeout=30)
    if not result.success:
        return result

    # Select dependencies based on framework
    if target_framework == "angular":
        runtime_deps = _ANGULAR_DEPS
        dev_deps = _ANGULAR_DEV_DEPS
    else:
        runtime_deps = _REACT_DEPS
        dev_deps = _REACT_DEV_DEPS

    # Step 2: Install runtime dependencies
    install_cmd = ["npm", "install", "--save"] + runtime_deps
    if use_nvm:  # pragma: no cover
        result = run_command_with_nvm(
            install_cmd, cwd=cwd, node_version=FRONTEND_NODE_VERSION, timeout=BUILD_TIMEOUT
        )
    else:
        result = run_command(install_cmd, cwd=cwd, timeout=BUILD_TIMEOUT)
    if not result.success:
        return result

    # Step 3: Install dev dependencies
    dev_install_cmd = ["npm", "install", "--save-dev"] + dev_deps
    if use_nvm:  # pragma: no cover
        result = run_command_with_nvm(
            dev_install_cmd, cwd=cwd, node_version=FRONTEND_NODE_VERSION, timeout=BUILD_TIMEOUT
        )
    else:
        result = run_command(dev_install_cmd, cwd=cwd, timeout=BUILD_TIMEOUT)
    if not result.success:
        return result

    # Framework-specific scaffolding
    if target_framework == "angular":
        return _scaffold_angular_project(cwd)
    else:
        return _scaffold_react_project(cwd)


def _scaffold_angular_project(
    cwd: Path,
) -> CommandResult:  # pragma: no cover  # integration-only: runs real `ng new` scaffolding
    """Write Angular-specific config and scaffold files."""
    # Write config files (only if they don't already exist)
    _write_if_missing(cwd / "angular.json", _MINIMAL_ANGULAR_JSON)
    _write_if_missing(cwd / "tsconfig.json", _MINIMAL_TSCONFIG)

    # Write minimal scaffold files
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

    # Environment files for API base URL
    env_dir = src / "environments"
    env_dir.mkdir(parents=True, exist_ok=True)
    _write_if_missing(env_dir / "environment.ts", _MINIMAL_ENVIRONMENT_TS)
    _write_if_missing(env_dir / "environment.prod.ts", _MINIMAL_ENVIRONMENT_PROD_TS)

    # Pin Node version for nvm use
    _write_if_missing(cwd / ".nvmrc", FRONTEND_NODE_VERSION + "\n")

    # Linting and testing configuration
    _write_if_missing(cwd / "eslint.config.js", _MINIMAL_ANGULAR_ESLINT_CONFIG)
    _write_if_missing(cwd / "vitest.config.mts", _MINIMAL_ANGULAR_VITEST_CONFIG)
    _write_if_missing(src / "test-setup.ts", _MINIMAL_ANGULAR_TEST_SETUP)

    # Minimal test file so vitest runs without error
    _write_if_missing(
        app / "app.component.spec.ts",
        "import { describe, it, expect } from 'vitest';\n\n"
        "describe('AppComponent', () => {\n"
        "  it('should be defined', () => {\n"
        "    expect(true).toBe(true);\n"
        "  });\n"
        "});\n",
    )

    # Create docs folder for documentation
    _ensure_docs_folder(cwd)

    # Update package.json with lint/test scripts (preserve any existing ones)
    def _add_angular_scripts(pkg_data: dict) -> None:
        scripts = pkg_data.get("scripts", {})
        scripts.setdefault("lint", "npx ng lint")
        scripts.setdefault("test", "vitest run")
        scripts.setdefault("test:coverage", "vitest run --coverage")
        pkg_data["scripts"] = scripts

    _update_package_json_scripts(cwd, _add_angular_scripts)

    logger.info("Angular project initialized successfully at %s", cwd)
    return CommandResult(
        success=True,
        exit_code=0,
        stdout=f"Angular project initialized at {cwd}",
        stderr="",
    )


def _scaffold_react_project(
    cwd: Path,
) -> CommandResult:  # pragma: no cover  # integration-only: runs real `npm create vite` scaffolding
    """Write React-specific config and scaffold files."""
    # Write config files (only if they don't already exist)
    _write_if_missing(cwd / "vite.config.ts", _MINIMAL_REACT_VITE_CONFIG)
    _write_if_missing(cwd / "tsconfig.json", _MINIMAL_REACT_TSCONFIG)
    _write_if_missing(cwd / "tsconfig.node.json", _MINIMAL_REACT_TSCONFIG_NODE)
    _write_if_missing(cwd / "index.html", _MINIMAL_REACT_INDEX_HTML)

    # Write minimal scaffold files
    src = cwd / "src"
    src.mkdir(parents=True, exist_ok=True)

    _write_if_missing(src / "main.tsx", _MINIMAL_REACT_MAIN_TSX)
    _write_if_missing(src / "App.tsx", _MINIMAL_REACT_APP_TSX)
    _write_if_missing(src / "index.css", _MINIMAL_REACT_INDEX_CSS)
    _write_if_missing(src / "config.ts", _MINIMAL_REACT_CONFIG_TS)
    _write_if_missing(src / "vite-env.d.ts", '/// <reference types="vite/client" />\n')

    # Components and hooks directories
    (src / "components").mkdir(parents=True, exist_ok=True)
    (src / "hooks").mkdir(parents=True, exist_ok=True)
    (src / "services").mkdir(parents=True, exist_ok=True)
    (src / "types").mkdir(parents=True, exist_ok=True)

    # Create docs folder for documentation
    _ensure_docs_folder(cwd)

    # Pin Node version for nvm use
    _write_if_missing(cwd / ".nvmrc", FRONTEND_NODE_VERSION + "\n")

    # Linting and testing configuration
    _write_if_missing(cwd / "eslint.config.mjs", _MINIMAL_REACT_ESLINT_CONFIG)
    _write_if_missing(cwd / "vitest.config.ts", _MINIMAL_REACT_VITEST_CONFIG)

    # Minimal test file so vitest runs without error
    _write_if_missing(
        src / "App.test.tsx",
        "import { describe, it, expect } from 'vitest';\n\n"
        "describe('App', () => {\n"
        "  it('should be defined', () => {\n"
        "    expect(true).toBe(true);\n"
        "  });\n"
        "});\n",
    )

    # Update package.json with scripts (wholesale replace, unlike Angular's merge)
    def _set_react_scripts(pkg_data: dict) -> None:
        pkg_data["scripts"] = {
            "dev": "vite",
            "build": "tsc && vite build",
            "preview": "vite preview",
            "test": "vitest run",
            "test:coverage": "vitest run --coverage",
            "lint": "eslint .",
        }

    _update_package_json_scripts(cwd, _set_react_scripts)

    logger.info("React project initialized successfully at %s", cwd)
    return CommandResult(
        success=True,
        exit_code=0,
        stdout=f"React project initialized at {cwd}",
        stderr="",
    )


def _write_if_missing(filepath: Path, content: str) -> None:
    """Write *content* to *filepath* only if the file does not yet exist."""
    if not filepath.exists():
        filepath.parent.mkdir(parents=True, exist_ok=True)
        filepath.write_text(content, encoding="utf-8")
        logger.info("Created %s", filepath)


def _ensure_docs_folder(cwd: Path) -> None:
    """Create ``cwd/docs`` with a placeholder ``.gitkeep`` if it doesn't exist yet."""
    docs_dir = cwd / "docs"
    if not docs_dir.exists():
        docs_dir.mkdir(parents=True, exist_ok=True)
        _write_if_missing(docs_dir / ".gitkeep", "")


def _update_package_json_scripts(cwd: Path, mutate: Callable[[dict], None]) -> None:
    """Read ``cwd/package.json``, apply *mutate* to its parsed contents, and write it back.

    Preconditions: *mutate* mutates its dict argument in place (typically
        ``data["scripts"]``); it does not need to return anything.
    Postconditions: no-op if ``package.json`` doesn't exist. Best-effort — any
        read/parse/write error is logged and swallowed, matching the rest of
        this module's scaffolding helpers.
    """
    pkg_path = cwd / "package.json"
    if not pkg_path.exists():
        return
    try:  # pragma: no cover  # integration-only: exercised only when a real npm init wrote package.json
        pkg_data = json.loads(pkg_path.read_text(encoding="utf-8"))
        mutate(pkg_data)
        pkg_path.write_text(json.dumps(pkg_data, indent=2), encoding="utf-8")
    except Exception as e:  # pragma: no cover  # integration-only: see try above
        logger.warning("Could not update package.json scripts: %s", e)


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
ruff>=0.4.0
pytest>=8.0,<9.0
pytest-cov>=5.0,<6.0
"""

_MINIMAL_PYPROJECT_TOML = """\
[project]
name = "app"
version = "0.1.0"
requires-python = ">=3.10"

[tool.ruff]
target-version = "py310"
line-length = 120
src = ["."]

[tool.ruff.lint]
select = ["E", "F", "I", "N", "W", "UP", "B", "SIM"]
ignore = ["E501"]

[tool.ruff.format]
quote-style = "double"
indent-style = "space"

[tool.pytest.ini_options]
addopts = "-v"
testpaths = ["tests"]
norecursedirs = [".git", "__pycache__", "*.egg-info", ".venv", "venv", "node_modules"]

[tool.coverage.run]
source = ["app"]
omit = ["tests/*", "conftest.py", "__init__.py"]

[tool.coverage.report]
fail_under = 80
"""

_MINIMAL_MAKEFILE = """\
.PHONY: install install-dev lint lint-fix test coverage run

VENV ?= .venv
PYTHON ?= $(VENV)/bin/python
PIP ?= $(VENV)/bin/pip

install:
\t@python3 -m venv $(VENV) 2>/dev/null || true
\t$(PIP) install --upgrade pip
\t$(PIP) install -r requirements.txt

install-dev: install
\t$(PIP) install ruff pytest pytest-cov

lint:
\t$(PYTHON) -m ruff check .
\t$(PYTHON) -m ruff format --check .

lint-fix:
\t$(PYTHON) -m ruff check . --fix
\t$(PYTHON) -m ruff format .

test:
\t$(PYTHON) -m pytest -v

coverage:
\t$(PYTHON) -m pytest --cov=app --cov-report=term-missing --cov-fail-under=80

run:
\tuvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
"""

_MINIMAL_APP_MAIN_PY = """\"\"\"FastAPI application entry point.\"\"\"
from fastapi import FastAPI

app = FastAPI(title="API", version="0.1.0")


@app.get("/health")
def health():
    return {"status": "ok"}
"""


def ensure_backend_project_initialized(  # pragma: no cover  # integration-only: scaffolds a Python project on disk
    backend_dir: str | Path,
) -> CommandResult:  # pragma: no cover
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
    from shared.git.git_utils import (
        ensure_files_committed_on_main,
        initialize_new_repo,
    )

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
        _write_if_missing(
            tests_dir / "conftest.py",
            '"""Shared test fixtures."""\n',
        )
    else:
        logger.info("Backend project already initialized at %s", cwd)

    # Always ensure linting and testing configuration exists
    _write_if_missing(cwd / "pyproject.toml", _MINIMAL_PYPROJECT_TOML)
    _write_if_missing(cwd / "Makefile", _MINIMAL_MAKEFILE)

    # Always ensure repo files exist
    _write_if_missing(cwd / ".gitignore", _PYTHON_GITIGNORE)
    _write_if_missing(cwd / "README.md", "")
    _write_if_missing(cwd / "CONTRIBUTORS.md", "")
    # Create docs folder for documentation
    docs_dir = cwd / "docs"
    if not docs_dir.exists():
        docs_dir.mkdir(parents=True, exist_ok=True)
        _write_if_missing(docs_dir / ".gitkeep", "")

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
            logger.warning("pip install failed (non-blocking): %s", result.error_summary)
    except Exception as e:
        logger.warning("pip install failed (non-blocking): %s", e)

    logger.info("Backend project initialized successfully at %s", cwd)
    return CommandResult(
        success=True,
        exit_code=0,
        stdout=f"Backend project initialized at {cwd}",
        stderr="",
    )
