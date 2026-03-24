"""Load repository content into a structured context for SOC2 audit agents."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import List, Set

from .models import RepoContext

logger = logging.getLogger(__name__)

# File extensions we consider relevant for SOC2 audit (code, config, infra, docs)
_CODE_EXTENSIONS = {".py", ".ts", ".tsx", ".js", ".jsx", ".java", ".go", ".rb", ".php", ".cs"}
_CONFIG_EXTENSIONS = {".yml", ".yaml", ".json", ".toml", ".env", ".ini", ".cfg", ".conf"}
_DOC_EXTENSIONS = {".md", ".rst", ".txt"}
_RELEVANT_EXTENSIONS = _CODE_EXTENSIONS | _CONFIG_EXTENSIONS | _DOC_EXTENSIONS

# Directories to skip when scanning
_SKIP_DIRS = {
    ".git",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    "dist",
    "build",
    ".angular",
    "vendor",
}


def _should_skip(path: Path, repo_root: Path) -> bool:
    """Return True if this path should be excluded from the audit context."""
    rel = path.relative_to(repo_root)
    for part in rel.parts:
        if part in _SKIP_DIRS or part.startswith(".") and part != ".env":
            return True
    return False


def load_repo_context(repo_path: str | Path) -> RepoContext:
    """
    Scan the repository and build a RepoContext with code/config/docs content
    suitable for SOC2 audit agents. Truncates if content exceeds limits.
    """
    root = Path(repo_path).resolve()
    if not root.is_dir():
        raise ValueError(f"Repository path is not a directory: {repo_path}")

    file_list: List[str] = []
    code_parts: List[str] = []
    readme_content = ""

    for f in root.rglob("*"):
        if not f.is_file():
            continue
        if _should_skip(f, root):
            continue
        if f.suffix.lower() not in _RELEVANT_EXTENSIONS:
            continue

        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            logger.debug("Skip reading %s: %s", f, e)
            continue

        rel_path = str(f.relative_to(root))
        file_list.append(rel_path)

        # Prefer README at root for overview
        if rel_path.lower().startswith("readme") and not readme_content:
            readme_content = text

        # Add to code summary with a header
        if f.suffix.lower() in _CODE_EXTENSIONS | _CONFIG_EXTENSIONS:
            code_parts.append(f"### {rel_path} ###\n{text}")

    code_summary = (
        "\n\n".join(code_parts) if code_parts else "# No relevant code or config files found."
    )
    file_list = sorted(file_list)

    # Infer tech stack from extensions
    exts: Set[str] = set()
    for p in file_list:
        if "." in p:
            exts.add(Path(p).suffix.lower())
    hints = []
    if ".py" in exts:
        hints.append("Python")
    if ".ts" in exts or ".tsx" in exts:
        hints.append("TypeScript")
    if ".java" in exts:
        hints.append("Java")
    if ".yml" in exts or ".yaml" in exts:
        hints.append("YAML config")
    if ".json" in exts:
        hints.append("JSON config")
    tech_stack_hint = ", ".join(hints) if hints else "Unknown"

    return RepoContext(
        repo_path=str(root),
        code_summary=code_summary,
        readme_content=readme_content,
        file_list=file_list,
        tech_stack_hint=tech_stack_hint,
    )
