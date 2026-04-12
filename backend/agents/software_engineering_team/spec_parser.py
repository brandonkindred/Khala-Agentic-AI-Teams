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
    ".md",
    ".txt",
    ".rst",
    ".adoc",
    # Config files
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".cfg",
    # Code samples/examples that might be part of spec
    ".py",
    ".js",
    ".ts",
    ".jsx",
    ".tsx",
    ".java",
    ".go",
    ".rs",
    ".html",
    ".css",
    ".scss",
    ".sql",
    # Data/schema files
    ".csv",
    ".xml",
    ".graphql",
    ".proto",
}

# Files/directories to exclude from context gathering
CONTEXT_EXCLUDE_PATTERNS = {
    # Hidden directories
    ".git",
    ".svn",
    ".hg",
    # Build/dependency directories
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    "env",
    "dist",
    "build",
    "target",
    ".next",
    ".nuxt",
    # IDE/editor
    ".idea",
    ".vscode",
    ".cursor",
    # Cache/temp
    ".cache",
    ".tmp",
    "tmp",
    "temp",
    # Plan directory (created by agents)
    "plan",
}

# Maximum file size to include (in bytes) - 100KB
MAX_CONTEXT_FILE_SIZE = 100 * 1024

# Maximum total context size (in chars) - 500KB
MAX_TOTAL_CONTEXT_SIZE = 500 * 1024


def parse_spec_with_llm(spec_content: str, llm_client=None) -> ProductRequirements:
    """
    Use LLM to extract structured ProductRequirements from spec content.
    """
    import json as _json

    from strands import Agent
    from strands.models.model import Model as _StrandsModel

    if llm_client is not None and isinstance(llm_client, _StrandsModel):
        model = llm_client
    else:
        from llm_service import get_strands_model

        model = get_strands_model("spec_intake")

    logger.info("Parsing spec with LLM (%s chars)", len(spec_content))
    system_prompt = """Parse the following software project specification into a structured format.

Return a single JSON object with:
- "title": string (project/feature name)
- "description": string (full description)
- "acceptance_criteria": list of strings (must-have requirements)
- "constraints": list of strings (technical/business constraints)
- "priority": string ("high", "medium", or "low")

Respond with valid JSON only. No explanatory text."""

    prompt = (
        "Parse this specification and return JSON with acceptance_criteria and constraints.\n"
        "Specification:\n---\n" + spec_content + "\n---"
    )

    agent = Agent(model=model, system_prompt=system_prompt)
    result = agent(prompt)
    raw = str(result).strip()
    data = _json.loads(raw)
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
    logger.info(
        "Parsed spec: title=%s, %s acceptance criteria", reqs.title, len(reqs.acceptance_criteria)
    )
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


_NO_SPEC_MESSAGE = (
    "No spec file found; looked for plan/product_analysis/*.md, plan/validated_spec.md, "
    "plan/updated_spec.md, plan/updated_spec_v*.md, "
    f"{SPEC_FILENAME}, spec.md"
)


