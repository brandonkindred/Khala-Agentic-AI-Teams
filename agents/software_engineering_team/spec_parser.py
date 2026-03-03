"""
Parse initial_spec.md into ProductRequirements for the software engineering team.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from software_engineering_team.shared.models import ProductRequirements

logger = logging.getLogger(__name__)

SPEC_FILENAME = "initial_spec.md"

ENV_WORKSPACE_ROOT = "WORKSPACE_ROOT"

# File extensions to include when gathering context
CONTEXT_FILE_EXTENSIONS = {
    # Documentation
    ".md", ".txt", ".rst", ".adoc",
    # Config files
    ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg",
    # Code samples/examples that might be part of spec
    ".py", ".js", ".ts", ".jsx", ".tsx", ".java", ".go", ".rs",
    ".html", ".css", ".scss", ".sql",
    # Data/schema files
    ".csv", ".xml", ".graphql", ".proto",
}

# Files/directories to exclude from context gathering
CONTEXT_EXCLUDE_PATTERNS = {
    # Hidden directories
    ".git", ".svn", ".hg",
    # Build/dependency directories
    "node_modules", "__pycache__", ".venv", "venv", "env",
    "dist", "build", "target", ".next", ".nuxt",
    # IDE/editor
    ".idea", ".vscode", ".cursor",
    # Cache/temp
    ".cache", ".tmp", "tmp", "temp",
    # Plan directory (created by agents)
    "plan",
}

# Maximum file size to include (in bytes) - 100KB
MAX_CONTEXT_FILE_SIZE = 100 * 1024

# Maximum total context size (in chars) - 500KB
MAX_TOTAL_CONTEXT_SIZE = 500 * 1024


def parse_spec_with_llm(spec_content: str, llm_client) -> ProductRequirements:
    """
    Use LLM to extract structured ProductRequirements from spec content.
    """
    logger.info("Parsing spec with LLM (%s chars)", len(spec_content))
    prompt = """Parse the following software project specification into a structured format.

Return a single JSON object with:
- "title": string (project/feature name)
- "description": string (full description)
- "acceptance_criteria": list of strings (must-have requirements)
- "constraints": list of strings (technical/business constraints)
- "priority": string ("high", "medium", or "low")

