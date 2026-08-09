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
from typing import Any, Dict, Iterable

from pydantic import ValidationError

from shared.llm_recovery import extract_json_object as _shared_extract_json_object

from ..spec_dsl import EntryRuleAdapter, ExitRuleAdapter, SizingRuleAdapter


def extract_json_object(text: str) -> Dict[str, Any]:
    """Extract a JSON object from an LLM response, tolerating markdown fences.

    A thin **strict** wrapper over ``shared.llm_recovery.extract_json_object``
    (``repair=False``): the shared engine's string-aware brace scanner locates
    the authoritative balanced ``{...}`` — braces and quotes inside a JSON string
    literal do not affect nesting depth, which matters because the refinement
    agent funnels a full Python program through ``strategy_code`` (comments,
    string/regex literals, f-string format specs routinely carry unbalanced
    braces). Strict mode is deliberate: tolerant ``json-repair`` is disabled so a
    malformed or truncated payload surfaces as a ``ValueError`` and the
    spec-authoring agents re-prompt the model, rather than silently accepting a
    repaired guess of half-written code.

    Preconditions: ``text`` is a string (possibly empty).
    Postconditions: returns the parsed dict for the authoritative balanced
    ``{...}`` in the response. Raises ``ValueError`` when no strictly-valid JSON
    object can be recovered. Used by every spec-authoring or spec-reviewing agent
    in this package — keep the raise-on-failure contract stable.
    """
    result = _shared_extract_json_object(text, repair=False)
    if result is None:
        raise ValueError("No JSON object found in LLM response")
    return result


def coerce_strict_bool(raw: Any) -> bool:
    """Strict-mode boolean coercion for LLM JSON payload fields.

    Plain ``bool(raw)`` would treat ``"false"`` (the string) as truthy and
    wave through a value the caller meant to be negative. Accept only real
    ``bool``s and the case-insensitive string literals ``"true"`` /
    ``"false"``; anything else (unexpected ints, ``None``, malformed JSON
    types) falls closed to ``False``.

    Preconditions: ``raw`` is any value extracted from a parsed LLM JSON
    payload.
    Postconditions: returns a real ``bool``; never raises.
    """
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        normalised = raw.strip().lower()
        if normalised == "true":
            return True
        if normalised == "false":
            return False
    return False


_JSON_CORRECTION_PREAMBLE = """\
Your previous response could not be parsed as a single JSON object
({error}). Return ONLY one JSON object with no surrounding prose, no
markdown fences, and no trailing commentary.{keys_hint}
Every brace must balance.

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
    "coerce_strict_bool",
    "extract_json_object",
    "validate_structured_rules",
)
