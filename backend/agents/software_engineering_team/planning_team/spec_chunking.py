"""
Spec chunking utilities for splitting large specs into context-sized pieces.

Used by Tech Lead when calling Spec Chunk Analyzer so each LLM call stays
within 16K/32K token limits.
"""

from __future__ import annotations

import re
from typing import List, Tuple


def chunk_spec_by_size(
    spec_content: str,
    max_chars: int = 12000,
    overlap: int = 500,
) -> List[str]:
    """
    Split spec content into consecutive chunks of at most max_chars, with optional overlap.

    Overlap helps avoid cutting mid-sentence; the last `overlap` chars of each chunk
    are repeated at the start of the next chunk.

    Args:
        spec_content: Full spec text to split.
        max_chars: Maximum characters per chunk (default 12K for ~3K tokens).
        overlap: Number of characters to overlap between chunks (default 500).

    Returns:
        List of spec chunks. Empty if spec_content is empty.
    """
    if not spec_content or not spec_content.strip():
        return []

    spec = spec_content.strip()
    if len(spec) <= max_chars:
        return [spec]

    chunks: List[str] = []
    start = 0
    end = min(max_chars, len(spec))

    while start < len(spec):
        chunk = spec[start:end]
        if chunk.strip():
            chunks.append(chunk)
        start = end - overlap if overlap > 0 and end < len(spec) else end
        end = min(start + max_chars, len(spec))
        if start >= len(spec):
            break

    return chunks


def chunk_spec_by_sections(
    spec_content: str,
    max_chars: int = 12000,
) -> List[Tuple[str, str]]:
    """
    Split spec content by ## headers, capping each section (or group of small sections) at max_chars.

    Returns a list of (section_title, content) tuples. Section title is the ## header
    for that chunk; empty string if the chunk starts before any header.

    Args:
        spec_content: Full spec text to split.
        max_chars: Maximum characters per chunk (default 12K).

    Returns:
        List of (section_title, content) tuples. Section titles may be empty for
        intro content before the first ##.
    """
    if not spec_content or not spec_content.strip():
        return []

    spec = spec_content.strip()
    if len(spec) <= max_chars:
        return [("", spec)]

    # Pattern: ## header (allows optional leading whitespace for indented headers)
    header_pattern = re.compile(r"^\s*(#{1,6})\s+(.+)$", re.MULTILINE)

    sections: List[Tuple[str, str]] = []  # (title, content)
    last_end = 0
    last_title = ""

    for match in header_pattern.finditer(spec):
        start = match.start()
        title = match.group(2).strip()
        if start > last_end:
            content = spec[last_end:start].strip()
            if content:
                sections.append((last_title, content))
        last_end = match.end()
        last_title = title

    if last_end < len(spec):
        content = spec[last_end:].strip()
        if content:
            sections.append((last_title, content))

    if not sections:
        return [("", spec)]

    # Group sections into chunks of at most max_chars
    chunks: List[Tuple[str, str]] = []
    current_content: List[str] = []
    current_title = ""

    for title, content in sections:
        new_content = current_content + [content]
        new_len = len("\n\n".join(new_content))
        if new_len > max_chars and current_content:
            # Flush current chunk before adding this section
            combined = "\n\n".join(current_content)
            chunks.append((current_title, combined))
            current_content = []
            current_title = ""

        # If single section exceeds max_chars, sub-split by size
        if len(content) > max_chars:
            sub_chunks = chunk_spec_by_size(content, max_chars=max_chars, overlap=0)
            for i, sub in enumerate(sub_chunks):
                chunks.append((f"{title} (part {i+1})" if title else "", sub))
            continue

        current_content.append(content)
        len("\n\n".join(current_content))
        if not current_title:
            current_title = title

    if current_content:
        combined = "\n\n".join(current_content)
        chunks.append((current_title, combined))

    return chunks
