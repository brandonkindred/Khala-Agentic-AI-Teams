"""OpenAI-compatible tool definitions for read-only repo inspection (names match executor dispatch)."""

from __future__ import annotations

from typing import Any, List

# Tool function names must match keys in ``build_repo_inspect_handlers``.
REPO_INSPECT_TOOL_DEFINITIONS: List[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": (
                "List files and directories under a path in the repository. Read-only. "
                "Paths are relative to the repo root (no absolute paths, no ..). Build, "
                "VCS, and dependency-cache directories (e.g. .git, node_modules) are skipped. "
                "Use this to discover existing code and confirm whether a file already exists "
                "before creating it."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative directory to list (default repo root '.').",
                    },
                    "glob": {
                        "type": "string",
                        "description": (
                            "Optional glob pattern evaluated under 'path' (e.g. '*.py' or "
                            "'**/*.ts' for a recursive match). When omitted, immediate entries "
                            "of the directory are listed."
                        ),
                    },
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "Return the full contents of a single file in the repository. Read-only. "
                "The path must be relative to the repo root (no absolute paths, no ..). "
                "Output is byte-bounded: a file larger than the limit returns an error "
                "(it is never partially returned) — read a more specific file instead."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative path of the file to read.",
                    },
                    "max_bytes": {
                        "type": "integer",
                        "description": (
                            "Optional lower byte limit for this read; clamped to the configured "
                            "ceiling. A file larger than the effective limit returns an error."
                        ),
                    },
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    },
]