def get_latest_spec_content(repo_path: str | Path) -> str:
    """
    Load the latest specification content from the repo, following PRA versioning.

    Precedence (first existing wins):
    0. plan/product_analysis/: validated_spec.md, updated_spec.md, updated_spec_vN.md (largest N)
    1. plan/: validated_spec.md, updated_spec.md, updated_spec_vN.md (largest N)
    2. initial_spec.md at repo root
    3. spec.md at repo root

    Raises FileNotFoundError if no candidate file exists.
    """
    path = Path(repo_path).resolve()
    if not path.exists() or not path.is_dir():
        raise FileNotFoundError(
            f"{_NO_SPEC_MESSAGE}. Repo path does not exist or is not a directory."
        )

    # 0. plan/product_analysis/ (PRA output) - first precedence
    product_analysis_dir = path / "plan" / "product_analysis"
    if product_analysis_dir.exists():
        candidate = product_analysis_dir / "validated_spec.md"
        if candidate.exists():
            return candidate.read_text(encoding="utf-8")
        candidate = product_analysis_dir / "updated_spec.md"
        if candidate.exists():
            return candidate.read_text(encoding="utf-8")
        versioned = list(product_analysis_dir.glob("updated_spec_v*.md"))
        if versioned:
            best: Optional[Tuple[int, Path]] = None
            for f in versioned:
                try:
                    n_str = f.stem.split("_v")[-1] if "_v" in f.stem else ""
                    n = int(n_str) if n_str.isdigit() else -1
                    if best is None or n > best[0]:
                        best = (n, f)
                except (ValueError, IndexError):
                    continue
            if best is not None:
                return best[1].read_text(encoding="utf-8")

    plan_dir = path / "plan"

    # 1. plan/validated_spec.md
    candidate = plan_dir / "validated_spec.md"
    if candidate.exists():
        return candidate.read_text(encoding="utf-8")

    # 2. plan/updated_spec.md
    candidate = plan_dir / "updated_spec.md"
    if candidate.exists():
        return candidate.read_text(encoding="utf-8")

    # 3. plan/updated_spec_vN.md with largest N
    versioned = list(plan_dir.glob("updated_spec_v*.md")) if plan_dir.exists() else []
    if versioned:
        best = None
        for f in versioned:
            try:
                n_str = f.stem.split("_v")[-1] if "_v" in f.stem else ""
                n = int(n_str) if n_str.isdigit() else -1
                if best is None or n > best[0]:
                    best = (n, f)
            except (ValueError, IndexError):
                continue
        if best is not None:
            return best[1].read_text(encoding="utf-8")

    # 4. initial_spec.md at root
    candidate = path / SPEC_FILENAME
    if candidate.exists():
        return candidate.read_text(encoding="utf-8")

    # 5. spec.md at root
    candidate = path / "spec.md"
    if candidate.exists():
        return candidate.read_text(encoding="utf-8")

    raise FileNotFoundError(f"{_NO_SPEC_MESSAGE} at {path}")


def get_latest_spec_path(repo_path: str | Path) -> Path:
    """
    Return the Path of the latest specification file in the repo (same precedence as get_latest_spec_content).

    Precedence (first existing wins):
    0. plan/product_analysis/: validated_spec.md, updated_spec.md, updated_spec_vN.md (largest N)
    1. plan/: validated_spec.md, updated_spec.md, updated_spec_vN.md (largest N)
    2. initial_spec.md at repo root
    3. spec.md at repo root

    Raises FileNotFoundError if no candidate file exists.
    """
    path = Path(repo_path).resolve()
    if not path.exists() or not path.is_dir():
        raise FileNotFoundError(
            f"{_NO_SPEC_MESSAGE}. Repo path does not exist or is not a directory."
        )

    # 0. plan/product_analysis/ (PRA output) - first precedence
    product_analysis_dir = path / "plan" / "product_analysis"
    if product_analysis_dir.exists():
        candidate = product_analysis_dir / "validated_spec.md"
        if candidate.exists():
            return candidate
        candidate = product_analysis_dir / "updated_spec.md"
        if candidate.exists():
            return candidate
        versioned = list(product_analysis_dir.glob("updated_spec_v*.md"))
        if versioned:
            best: Optional[Tuple[int, Path]] = None
            for f in versioned:
                try:
                    n_str = f.stem.split("_v")[-1] if "_v" in f.stem else ""
                    n = int(n_str) if n_str.isdigit() else -1
                    if best is None or n > best[0]:
                        best = (n, f)
                except (ValueError, IndexError):
                    continue
            if best is not None:
                return best[1]

    plan_dir = path / "plan"

    # 1. plan/validated_spec.md
    candidate = plan_dir / "validated_spec.md"
    if candidate.exists():
        return candidate

    # 2. plan/updated_spec.md
    candidate = plan_dir / "updated_spec.md"
    if candidate.exists():
        return candidate

    # 3. plan/updated_spec_vN.md with largest N
    versioned = list(plan_dir.glob("updated_spec_v*.md")) if plan_dir.exists() else []
    if versioned:
        best = None
        for f in versioned:
            try:
                n_str = f.stem.split("_v")[-1] if "_v" in f.stem else ""
                n = int(n_str) if n_str.isdigit() else -1
                if best is None or n > best[0]:
                    best = (n, f)
            except (ValueError, IndexError):
                continue
        if best is not None:
            return best[1]

    # 4. initial_spec.md at root
    candidate = path / SPEC_FILENAME
    if candidate.exists():
        return candidate

    # 5. spec.md at root
    candidate = path / "spec.md"
    if candidate.exists():
        return candidate

    raise FileNotFoundError(f"{_NO_SPEC_MESSAGE} at {path}")