Specification:
---
"""
    prompt += spec_content
    prompt += "\n---\n\nRespond with valid JSON only. No explanatory text."

    data = llm_client.complete_json(prompt, temperature=0.1)
    if not isinstance(data.get("acceptance_criteria"), list):
        raise ValueError(
            f"LLM returned invalid spec structure: 'acceptance_criteria' must be a list, got {type(data.get('acceptance_criteria'))}"
        )
    if not isinstance(data.get("constraints"), list):
        raise ValueError(
            f"LLM returned invalid spec structure: 'constraints' must be a list, got {type(data.get('constraints'))}"
        )
    reqs = ProductRequirements(
        title=data.get("title") or "Software Project",
        description=data.get("description") or spec_content[:2000],
        acceptance_criteria=data["acceptance_criteria"],
        constraints=data["constraints"],
        priority=data.get("priority") or "medium",
        metadata={"parsed_from": "initial_spec.md"},
    )
    logger.info("Parsed spec: title=%s, %s acceptance criteria", reqs.title, len(reqs.acceptance_criteria))
    return reqs


def load_spec_from_repo(repo_path: str | Path) -> str:
    """
    Load initial_spec.md from the root of the given path.
    Raises FileNotFoundError if not found.
    """
    path = Path(repo_path).resolve()
    spec_file = path / SPEC_FILENAME
    if not spec_file.exists():
        raise FileNotFoundError(f"{SPEC_FILENAME} not found at {spec_file}")
    return spec_file.read_text()


def _should_include_path(path: Path, base_path: Path) -> bool:
    """Check if a path should be included in context gathering."""
    # Check if any parent directory matches exclude patterns
    try:
        rel_parts = path.relative_to(base_path).parts
    except ValueError:
        return False
    
    for part in rel_parts:
        if part in CONTEXT_EXCLUDE_PATTERNS:
            return False
        if part.startswith("."):
            return False
    
    return True


def _read_file_safely(file_path: Path) -> Optional[str]:
    """Safely read a file, returning None if it fails or is too large."""
    try:
        if file_path.stat().st_size > MAX_CONTEXT_FILE_SIZE:
            logger.debug("Skipping large file: %s (>%d bytes)", file_path, MAX_CONTEXT_FILE_SIZE)
            return None
        
        content = file_path.read_text(encoding="utf-8", errors="replace")
        return content
    except Exception as e:
        logger.debug("Could not read file %s: %s", file_path, e)
        return None


def gather_context_files(repo_path: str | Path) -> Dict[str, str]:
    """
    Gather all relevant context files from the repository.
    
    Returns a dict mapping relative file paths to their content.
    Excludes initial_spec.md (handled separately), hidden files/dirs,
    and common non-relevant directories (node_modules, .git, etc.).
    
    Args:
        repo_path: Path to the repository root.
        
    Returns:
        Dict mapping relative path strings to file content.
    """
    path = Path(repo_path).resolve()
    context_files: Dict[str, str] = {}
    total_size = 0
    
    if not path.exists() or not path.is_dir():
        logger.warning("Context gathering: path does not exist or is not a directory: %s", path)
        return context_files
    
    for file_path in path.rglob("*"):
        # Skip directories
        if not file_path.is_file():
            continue
        
        # Skip the main spec file (handled separately)
        if file_path.name == SPEC_FILENAME:
            continue
        
        # Check extension
        if file_path.suffix.lower() not in CONTEXT_FILE_EXTENSIONS:
            continue
        
        # Check exclude patterns
        if not _should_include_path(file_path, path):
            continue
        
        # Read the file
        content = _read_file_safely(file_path)
        if content is None:
            continue
        
        # Check total size limit
        if total_size + len(content) > MAX_TOTAL_CONTEXT_SIZE:
            logger.info(
                "Context gathering: reached size limit (%d chars), stopping",
                MAX_TOTAL_CONTEXT_SIZE,
            )
            break
        
        rel_path = str(file_path.relative_to(path))
        context_files[rel_path] = content
        total_size += len(content)
    
    logger.info(
        "Gathered %d context files (%d chars total) from %s",
        len(context_files),
        total_size,
        path,
    )
    
    return context_files


def format_context_for_prompt(context_files: Dict[str, str]) -> str:
    """
    Format context files into a string suitable for inclusion in LLM prompts.
    
    Args:
        context_files: Dict mapping relative paths to file content.
        
    Returns:
        Formatted string with file contents.
    """
    if not context_files:
        return ""
    
    sections = []
    for file_path, content in sorted(context_files.items()):
        # Truncate very long files for the prompt
        truncated = content[:8000] if len(content) > 8000 else content
        suffix = f"\n... (truncated, {len(content)} total chars)" if len(content) > 8000 else ""
        
        sections.append(f"### File: {file_path}\n```\n{truncated}{suffix}\n```")
    
    return "\n\n".join(sections)


def load_spec_with_context(repo_path: str | Path) -> Tuple[str, Dict[str, str]]:
    """
    Load the initial_spec.md and gather all context files from the repository.
    
    This provides the PRA agent with full visibility into all provided materials.
    
    Args:
        repo_path: Path to the repository root.
        
    Returns:
        Tuple of (spec_content, context_files_dict).
        
    Raises:
        FileNotFoundError: If initial_spec.md is not found.
    """
    spec_content = load_spec_from_repo(repo_path)
    context_files = gather_context_files(repo_path)
    
    return spec_content, context_files


def _check_workspace_containment(path: Path) -> None:
    """Reject *path* if it escapes the configured workspace root.

    When ``WORKSPACE_ROOT`` is set, *path* (already resolved) must be equal to
    or a sub-directory of the workspace root.  This prevents path-traversal
    attacks where a caller supplies ``../../sensitive``.

    If ``WORKSPACE_ROOT`` is **not** set the check is a no-op so existing
    development workflows are unaffected.
    """
    workspace_root_str = os.environ.get(ENV_WORKSPACE_ROOT)
    if not workspace_root_str:
        return
    workspace_root = Path(workspace_root_str).resolve()
    try:
        path.relative_to(workspace_root)
    except ValueError:
        raise ValueError(
            f"Path {path} is outside the allowed workspace root ({workspace_root})"
        )


def validate_work_path(work_path: str | Path) -> Path:
    """
    Validate that the path exists, is a directory, and has initial_spec.md.
    Does not require the path to be a git repository.
    When WORKSPACE_ROOT is set, also verifies the path does not escape the
    workspace root (path-traversal protection).
    Returns the resolved Path. Raises ValueError on failure.
    """
    path = Path(work_path).resolve()
    _check_workspace_containment(path)
    if not path.exists():
        raise ValueError(f"Path does not exist: {path}")
    if not path.is_dir():
        raise ValueError(f"Path is not a directory: {path}")
    spec_file = path / SPEC_FILENAME
    if not spec_file.exists():
        raise ValueError(f"{SPEC_FILENAME} not found at {spec_file}")
    return path


def validate_repo_path(repo_path: str | Path) -> Path:
    """
    Validate that the path exists, is a directory, is a git repo, and has initial_spec.md.
    When WORKSPACE_ROOT is set, also verifies the path does not escape the
    workspace root (path-traversal protection).
    Returns the resolved Path. Raises ValueError on failure.
    """
    path = Path(repo_path).resolve()
    _check_workspace_containment(path)
    if not path.exists():
        raise ValueError(f"Path does not exist: {path}")
    if not path.is_dir():
        raise ValueError(f"Path is not a directory: {path}")
    if not (path / ".git").exists():
        raise ValueError(f"Path is not a git repository (no .git): {path}")
    spec_file = path / SPEC_FILENAME
    if not spec_file.exists():
        raise ValueError(f"{SPEC_FILENAME} not found in repo root at {spec_file}")
    return path
