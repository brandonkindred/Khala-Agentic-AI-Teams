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
import re
from pathlib import Path
from typing import Optional

# Scan first N chars of spec for framework mentions to avoid false positives in long docs
_SPEC_SCAN_CHARS = 16_000

# Word-boundary patterns for framework names (case-insensitive)
_REACT_PATTERN = re.compile(
    r"\b(?:react(?:\s+(?:app|application|frontend|ui|framework))?|use\s+react)\b",
    re.IGNORECASE,
)
_VUE_PATTERN = re.compile(
    r"\b(?:vue(?:\s*(?:\.?\s*js|3)?|(?:app|application|frontend|framework))?|use\s+vue)\b",
    re.IGNORECASE,
)
_ANGULAR_PATTERN = re.compile(
    r"\b(?:angular(?:\s+(?:app|application|frontend|ui|framework))?|use\s+angular)\b",
    re.IGNORECASE,
)


def detect_framework_from_project(repo_path: Optional[Path]) -> Optional[str]:
    """
    Detect frontend framework from existing project files.

    Checks for:
    - angular.json -> Angular
    - package.json with @angular/core -> Angular
    - package.json with react -> React
    - package.json with vue -> Vue

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
        except (json.JSONDecodeError, Exception):
            pass

    # Check for framework-specific files
    if (repo_path / "vue.config.js").exists() or (repo_path / "vite.config.ts").exists():
        # Could be React or Vue with Vite, check for Vue-specific markers
        for f in repo_path.rglob("*.vue"):
            return "vue"

    return None


def get_frontend_framework_from_spec(spec_content: str) -> Optional[str]:
    """
    Detect if the spec explicitly requires Angular, React, or Vue.

    Returns "angular", "react", "vue", or None. Uses word-boundary and phrase
    checks to avoid false positives (e.g. "reaction" does not set React).
    Scans the first _SPEC_SCAN_CHARS of the spec.
    """
    if not spec_content or not spec_content.strip():
        return None
    text = spec_content[:_SPEC_SCAN_CHARS]

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
