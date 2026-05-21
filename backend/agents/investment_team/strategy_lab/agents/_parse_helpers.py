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
    "extract_json_object",
    "validate_structured_rules",
)
