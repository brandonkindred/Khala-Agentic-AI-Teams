"""Single shared audience-formatting implementation.

Used by both the API layer (``api/models.py``, operating on ``AudienceDetails``
Pydantic models or strings) and the job-runner layer
(``shared/run_pipeline_job.py``, operating on plain dicts or strings), so the
"profession: X; skill_level: Y; interests: Z; other" formatting logic exists
in exactly one place.
"""

from __future__ import annotations

from typing import Any, Optional


def format_audience(audience: Any) -> Optional[str]:
    """Format an audience value into a display string, or None when empty.

    Preconditions:
        - ``audience`` is a str, a dict, an object exposing
          ``profession``/``skill_level``/``hobbies``/``other`` attributes
          (e.g. a Pydantic ``AudienceDetails``), None, or any other value
          (coerced to None).
    Postconditions:
        - Returns a trimmed, non-empty string built as
          "profession: ...; skill_level: ...; interests: ...; other",
          omitting any part whose value is falsy, or None when the input is
          empty/unusable.
    """
    if audience is None:
        return None
    if isinstance(audience, str):
        return audience.strip() or None
    if isinstance(audience, dict):
        get = audience.get
    elif hasattr(audience, "profession") or hasattr(audience, "skill_level"):
        get = lambda key: getattr(audience, key, None)  # noqa: E731
    else:
        return None

    parts = []
    profession = get("profession")
    if profession:
        parts.append(f"profession: {profession}")
    skill_level = get("skill_level")
    if skill_level:
        parts.append(f"skill_level: {skill_level}")
    hobbies = get("hobbies")
    if hobbies:
        parts.append(f"interests: {', '.join(hobbies)}")
    other = get("other")
    if other:
        parts.append(other)
    return "; ".join(parts) if parts else None
