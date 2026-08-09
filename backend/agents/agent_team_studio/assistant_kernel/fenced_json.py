"""Fenced-JSON-in-prose extraction, stripping, and keyed-list merge helpers.

Design-assistant LLM replies embed structured updates as fenced code blocks
inside otherwise free-form prose (e.g. ```` ```agent\n{...}\n``` ````). Both
``agent_studio.assistant`` and ``agentic_team_provisioning.assistant.agent``
parse this shape with near-identical regex + ``json.loads`` helpers that
differ only in the block's tag and expected JSON type; this module is the
shared implementation. Each assistant's *merge* strategy (how a parsed block
folds onto the current draft) stays caller-supplied — one does a field-level
overlay with re-validation, the other rebuilds the draft from field
fallbacks, and unifying those is out of scope here — except for the one
merge shape both use: overlaying a list of dicts by key, which is common
enough to share (:func:`merge_list_by_key`).
"""

from __future__ import annotations

import json
import re
from typing import Any


def parse_fenced_json(
    text: str, tag: str, *, expected_type: type | tuple[type, ...] = dict
) -> Any | None:
    """Extract and parse the first ```` ```{tag} ... ``` ```` block in ``text``.

    Preconditions:
        * ``tag`` contains no regex metacharacters that would need escaping
          beyond what :func:`re.escape` handles (callers pass literal block
          names like ``"agent"``/``"process"``/``"suggestions"``).
    Postconditions:
        * Returns the parsed JSON value when a block is found, its body is
          valid JSON, and the parsed value is an instance of
          ``expected_type``. Returns ``None`` otherwise — missing block,
          malformed JSON, or a value of the wrong top-level type — and never
          raises, so a pathological model output degrades to "no parseable
          update" rather than propagating an exception.
        * When ``text`` contains more than one ``{tag}`` block, only the
          first (non-greedy match) is considered.
    """
    pattern = re.compile(r"```" + re.escape(tag) + r"\s*\n?(.*?)```", re.DOTALL)
    match = pattern.search(text)
    if not match:
        return None
    try:
        data = json.loads(match.group(1).strip())
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, expected_type) else None


def strip_fenced_blocks(text: str, tags: list[str]) -> str:
    """Remove every ```` ```{tag} ... ``` ```` block (for each tag in ``tags``) from ``text``.

    Preconditions:
        * ``tags`` contains literal block names (see :func:`parse_fenced_json`).
    Postconditions:
        * Returns ``text`` with every block for every listed tag removed and
          the result stripped of leading/trailing whitespace. A tag with no
          matching block in ``text`` is a no-op for that tag.
    """
    for tag in tags:
        text = re.sub(r"```" + re.escape(tag) + r"\s*\n?.*?```", "", text, flags=re.DOTALL)
    return text.strip()


def merge_list_by_key(current: list[dict], incoming: list[dict], *, key: str) -> list[dict]:
    """Overlay ``incoming`` dicts onto ``current`` by a shared dict key.

    Entries in ``current`` whose key ``incoming`` doesn't mention are kept
    unchanged and keep their original position; entries ``incoming`` does
    mention are replaced in place; keys present only in ``incoming`` are
    appended, in ``incoming``'s order. This is the "partial echo doesn't
    discard the rest" merge both assistants need for keyed sub-lists (e.g.
    an agent definition's operating states) without wholesale-replacing a
    list the model only echoed part of.

    Preconditions:
        * Every dict in ``current`` and ``incoming`` contains ``key``, and
          the values under ``key`` are hashable. Violating this is a caller
          bug (malformed entries must be filtered out, or rejected, before
          calling) — this function does not coerce or skip bad entries.
    Postconditions:
        * Returns a new list; neither ``current`` nor ``incoming`` is
          mutated.
    """
    assert all(key in item for item in current), f"every current entry must contain {key!r}"
    assert all(key in item for item in incoming), f"every incoming entry must contain {key!r}"
    by_key = {item[key]: item for item in current}
    for item in incoming:
        by_key[item[key]] = item
    return list(by_key.values())
