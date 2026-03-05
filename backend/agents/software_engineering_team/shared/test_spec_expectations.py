"""
Extract expectations from tests and specs before coding.

Agents use these to ensure generated code satisfies existing test imports,
route references, and spec invariants.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import List, Tuple


def _extract_python_imports(content: str) -> List[Tuple[str, str]]:
    """
    Extract (module, symbol) from Python import statements.

    Handles: from X import Y, from X import Y, Z
    Returns list of (module, symbol).
    """
    results: List[Tuple[str, str]] = []
    # from app.database import Base
    for m in re.finditer(r"from\s+([a-zA-Z0-9_.]+)\s+import\s+([a-zA-Z0-9_,\s]+)", content):
        module = m.group(1)
        symbols = [s.strip() for s in m.group(2).split(",")]
        for sym in symbols:
            if sym and not sym.startswith("#"):
                results.append((module, sym))
    return results


def extract_backend_test_expectations(repo_path: Path) -> str:
    """
    Scan backend tests/ for imports and derive a checklist.

    Returns a string like:
    - app.database must export Base
    - app.models must export Tenant, ApiToken, Task
    """
    tests_dir = repo_path / "tests"
    if not tests_dir.exists() or not tests_dir.is_dir():
        return ""

    imports: List[Tuple[str, str]] = []
    for f in tests_dir.rglob("test_*.py"):
        if not f.is_file():
            continue
        try:
            content = f.read_text(encoding="utf-8", errors="replace")
            imports.extend(_extract_python_imports(content))
        except Exception:
            pass

    if not imports:
        return ""

    # Group by module
    by_module: dict[str, set[str]] = {}
    for module, symbol in imports:
        if module.startswith("app.") or module == "app":
            by_module.setdefault(module, set()).add(symbol)

    lines = []
    for module in sorted(by_module.keys()):
        symbols = sorted(by_module[module])
        lines.append(f"- {module} must export: {', '.join(symbols)}")
    return "\n".join(lines) if lines else ""


def extract_frontend_route_expectations(repo_path: Path) -> str:
    """
    Scan app.routes.ts and route configs for component paths.

    Returns a checklist of paths that must exist (e.g. ./components/task-form/task-form.component).
    """
    routes_path = repo_path / "src" / "app" / "app.routes.ts"
    if not routes_path.exists():
        return ""

    try:
        content = routes_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""

    # Match: loadComponent: () => import('./components/foo/foo.component')
    # Match: import('./components/foo/foo.component')
    # Match: from './components/foo/foo.component'
    paths: List[str] = []
    for m in re.finditer(
        r"(?:import|loadComponent|from)\s*\(\s*['\"]([^'\"]+\.component)['\"]",
        content,
    ):
        path = m.group(1).strip()
        if path and path not in paths:
            paths.append(path)

    # Also: import { X } from './path'
    for m in re.finditer(r"from\s+['\"]([^'\"]+\.component)['\"]", content):
        path = m.group(1).strip()
        if path and path not in paths:
            paths.append(path)

    if not paths:
        return ""
    lines = [f"- Route/import path must exist: {p}" for p in sorted(paths)]
    return "\n".join(lines)


def build_test_spec_checklist(
    repo_path: Path,
    agent_type: str,
) -> str:
    """
    Build a checklist string for the agent based on tests and routes.

    agent_type: "backend" | "frontend"
    """
    path = Path(repo_path).resolve()
    parts: List[str] = []

    if agent_type == "backend":
        backend_expectations = extract_backend_test_expectations(path)
        if backend_expectations:
            parts.append("**Test expectations (from tests/):**")
            parts.append(backend_expectations)
            parts.append("")
            parts.append("Ensure your implementation satisfies these imports/exports.")

    elif agent_type == "frontend":
        frontend_expectations = extract_frontend_route_expectations(path)
        if frontend_expectations:
            parts.append("**Route/component expectations (from app.routes.ts):**")
            parts.append(frontend_expectations)
            parts.append("")
            parts.append("Ensure every referenced component path exists and exports the expected class.")

    return "\n".join(parts).strip() if parts else ""
