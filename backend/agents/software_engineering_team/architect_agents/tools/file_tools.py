"""File I/O tools for reading spec and planning docs."""

from __future__ import annotations

from pathlib import Path

from strands import tool


@tool
def file_read_tool(path: str) -> str:
    """Read the contents of a file from the filesystem.

    Use this to load spec documents, planning docs, and other text files.
    Supports relative and absolute paths. Paths are resolved relative to
    the current working directory unless an absolute path is given.

    Args:
        path: File path to read (relative or absolute). Must point to a
            file within the project/spec directory for security.

    Returns:
        The file contents as a string. Returns an error message if the
        file cannot be read (not found, permission denied, etc.).
    """
    try:
        p = Path(path).resolve()
        if not p.exists():
            return f"Error: File not found: {path}"
        if not p.is_file():
            return f"Error: Path is not a file: {path}"
        return p.read_text(encoding="utf-8", errors="replace")
    except PermissionError as e:
        return f"Error: Permission denied reading {path}: {e}"
    except Exception as e:
        return f"Error reading {path}: {e}"
