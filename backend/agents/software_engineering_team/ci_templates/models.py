"""Pydantic models for CI template parameters.

Preconditions:
    None — all fields have sensible defaults.
Postconditions:
    Constructed model is a valid, immutable configuration for CI template rendering.
"""

from __future__ import annotations

from pydantic import BaseModel


class BackendCIParams(BaseModel):
    """Parameters for rendering the backend CI workflow template.

    Invariants:
        python_version is a valid CPython version string (e.g. "3.10", "3.12").
        package_manager is one of "pip", "poetry", "pipenv".
    """

    python_version: str = "3.10"
    test_command: str = "pytest"
    lint_command: str = "ruff check ."
    format_check_command: str = "ruff format --check ."
    package_manager: str = "pip"
    install_command: str = "pip install -r requirements.txt"
    enable_sast: bool = True
    enable_sca: bool = True
    enable_secrets_scan: bool = True


class FrontendCIParams(BaseModel):
    """Parameters for rendering the frontend CI workflow template.

    Invariants:
        node_version is a valid Node.js major version string (e.g. "22", "20").
        package_manager is one of "npm", "yarn", "pnpm".
    """

    node_version: str = "22"
    package_manager: str = "npm"
    install_command: str = "npm ci"
    test_command: str = "npm test"
    lint_command: str = "npm run lint"
    build_command: str = "npm run build"
    enable_sca: bool = True
    enable_secrets_scan: bool = True
