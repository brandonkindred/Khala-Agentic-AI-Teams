"""Dispatch Git tool calls to software_engineering_team.shared.git_utils."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Callable, Dict

from software_engineering_team.shared.git_utils import (
    DEVELOPMENT_BRANCH,
    _run_git,
    checkout_branch,
    commit_working_tree,
    create_feature_branch,
    merge_branch,
    write_files_and_commit,
)

from .context import GitToolContext
from .definitions import GIT_TOOL_DEFINITIONS

logger = logging.getLogger(__name__)

# Branches the model may name explicitly (plus feature/fix/refactor/*).
_BRANCH_RE = re.compile(
    r"^(?:development|main|master|HEAD|feature/[A-Za-z0-9][A-Za-z0-9_./-]*|"
    r"fix/[A-Za-z0-9][A-Za-z0-9_./-]*|refactor/[A-Za-z0-9][A-Za-z0-9_./-]*)$"
)

_FEATURE_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_./-]*$")


def _strip_model_repo_path(args: dict[str, Any]) -> dict[str, Any]:
    """Ignore repo_path if the model sends it; execution always uses GitToolContext."""
    out = {k: v for k, v in args.items() if k != "repo_path"}
    return out


def _validate_branch(name: str) -> bool:
    if not name or len(name) > 200:
        return False
    return bool(_BRANCH_RE.match(name.strip()))


def _validate_rel_paths(paths: Dict[str, str]) -> tuple[bool, str]:
    for p in paths:
        path = Path(p)
        if path.is_absolute():
            return False, f"absolute path not allowed: {p}"
        parts = path.parts
        if ".." in parts or p.startswith("/"):
            return False, f"path escapes repo: {p}"
    return True, ""


def execute_git_tool(name: str, arguments: dict[str, Any], ctx: GitToolContext) -> dict[str, Any]:
    """
    Run a single Git tool by OpenAI function name. Returns a JSON-serializable dict
    (success, message, and optional details).
    """
    args = _strip_model_repo_path(dict(arguments or {}))
    repo = ctx.repo_path
    if not (repo / ".git").exists():
        return {"success": False, "error": "not_a_git_repository", "message": str(repo)}

    try:
        if name == "git_status":
            code, out = _run_git(repo, ["git", "status", "--porcelain"])
            return {"success": code == 0, "stdout": out, "returncode": code}

        if name == "git_diff":
            staged = bool(args.get("staged"))
            paths = args.get("paths")
            cmd = ["git", "diff"]
            if staged:
                cmd.append("--cached")
            if isinstance(paths, list) and paths:
                ok, err = _validate_rel_paths({str(p): "" for p in paths})
                if not ok:
                    return {"success": False, "error": "invalid_paths", "message": err}
                cmd.append("--")
                cmd.extend(str(p) for p in paths)
            code, out = _run_git(repo, cmd)
            return {"success": code == 0, "stdout": out, "returncode": code}

        if name == "git_log":
            limit = int(args.get("limit") or 20)
            limit = max(1, min(limit, 500))
            code, out = _run_git(repo, ["git", "log", f"-{limit}", "--oneline"])
            return {"success": code == 0, "stdout": out, "returncode": code}

        if name == "git_checkout_branch":
            branch = str(args.get("branch") or "").strip()
            if not _validate_branch(branch):
                return {"success": False, "error": "invalid_branch", "message": branch}
            ok, msg = checkout_branch(repo, branch)
            return {"success": ok, "message": msg}

        if name == "git_create_feature_branch":
            slug = str(args.get("feature_name") or "").strip()
            if not _FEATURE_SLUG_RE.match(slug):
                return {"success": False, "error": "invalid_feature_name", "message": slug}
            base = str(args.get("base_branch") or ctx.default_base_branch).strip()
            if not _validate_branch(base):
                return {"success": False, "error": "invalid_base_branch", "message": base}
            ok, msg = create_feature_branch(repo, base, slug)
            return {"success": ok, "message": msg, "branch": msg if ok else None}

        if name == "git_write_files_and_commit":
            files = args.get("files")
            message = str(args.get("message") or "").strip()
            if not isinstance(files, dict) or not files:
                return {"success": False, "error": "missing_files", "message": "files must be a non-empty object"}
            if not message:
                return {"success": False, "error": "missing_message", "message": "commit message required"}
            str_files = {str(k): str(v) for k, v in files.items()}
            ok_paths, err = _validate_rel_paths(str_files)
            if not ok_paths:
                return {"success": False, "error": "invalid_paths", "message": err}
            ok, msg = write_files_and_commit(repo, str_files, message)
            return {"success": ok, "message": msg}

        if name == "git_commit_working_tree":
            message = str(args.get("message") or "").strip()
            if not message:
                return {"success": False, "error": "missing_message", "message": "commit message required"}
            ok, msg = commit_working_tree(repo, message)
            return {"success": ok, "message": msg}

        if name == "git_merge_branch":
            if not ctx.allow_merge_to_default_branch:
                return {"success": False, "error": "merge_disabled", "message": "merge not allowed for this job"}
            source = str(args.get("source_branch") or "").strip()
            target = str(args.get("target_branch") or "").strip()
            if not _validate_branch(source) or not _validate_branch(target):
                return {
                    "success": False,
                    "error": "invalid_branch",
                    "message": f"source={source!r} target={target!r}",
                }
            if target != DEVELOPMENT_BRANCH and target != ctx.default_base_branch:
                return {
                    "success": False,
                    "error": "target_not_allowed",
                    "message": f"target must be {DEVELOPMENT_BRANCH!r} or default base",
                }
            ok, msg = merge_branch(repo, source, target)
            return {"success": ok, "message": msg}

        return {"success": False, "error": "unknown_tool", "message": name}
    except Exception as e:
        logger.warning("execute_git_tool %s failed: %s", name, e)
        return {"success": False, "error": "exception", "message": str(e)}


def build_git_tool_handlers(ctx: GitToolContext) -> Dict[str, Callable[[dict[str, Any]], dict[str, Any]]]:
    """Handlers for use with ``llm_service.tool_loop.complete_json_with_tool_loop``."""

    def _wrap(tool_name: str) -> Callable[[dict[str, Any]], dict[str, Any]]:
        def _run(args: dict[str, Any]) -> dict[str, Any]:
            return execute_git_tool(tool_name, args, ctx)

        return _run

    return {fn["function"]["name"]: _wrap(fn["function"]["name"]) for fn in GIT_TOOL_DEFINITIONS}
