"""
Resolve frontend framework from spec text, task metadata, or project files.

The system supports Angular, React, and Vue. Framework detection order:
1. Task metadata (explicit framework_target)
2. Existing project files (angular.json, package.json dependencies)
3. Spec content (mentions of framework names)
4. No default - returns None if no framework is detected

Callers should handle None appropriately (e.g., ask user or use their own default).
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Optional

# Word-boundary patterns for framework names (case-insensitive)
_REACT_PATTERN = re.compile(
    r"\b(?:react(?:\s+(?:app|application|frontend|ui|framework))?|use\s+react)\b",
    re.IGNORECASE,
)
_VUE_PATTERN = re.compile(
    r"\b(?:vue(?:\s*(?:\.?\s*js|3)?|\s+(?:app|application|frontend|framework))?|use\s+vue)\b",
    re.IGNORECASE,
)
_ANGULAR_PATTERN = re.compile(
    r"\b(?:angular(?:\s+(?:app|application|frontend|ui|framework))?|use\s+angular)\b",
    re.IGNORECASE,
)

# Directories excluded when scanning for framework marker files, so a file shipped
# inside a dependency or build output doesn't misclassify the whole project.
_IGNORED_MARKER_DIRS = frozenset({"node_modules", "dist", "build", ".git"})


def _has_first_party_marker(repo_path: Path, suffix: str) -> bool:
    """True if a file whose name ends with `suffix` exists outside dependency/build
    directories.

    Prunes ignored directories during the walk (rather than post-filtering matches
    from Path.rglob) so large or unreadable dependency trees are never descended
    into; os.walk silently skips a directory it can't list rather than raising.
    """
    for _dirpath, dirnames, filenames in os.walk(repo_path):
        dirnames[:] = [d for d in dirnames if d not in _IGNORED_MARKER_DIRS]
        if any(name.endswith(suffix) for name in filenames):
            return True
    return False


def detect_framework_from_project(repo_path: Optional[Path]) -> Optional[str]:
    """
    Detect frontend framework from existing project files.

    Checks for:
    - angular.json -> Angular
    - package.json with @angular/core or @angular/common -> Angular
    - package.json with react or react-dom -> React
    - package.json with vue -> Vue
    - vue.config.js (Vue CLI-specific) -> Vue
    - vite.config.ts or vite.config.js naming "@vitejs/plugin-vue" -> Vue
    - vite.config.ts or vite.config.js naming "@vitejs/plugin-react" -> React
    - vite.config.ts or vite.config.js with no plugin marker: falls back to a
      *.vue file -> Vue, else a *.jsx or *.tsx file -> React. Vite is
      framework-agnostic and Vue also supports JSX/TSX, so this fallback is a
      best-effort guess used only when the config doesn't name its plugin. The
      file-marker scans ignore node_modules/dist/build/.git so a marker shipped
      inside a dependency or build artifact doesn't misclassify the project.

    An unreadable file or directory encountered while scanning for these markers
    is treated as "no framework detected" rather than raising.

    Returns "angular", "react", "vue", or None if not detected.
    """
    if not repo_path or not repo_path.is_dir():
        return None

    # Check for Angular-specific config file
    if (repo_path / "angular.json").exists():
        return "angular"

    # Check package.json for framework dependencies
    pkg_path = repo_path / "package.json"
    if pkg_path.exists():
        try:
            content = pkg_path.read_text(encoding="utf-8")
            pkg_data = json.loads(content)
            all_deps = {
                **pkg_data.get("dependencies", {}),
                **pkg_data.get("devDependencies", {}),
            }

            # Check for Angular
            if "@angular/core" in all_deps or "@angular/common" in all_deps:
                return "angular"

            # Check for React
            if "react" in all_deps or "react-dom" in all_deps:
                return "react"

            # Check for Vue
            if "vue" in all_deps:
                return "vue"
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            pass

    # vue.config.js is Vue CLI-specific (unlike Vite, no other framework uses it)
    if (repo_path / "vue.config.js").exists():
        return "vue"

    # vite.config.ts/js is framework-agnostic; prefer its named plugin, falling
    # back to file markers only when the config doesn't identify a plugin
    vite_config_path = repo_path / "vite.config.ts"
    if not vite_config_path.exists():
        vite_config_path = repo_path / "vite.config.js"
    if vite_config_path.exists():
        try:
            config_content = vite_config_path.read_text(encoding="utf-8")
            if "@vitejs/plugin-vue" in config_content:
                return "vue"
            if "@vitejs/plugin-react" in config_content:
                return "react"
            if _has_first_party_marker(repo_path, ".vue"):
                return "vue"
            if _has_first_party_marker(repo_path, ".jsx") or _has_first_party_marker(
                repo_path, ".tsx"
            ):
                return "react"
        except (OSError, UnicodeDecodeError):
            pass

    return None


def get_frontend_framework_from_spec(spec_content: str) -> Optional[str]:
    """
    Detect if the spec explicitly requires Angular, React, or Vue.

    Returns "angular", "react", "vue", or None. Uses word-boundary and phrase
    checks to avoid false positives (e.g. "reaction" does not set React).
    Scans the full spec content.
    """
    if not spec_content or not spec_content.strip():
        return None
    text = spec_content

    # Check for explicit framework mentions
    if _ANGULAR_PATTERN.search(text):
        return "angular"
    if _REACT_PATTERN.search(text):
        return "react"
    if _VUE_PATTERN.search(text):
        return "vue"
    return None


def resolve_frontend_framework(
    task_metadata: Optional[dict],
    spec_content: Optional[str],
    repo_path: Optional[Path] = None,
) -> Optional[str]:
    """
    Resolve framework in order: task metadata -> project files -> spec -> None.

    Returns a normalized value: "angular", "react", "vue", or None if not detected.
    Callers should handle None (e.g., by using their own default or prompting).
    """
    meta = task_metadata or {}
    from_meta = meta.get("framework_target")
    if from_meta:
        normalized = str(from_meta).lower().strip()
        if normalized in ("react", "vue", "angular"):
            return normalized

    # Check existing project files
    from_project = detect_framework_from_project(repo_path)
    if from_project:
        return from_project

    # Check spec content
    from_spec = get_frontend_framework_from_spec(spec_content or "")
    if from_spec:
        return from_spec

    return None
