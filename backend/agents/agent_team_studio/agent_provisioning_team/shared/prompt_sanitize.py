"""Sanitize manifest-supplied values before interpolating them into LLM prompts."""

from __future__ import annotations

import re

# Characters that have no business inside an interpolated prompt variable.
# We allow letters, digits, basic punctuation and whitespace; everything else
# is removed. This is a defense-in-depth measure against prompt injection
# through manifest fields.
_PROMPT_VAR_DISALLOWED = re.compile(r"[^A-Za-z0-9 _\-./:@,()\[\]{}+=#'\"\n\t]")
_PROMPT_VAR_MAX_LEN = 100000


def sanitize_prompt_var(value: object, *, max_len: int = _PROMPT_VAR_MAX_LEN) -> str:
    """Make a manifest-supplied value safe to interpolate into an LLM prompt.

    - Coerces to str
    - Strips disallowed characters
    - Caps length at ``max_len`` (default 100k chars) to prevent a prompt-bomb
      / context-blowing input while still allowing large legitimate prompts;
      a truncated value carries a trailing ``"…[truncated]"`` marker, so its
      final length is ``max_len + len("…[truncated]")``, not exactly ``max_len``
    """
    text = "" if value is None else str(value)
    text = _PROMPT_VAR_DISALLOWED.sub("", text)
    if len(text) > max_len:
        text = text[:max_len] + "…[truncated]"
    return text
