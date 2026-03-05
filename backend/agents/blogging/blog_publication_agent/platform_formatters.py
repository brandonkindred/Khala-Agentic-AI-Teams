"""
Platform-specific formatters for blog posts (Medium, dev.to, Substack).
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import List, Optional


def _extract_title_from_draft(draft: str) -> str:
    """Extract title from first H1 in draft."""
    match = re.search(r"^#\s+(.+)$", draft.strip(), re.MULTILINE)
    return match.group(1).strip() if match else "Untitled Post"


def format_for_medium(draft: str) -> str:
    """
    Format draft for Medium.com. Medium accepts Markdown; ensure clean formatting.
    """
    return draft.strip()


def format_for_devto(
    draft: str,
    title: str,
    tags: Optional[List[str]] = None,
    canonical_url: Optional[str] = None,
) -> str:
    """
    Format draft for dev.to with front matter.
    Front matter: title, published, tags, canonical_url (when cross posting).
    """
    tags = tags or []
    front_matter_lines = [
        "---",
        f"title: {title}",
        f"published: false",
        f"tags: {', '.join(tags) if tags else ''}",
    ]
    if canonical_url:
        front_matter_lines.append(f"canonical_url: {canonical_url}")
    front_matter_lines.append("---")
    front_matter_lines.append("")

    body = draft.strip()
    return "\n".join(front_matter_lines) + body


def format_for_substack(draft: str) -> str:
    """
    Format draft for Substack. Substack uses Markdown; paste-ready formatting.
    """
    return draft.strip()
