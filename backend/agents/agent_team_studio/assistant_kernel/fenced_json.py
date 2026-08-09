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


def _fenced_block_pattern(tag: str) -> re.Pattern[str]:
    """Compile the regex matching a fenced ```` ```{tag} ... ``` ```` block.

    Preconditions:
        * ``tag`` contains no regex metacharacters that would need escaping
          beyond what :func:`re.escape` handles (callers pass literal block
          names like ``"agent"``/``"process"``/``"suggestions"``).
    Postconditions:
        * The returned pattern's match starts at a literal ```` ```{tag} ````
          fence and requires the character immediately after ``tag`` to be
          whitespace or the fence's closing backtick — via the ``(?=\\s|`)``
          lookahead — so a shorter tag never matches as a prefix of a longer
          one, whether the longer tag extends it with a word character
          (``"agent"`` vs. ```` ```agents ````) or punctuation (``"agent"``
          vs. ```` ```agent-v2 ````).
        * The closing ```` ``` ```` must be on its own line — preceded by a
          newline and followed by a newline or end of string — so a literal
          backtick run embedded inside the JSON body (e.g. a
          ``system_prompt`` string containing a markdown code example like
          ```` ```python ... ``` ````) is never mistaken for the block's
          real closing fence. Group 1 captures the block body.
    """
    return re.compile(r"```" + re.escape(tag) + r"(?=\s|`)[^\n]*\n(.*?)\n```(?=\n|$)", re.DOTALL)


def parse_fenced_json(
    text: str, tag: str, *, expected_type: type | tuple[type, ...] = dict
) -> Any | None:
    """Extract and parse the first ```` ```{tag} ... ``` ```` block in ``text``.

    Preconditions:
        * See :func:`_fenced_block_pattern` for constraints on ``tag``.
    Postconditions:
        * Returns the parsed JSON value when a block is found, its body is
          valid JSON, and the parsed value is an instance of
          ``expected_type``. Returns ``None`` otherwise — missing block,
          malformed JSON (``json.JSONDecodeError``, a ``ValueError``
          subclass), or JSON that's syntactically valid but pathological in
          a way ``json.loads`` itself rejects (e.g. an integer literal so
          large it exceeds Python's int-string conversion limit, raising a
          plain ``ValueError``; or excessive nesting, raising
          ``RecursionError``) — or a value of the wrong top-level type. Never
          raises, so a pathological model output degrades to "no parseable
          update" rather than propagating an exception.
        * When ``text`` contains more than one ``{tag}`` block, only the
          first (non-greedy match) is considered. A block for a *different*,
          longer tag that merely starts with ``tag`` (e.g. ``agents`` vs.
          ``agent``) is never matched.
    """
    match = _fenced_block_pattern(tag).search(text)
    if not match:
        return None
    try:
        data = json.loads(match.group(1).strip())
    except (ValueError, RecursionError):
        return None
    return data if isinstance(data, expected_type) else None


def strip_fenced_blocks(text: str, tags: list[str]) -> str:
    """Remove every ```` ```{tag} ... ``` ```` block (for each tag in ``tags``) from ``text``.

    Preconditions:
        * ``tags`` contains literal block names (see :func:`_fenced_block_pattern`).
    Postconditions:
        * Returns ``text`` with every block for every listed tag removed and
          the result stripped of leading/trailing whitespace. A tag with no
          matching block in ``text`` is a no-op for that tag — including when
          ``text`` only contains a block for a longer tag sharing ``tag`` as
          a prefix, whether word-extended (stripping ``"agent"`` leaves a
          ```` ```agents ```` block untouched) or punctuation-extended
          (leaves a ```` ```agent-v2 ```` block untouched too).
    """
    for tag in tags:
        text = _fenced_block_pattern(tag).sub("", text)
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
