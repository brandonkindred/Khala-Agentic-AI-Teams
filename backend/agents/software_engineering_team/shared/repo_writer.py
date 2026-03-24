"""
Repository writer: writes agent outputs to the git repo and commits.

Includes path validation to reject file paths that look like task descriptions
or don't follow expected project structure.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Tuple

from .git_utils import write_files_and_commit

logger = logging.getLogger(__name__)

# Distinct error message when agent produced no file changes (LLM returned empty files dict)
NO_FILES_TO_WRITE_MSG = "No files to write"

# Maximum length for any single path segment (directory or filename without extension)
MAX_SEGMENT_LENGTH = 30
# Test files (test_*.py) may have longer descriptive names; allow up to 60 chars
MAX_TEST_FILE_SEGMENT_LENGTH = 60

# Pattern that matches names with 4+ hyphenated words (likely a sentence, not a component name)
_SENTENCE_NAME_RE = re.compile(r"^[a-z]+-[a-z]+-[a-z]+-[a-z]+")

# Pattern that matches names with 4+ underscored words (sentence-like Python names)
_SENTENCE_NAME_SNAKE_RE = re.compile(r"^[a-z]+_[a-z]+_[a-z]+_[a-z]+_[a-z]+")

# Pattern that rejects segments starting with common verbs (task-description-as-name)
_VERB_PREFIX_RE = re.compile(
    r"^(implement|create|build|setup|configure|add|make|define|develop|write|design|establish)[_-]"
)

# Pattern that detects filler words embedded in path segments
_FILLER_WORD_RE = re.compile(r"[_-](the|that|with|using|which|for|and|a|an)[_-]")

# Well-known directory names that are always allowed regardless of other rules
_ALLOWED_DIRS = frozenset(
    {
        "src",
        "app",
        "lib",
        "tests",
        "test",
        "spec",
        "components",
        "services",
        "models",
        "schemas",
        "routers",
        "controllers",
        "guards",
        "pipes",
        "shared",
        "pages",
        "features",
        "assets",
        "styles",
        "environments",
        "infrastructure",
        "config",
        "utils",
        "helpers",
        "middleware",
        "interceptors",
        "directives",
        "modules",
        "repository",
        "main",
        "node_modules",
        "dist",
        "build",
    }
)


def _validate_paths(files: Dict[str, str], subdir: str = "") -> Tuple[Dict[str, str], List[str]]:
    """
    Validate file paths from agent output. Rejects paths with:
    - Segments longer than MAX_SEGMENT_LENGTH (likely task-description-as-name)
    - Segments that look like sentences (4+ hyphenated/underscored words)
    - Segments starting with verbs (implement-, create-, build-, etc.)
    - Segments containing filler words (-the-, -with-, -using-, etc.)
    - Empty file content

    Well-known directory names (src, app, components, etc.) are always allowed.

    Returns (validated_files, warnings).
    """
    validated: Dict[str, str] = {}
    warnings: List[str] = []

    for path, content in files.items():
        # Always allow repo root .gitignore (written by merge_gitignore_entries)
        if path == ".gitignore":
            if content and content.strip():
                validated[path] = content
            else:
                warnings.append("REJECTED: empty content for '.gitignore'")
            continue
        segments = path.split("/")
        bad = False
        for seg in segments:
            name_part = seg.split(".")[0]  # strip extension
            if not name_part:
                continue
            # Skip well-known directory names
            if name_part.lower() in _ALLOWED_DIRS:
                continue
            # Test files (test_*.py) may have longer descriptive names
            max_len = (
                MAX_TEST_FILE_SEGMENT_LENGTH
                if (name_part.startswith("test_") and seg.endswith(".py"))
                else MAX_SEGMENT_LENGTH
            )
            if len(name_part) > max_len:
                warnings.append(
                    f"REJECTED: path segment '{seg}' is {len(name_part)} chars "
                    f"(max {max_len}) - likely task description as name: '{path}'"
                )
                bad = True
                break
            if _SENTENCE_NAME_RE.match(name_part):
                warnings.append(
                    f"REJECTED: path segment '{seg}' looks like a sentence (4+ hyphenated words), "
                    f"not a proper component/module name: '{path}'"
                )
                bad = True
                break
            # Exempt test files from sentence-like snake pattern (e.g. test_task_crud_qa.py)
            if not (name_part.startswith("test_") and seg.endswith(".py")):
                if _SENTENCE_NAME_SNAKE_RE.match(name_part):
                    warnings.append(
                        f"REJECTED: path segment '{seg}' looks like a sentence (5+ underscored words), "
                        f"not a proper module name: '{path}'"
                    )
                    bad = True
                    break
            # Allow migration/version script names (e.g. add_task_indexes.py under alembic/versions or migrations/)
            if _VERB_PREFIX_RE.match(name_part) and not (
                seg.endswith(".py") and ("versions" in segments or "migrations" in segments)
            ):
                warnings.append(
                    f"REJECTED: path segment '{seg}' starts with a verb "
                    f"(task description as name): '{path}'"
                )
                bad = True
                break
            if _FILLER_WORD_RE.search(name_part):
                warnings.append(
                    f"REJECTED: path segment '{seg}' contains filler words "
                    f"(task description as name): '{path}'"
                )
                bad = True
                break
        if bad:
            continue

        if not content or not content.strip():
            warnings.append(f"REJECTED: empty content for '{path}'")
            continue

        validated[path] = content

    return validated, warnings


def _output_to_files_dict(output: Any, subdir: str = "") -> Dict[str, str]:
    """
    Convert agent output to { path: content } dict.
    Handles BackendOutput, FrontendOutput, DevOpsOutput, SecurityOutput, QAOutput.
    """
    prefix = f"{subdir}/" if subdir else ""
    files: Dict[str, str] = {}

    # files dict (Backend, Frontend)
    if hasattr(output, "files") and output.files:
        for path, content in output.files.items():
            files[f"{prefix}{path}"] = content

    # code (Backend, Frontend) - use as main file if no files dict
    if hasattr(output, "code") and output.code and not files:
        lang = getattr(output, "language", "python")
        ext = ".py" if lang == "python" else ".java"
        files[f"{prefix}main{ext}"] = output.code

    # tests (Backend)
    if hasattr(output, "tests") and output.tests:
        files[f"{prefix}tests/test_main.py"] = output.tests

    # DevOps
    if hasattr(output, "pipeline_yaml") and output.pipeline_yaml:
        files[f"{prefix}.github/workflows/ci.yml"] = output.pipeline_yaml
    if hasattr(output, "dockerfile") and output.dockerfile:
        files[f"{prefix}Dockerfile"] = output.dockerfile
    if hasattr(output, "docker_compose") and output.docker_compose:
        files[f"{prefix}docker-compose.yml"] = output.docker_compose
    if hasattr(output, "iac_content") and output.iac_content:
        files[f"{prefix}infrastructure/main.tf"] = output.iac_content
    if hasattr(output, "artifacts") and output.artifacts:
        for path, content in output.artifacts.items():
            files[f"{prefix}{path}"] = content

    # QA/Security fixed_code
    if hasattr(output, "fixed_code") and output.fixed_code:
        files[f"{prefix}fixes.py"] = output.fixed_code

    return files


def merge_gitignore_entries(repo_path: Path, new_entries: List[str]) -> Tuple[str, bool]:
    """
    Merge new gitignore patterns into existing repo root .gitignore.
    Preserves existing content and order; appends new patterns (deduplicated).
    Returns (full file content, True if content changed).
    """
    path = Path(repo_path).resolve()
    gitignore_file = path / ".gitignore"
    existing = gitignore_file.read_text(encoding="utf-8") if gitignore_file.exists() else ""
    existing_lines = existing.splitlines()
    # Normalize non-empty, non-comment lines for dedup
    seen = {
        line.strip() for line in existing_lines if line.strip() and not line.strip().startswith("#")
    }
    added: List[str] = []
    for entry in new_entries:
        e = entry.strip()
        if e and e not in seen:
            seen.add(e)
            added.append(e)
    if not added:
        return existing, False
    result_lines = list(existing_lines)
    if result_lines and result_lines[-1].strip():
        result_lines.append("")
    result_lines.append("# Build/install artifacts")
    result_lines.extend(added)
    return "\n".join(result_lines) + "\n", True


def write_agent_output(
    repo_path: str | Path,
    output: Any,
    subdir: str = "",
    commit_message: str | None = None,
) -> Tuple[bool, str]:
    """
    Write agent output to repo and commit.

    output: BackendOutput, FrontendOutput, DevOpsOutput, or dict with fixed_code.
    subdir: optional subdirectory (e.g. "backend", "frontend")
    commit_message: override suggested_commit_message if provided.

    Validates file paths before writing. Rejects paths that look like task
    descriptions or have segments > MAX_SEGMENT_LENGTH characters.

    Returns (success, message).
    """
    if isinstance(output, dict):
        files = output.get("files", {})
        if output.get("fixed_code"):
            fix_path = output.get("fix_path", "fixes.py")
            files[fix_path] = output["fixed_code"]
        commit_message = commit_message or output.get("commit_message", "chore: apply fixes")
        gitignore_entries = output.get("gitignore_entries") or []
    else:
        files = _output_to_files_dict(output, subdir)
        commit_message = (
            commit_message
            or getattr(output, "suggested_commit_message", None)
            or "chore: agent output"
        )
        gitignore_entries = getattr(output, "gitignore_entries", None) or []

    # Merge agent-provided gitignore patterns into repo root .gitignore
    if gitignore_entries:
        repo = Path(repo_path).resolve()
        merged_content, changed = merge_gitignore_entries(repo, list(gitignore_entries))
        if changed:
            files[".gitignore"] = merged_content

    if not files:
        return False, NO_FILES_TO_WRITE_MSG

    # Validate paths before writing
    validated_files, warnings = _validate_paths(files, subdir)
    for w in warnings:
        logger.warning("repo_writer: %s", w)

    if not validated_files:
        rejected_paths = list(files.keys())
        return False, f"All {len(files)} files rejected by path validation: {rejected_paths}"

    if len(validated_files) < len(files):
        logger.warning(
            "repo_writer: %s of %s files passed validation (rejected %s)",
            len(validated_files),
            len(files),
            len(files) - len(validated_files),
        )

    return write_files_and_commit(Path(repo_path).resolve(), validated_files, commit_message)
