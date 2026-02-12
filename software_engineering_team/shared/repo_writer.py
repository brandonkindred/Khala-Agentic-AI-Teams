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

# Maximum length for any single path segment (directory or filename without extension)
MAX_SEGMENT_LENGTH = 40

# Pattern that matches names with 6+ hyphenated words (likely a sentence, not a component name)
_SENTENCE_NAME_RE = re.compile(r"^[a-z]+-[a-z]+-[a-z]+-[a-z]+-[a-z]+-[a-z]+")


def _validate_paths(files: Dict[str, str], subdir: str = "") -> Tuple[Dict[str, str], List[str]]:
    """
    Validate file paths from agent output. Rejects paths with:
    - Segments longer than MAX_SEGMENT_LENGTH (likely task-description-as-name)
    - Segments that look like sentences (6+ hyphenated words)
    - Empty file content

    Returns (validated_files, warnings).
    """
    validated: Dict[str, str] = {}
    warnings: List[str] = []

    for path, content in files.items():
        segments = path.split("/")
        bad = False
        for seg in segments:
            name_part = seg.split(".")[0]  # strip extension
            if not name_part:
                continue
            if len(name_part) > MAX_SEGMENT_LENGTH:
                warnings.append(
                    f"REJECTED: path segment '{seg}' is {len(name_part)} chars "
                    f"(max {MAX_SEGMENT_LENGTH}) - likely task description as name: '{path}'"
                )
                bad = True
                break
            if _SENTENCE_NAME_RE.match(name_part):
                warnings.append(
                    f"REJECTED: path segment '{seg}' looks like a sentence, "
                    f"not a proper component/module name: '{path}'"
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
    else:
        files = _output_to_files_dict(output, subdir)
        commit_message = commit_message or getattr(output, "suggested_commit_message", None) or "chore: agent output"

    if not files:
        return False, "No files to write"

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
            len(validated_files), len(files), len(files) - len(validated_files),
        )

    return write_files_and_commit(Path(repo_path).resolve(), validated_files, commit_message)
