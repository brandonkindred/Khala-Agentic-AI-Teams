"""Shared LLM-output validators for the Strategy Lab agent suite (#559).

After the structured-DSL migration (#537 step 2, #551), `StrategySpec`
accepts only structured `EntryRule` / `ExitRule` / `SizingRule` objects.
Agents that author specs from LLM JSON must therefore reject prose
payloads up front so the error surface points at the LLM call rather
than the orchestrator's `StrategySpec(...)` construction.

The helper here dispatches the rule-shaped slots of a parsed LLM dict
through the existing TypeAdapters in
``backend/agents/investment_team/strategy_lab/spec_dsl.py`` and wraps any
pydantic ``ValidationError`` in :class:`StrategySpecParseError` with a
message that names the offending field and quotes the failing payload.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, Iterable

from pydantic import ValidationError

from ..spec_dsl import EntryRuleAdapter, ExitRuleAdapter, SizingRuleAdapter


def extract_json_object(text: str) -> Dict[str, Any]:
    """Extract a JSON object from an LLM response, tolerating markdown fences.

    Preconditions: ``text`` is a string (possibly empty).
    Postconditions: returns the parsed dict for the outermost ``{...}`` in
    the response. Raises ``ValueError`` when no JSON object is present or
    the substring is not valid JSON. Used by every spec-authoring or
    spec-reviewing agent in this package — keep behaviour stable.
    """
    fence_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if fence_match:
        text = fence_match.group(1)

    start = text.find("{")
    if start == -1:
        raise ValueError("No JSON object found in LLM response")

    depth = 0
    end = start
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break

    try:
        return json.loads(text[start:end])
    except json.JSONDecodeError as exc:
        raise ValueError(f"Failed to parse JSON from LLM response: {exc}") from exc


def parse_retry_budget(env_name: str, default: int = 2) -> int:
    """Resolve a non-negative parse-retry budget from an env var.

    Reads ``env_name`` as an int (default ``default``); sub-zero values clamp
    to ``0`` (no retry); garbage / empty values fall back to ``default`` rather
    than raising — the surrounding LLM loop is best-effort. Shared by the
    spec-authoring agents that re-prompt on unparseable JSON (design,
    refinement).

    Preconditions: ``env_name`` is an env var name; ``default >= 0``.
    Postconditions: returns a non-negative int; never raises.
    """
    try:
        return max(int(os.environ.get(env_name, str(default))), 0)
    except ValueError:
        return default


_JSON_CORRECTION_PREAMBLE = """\
Your previous response could not be parsed as a single JSON object
({error}). Return ONLY one JSON object with no surrounding prose, no
markdown fences, and no trailing commentary.{keys_hint} Every brace must
balance.

--- ORIGINAL TASK BELOW ---
{original_prompt}
"""


def build_json_correction_prompt(user_prompt: str, exc: ValueError, *, keys_hint: str = "") -> str:
    """Render a re-prompt for a malformed-JSON (unparseable) response.

    Used by every spec-authoring agent that re-prompts the model after
    :func:`extract_json_object` fails to recover a balanced JSON object. The
    full exception text is embedded verbatim (no truncation) so the model sees
    exactly what went wrong. ``keys_hint``, when non-empty, is spliced in to
    name the exact keys the caller expects (the refinement agent uses it; the
    designer leaves it empty). Distinct from a DSL-validation correction, which
    quotes the offending field + pydantic error instead.

    Preconditions: ``exc`` is the ``ValueError`` raised by
    :func:`extract_json_object`; ``user_prompt`` is the original task; if
    given, ``keys_hint`` should begin with a leading space so it reads as a
    sentence continuation.
    Postconditions: returns a string instructing the model to re-emit a single,
    fence-free JSON object. The substituted values are not re-scanned for
    format fields, so literal braces in ``user_prompt`` / ``exc`` are safe.
    """
    return _JSON_CORRECTION_PREAMBLE.format(
        error=str(exc), keys_hint=keys_hint, original_prompt=user_prompt
    )


class StrategySpecParseError(ValueError):
    """Raised when an LLM payload cannot be parsed into the structured DSL.

    ``field`` is the spec field that failed (``entry_rules``, ``exit_rules``,
    ``sizing``). ``payload`` is the offending sub-value, snipped for log
    safety. ``cause`` is the chained pydantic ``ValidationError``.
    """

    def __init__(self, field: str, payload: Any, cause: Exception) -> None:
        snippet = _snippet(payload)
        super().__init__(
            f"LLM emitted prose or invalid structure; structured DSL required. "
            f"Field={field}; payload={snippet}; pydantic error={cause}"
        )
        self.field = field
        self.payload = payload
        self.__cause__ = cause


_RULE_LIST_FIELDS: Dict[str, Any] = {
    "entry_rules": EntryRuleAdapter,
    "exit_rules": ExitRuleAdapter,
}


def validate_structured_rules(parsed: Dict[str, Any]) -> None:
    """Dispatch rule-shaped slots of ``parsed`` through the DSL TypeAdapters.

    Mutates nothing. Raises :class:`StrategySpecParseError` on the first
    field that fails to validate. Fields that are absent from ``parsed``
    are skipped — the caller is responsible for required-field policy.
    """
    for field, adapter in _RULE_LIST_FIELDS.items():
        if field not in parsed:
            continue
        value = parsed[field]
        if not isinstance(value, list):
            raise StrategySpecParseError(
                field,
                value,
                TypeError(f"expected a list of rule objects, got {type(value).__name__}"),
            )
        for index, item in enumerate(value):
            try:
                adapter.validate_python(item)
            except ValidationError as exc:
                raise StrategySpecParseError(f"{field}[{index}]", item, exc) from exc

    if "sizing" in parsed:
        value = parsed["sizing"]
        try:
            SizingRuleAdapter.validate_python(value)
        except ValidationError as exc:
            raise StrategySpecParseError("sizing", value, exc) from exc


def _snippet(value: Any, limit: int = 200) -> str:
    """Render ``value`` as a short, log-safe string.

    Strings are quoted; dict / list payloads are JSON-encoded. Anything
    longer than ``limit`` chars is truncated with an ellipsis so prose
    runaways do not flood log lines.
    """
    if isinstance(value, str):
        text = repr(value)
    else:
        try:
            text = json.dumps(value, separators=(",", ":"), default=str)
        except (TypeError, ValueError):
            text = repr(value)
    if len(text) > limit:
        return text[: limit - 1] + "…"
    return text


__all__: Iterable[str] = (
    "StrategySpecParseError",
    "build_json_correction_prompt",
    "extract_json_object",
    "parse_retry_budget",
    "validate_structured_rules",
)
