"""Completeness check for LLM full-file rewrites, shared across code-v2 phases.

Used wherever an LLM re-emits a whole file (codegen, batch fixes, one-at-a-time
fixes, tool-agent fixes, the DbC comments pass) so a syntactically incomplete
rewrite is rejected before it's merged into the working file set, rather than
silently written to disk.
"""

from __future__ import annotations

import ast
from typing import Dict, Tuple


def reject_invalid_python(files: Dict[str, str]) -> Tuple[Dict[str, str], Dict[str, str]]:
    """Split ``files`` into syntactically valid and invalid Python entries.

    An LLM full-file rewrite can stop mid-file (e.g. abandon a class body)
    while still returning a response the API considers complete, so
    ``stop_reason``/``finish_reason`` truncation checks in the LLM client
    can't catch it. This is the last line of defense before a rewritten file
    is merged into the working file set.

    Preconditions:
        ``files`` maps repo-relative paths to full file content.
    Postconditions:
        Returns ``(valid, rejected)``; ``rejected`` maps path to the parse
        error message. Non-``.py`` paths are always considered valid. Pure.
    """
    valid: Dict[str, str] = {}
    rejected: Dict[str, str] = {}
    for path, content in files.items():
        if path.endswith(".py"):
            try:
                ast.parse(content)
            except SyntaxError as exc:
                rejected[path] = f"{exc.__class__.__name__}: {exc}"
                continue
        valid[path] = content
    return valid, rejected
