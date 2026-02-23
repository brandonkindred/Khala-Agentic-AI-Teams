"""
Resolve frontend framework from spec text or task metadata.

Used so the front-end coding agent uses Angular by default unless React or Vue
is specified in the spec or chosen by the planning team.
"""

from __future__ import annotations

import re
from typing import Optional

# Scan first N chars of spec for framework mentions to avoid false positives in long docs
_SPEC_SCAN_CHARS = 16_000

# Word-boundary patterns for framework names (case-insensitive)
_REACT_PATTERN = re.compile(
    r"\b(?:react(?:\s+(?:app|application|frontend|ui|framework))?|use\s+react)\b",
    re.IGNORECASE,
)
_VUE_PATTERN = re.compile(
    r"\b(?:vue(?:\s*(?:\.?\s*js|3)?|(?:app|application|frontend|framework))?|use\s+vue)\b",
    re.IGNORECASE,
)


def get_frontend_framework_from_spec(spec_content: str) -> Optional[str]:
    """
    Detect if the spec explicitly requires React or Vue.

    Returns "react", "vue", or None. Uses word-boundary and phrase checks to
    avoid false positives (e.g. "reaction" does not set React). Scans the
    first _SPEC_SCAN_CHARS of the spec. If both React and Vue appear, returns
    the first match in that order (React checked first).
    """
    if not spec_content or not spec_content.strip():
        return None
    text = spec_content[:_SPEC_SCAN_CHARS]
    if _REACT_PATTERN.search(text):
        return "react"
    if _VUE_PATTERN.search(text):
        return "vue"
    return None


def resolve_frontend_framework(
    task_metadata: Optional[dict],
    spec_content: Optional[str],
) -> str:
    """
    Resolve framework in order: task metadata -> spec -> default Angular.

    Returns a normalized value: "react", "vue", or "angular".
    """
    meta = task_metadata or {}
    from_meta = meta.get("framework_target")
    if from_meta:
        normalized = str(from_meta).lower().strip()
        if normalized in ("react", "vue", "angular"):
            return normalized
    from_spec = get_frontend_framework_from_spec(spec_content or "")
    if from_spec:
        return from_spec
    return "angular"