def get_next_updated_spec_version(repo_path: str | Path) -> int:
    """
    Return the next version number to use for updated_spec_vN.md in plan/product_analysis/.

    Scans only plan/product_analysis/ for updated_spec_v*.md, parses N from each
    filename, and returns max(N) + 1. Returns 1 if the directory does not exist
    or no updated_spec_v*.md files are present. Malformed filenames (e.g. no
    numeric suffix) are ignored.
    """
    path = Path(repo_path).resolve()
    product_analysis_dir = path / "plan" / "product_analysis"
    if not product_analysis_dir.exists() or not product_analysis_dir.is_dir():
        return 1
    versioned = list(product_analysis_dir.glob("updated_spec_v*.md"))
    max_n = 0
    for f in versioned:
        try:
            n_str = f.stem.split("_v")[-1] if "_v" in f.stem else ""
            if n_str.isdigit():
                max_n = max(max_n, int(n_str))
        except (ValueError, IndexError):
            continue
    return max_n + 1


def get_newest_spec_path(repo_path: str | Path) -> Path:
    """
    Return the path of the newest specification file that has "_spec" in the name.

    Searches plan/product_analysis/, then plan/, then repo root for any .md file
    whose name contains "_spec" (e.g. validated_spec.md, updated_spec.md,
    updated_spec_vN.md, initial_spec.md) or is spec.md. Returns the file with the
    most recent modification time. If no such file exists, falls back to
    get_latest_spec_path (precedence order).

    Raises FileNotFoundError if no spec file exists.
    """
    path = Path(repo_path).resolve()
    if not path.exists() or not path.is_dir():
        raise FileNotFoundError(
            f"{_NO_SPEC_MESSAGE}. Repo path does not exist or is not a directory."
        )

    candidates: List[Path] = []
    for directory, pattern in [
        (path / "plan" / "product_analysis", "*spec*.md"),
        (path / "plan", "*spec*.md"),
    ]:
        if directory.exists() and directory.is_dir():
            candidates.extend(directory.glob(pattern))
    if (path / SPEC_FILENAME).exists():
        candidates.append(path / SPEC_FILENAME)
    if (path / "spec.md").exists():
        candidates.append(path / "spec.md")

    if not candidates:
        return get_latest_spec_path(path)

    newest: Optional[Tuple[float, Path]] = None
    for f in candidates:
        if not f.is_file():
            continue
        try:
            mtime = f.stat().st_mtime
            if newest is None or mtime > newest[0]:
                newest = (mtime, f)
        except OSError:
            continue
    if newest is not None:
        return newest[1]
    return get_latest_spec_path(path)


def get_newest_spec_content(repo_path: str | Path) -> str:
    """
    Load the content of the newest spec file (by mtime, name contains "_spec").
    Same search as get_newest_spec_path. Raises FileNotFoundError if no spec exists.
    """
    return get_newest_spec_path(repo_path).read_text(encoding="utf-8")


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
    Load the latest spec and gather all context files from the repository.

    Uses the same precedence as get_latest_spec_content: plan/product_analysis/
    (validated_spec.md, updated_spec.md, updated_spec_vN.md), then plan/, then
    root (initial_spec.md, spec.md). This provides the PRA agent with full
    visibility into all provided materials.

    Args:
        repo_path: Path to the repository root.

    Returns:
        Tuple of (spec_content, context_files_dict).

    Raises:
        FileNotFoundError: If no spec file is found.
    """
    spec_content = get_latest_spec_content(repo_path)
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
        raise ValueError(f"Path {path} is outside the allowed workspace root ({workspace_root})")


def validate_work_path(work_path: str | Path) -> Path:
    """
    Validate that the path exists, is a directory, and has at least one loadable spec.
    A spec can be at repo root (initial_spec.md or spec.md) or under plan/ or
    plan/product_analysis/ (e.g. validated_spec.md, updated_spec_vN.md).
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
    try:
        get_latest_spec_content(path)
    except FileNotFoundError as e:
        raise ValueError(str(e)) from e
    return path


def validate_repo_path(repo_path: str | Path) -> Path:
    """
    Validate that the path exists, is a directory, is a git repo, and has at least one loadable spec.
    A spec can be at repo root (initial_spec.md or spec.md) or under plan/ or
    plan/product_analysis/ (e.g. validated_spec.md, updated_spec_vN.md).
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
    try:
        get_latest_spec_content(path)
    except FileNotFoundError as e:
        raise ValueError(str(e)) from e
    return path
