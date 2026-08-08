"""Shared parsing for the concatenated ``### path ###``-headered code transport.

Used by any agent that accepts a single ``code`` string covering multiple
files (the legacy transport predating per-file ``files`` dicts). Pure,
LLM-free, and total: every non-blank character of the input is covered by
some returned block.
"""

from __future__ import annotations

import re
from typing import List, Tuple

# Pattern: a whole line of the form "### path/to/file ###". Anchored to line
# boundaries with a single-line path so header-like fragments inside source
# (markdown headings, "### x" comments, mid-line strings) can never match,
# and a false header can never swallow lines of code the way an unanchored
# DOTALL pattern could.
_FILE_HEADER_PATTERN = re.compile(r"^###[ \t]+(\S[^\n]*?)[ \t]+###[ \t]*\n", re.MULTILINE)


def parse_code_into_file_blocks(code: str) -> List[Tuple[str, str]]:
    """
    Parse concatenated code into (path, content) blocks using ### path ### pattern.
    Returns list of (file_path, content) tuples.

    Only a complete line of the form ``### path ###`` counts as a header, so a
    header can never span source lines. A source line that happens to match
    that exact shape (e.g. inside a docstring) is still read as a header — an
    inherent ambiguity of the legacy ``code=`` transport; callers whose content
    may contain such lines must use a per-file ``files`` dict instead, which
    skips header parsing entirely.

    Postconditions:
        - Every non-blank character of ``code`` except recognized header lines
          is covered by some block: content before the first header (or all of
          it, when no header exists) becomes a ``('', content)`` block rather
          than being dropped.
    """
    blocks: List[Tuple[str, str]] = []
    matches = list(_FILE_HEADER_PATTERN.finditer(code))
    if not matches:
        if code.strip():
            blocks.append(("", code.strip()))
        return blocks
    preamble = code[: matches[0].start()]
    if preamble.strip():
        blocks.append(("", preamble.strip()))
    for i, m in enumerate(matches):
        path = m.group(1).strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(code)
        content = code[start:end].rstrip()
        blocks.append((path, content))
    return blocks
