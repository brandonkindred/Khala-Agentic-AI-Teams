"""Structured-output helpers for LLM calls.

This module provides both single-call and reasoning-then-formatting
structured output paths:

- ``complete_validated``: single-shot JSON → Pydantic validation with one
  self-correction retry.
- ``complete_json_via_reasoning``: two-pass split (prose reasoning via
  ``complete`` with ``think=True``; then JSON transcription via
  ``complete_json`` with ``think=False``).
- ``complete_validated_via_reasoning``: two-pass split (prose reasoning via
  ``complete``; then schema-validated JSON transcription via
  ``complete_validated`` with corrective retries).

Contract (applies to ``complete_validated`` and the formatting pass helpers):

- Does **not** call ``llm_service.util.extract_json_from_response``.
  Provider clients handle JSON parsing internally and raise
  :class:`LLMJsonParseError` on failure.
- JSON mode is already enforced unconditionally inside ``complete_json``
  (e.g. the Ollama client sets ``response_format={"type":"json_object"}``),
  so this module does not need to configure it.
- On success after a correction, logs a single INFO line.
  On terminal failure, logs a single WARNING. For a parse failure, the
  original :class:`LLMJsonParseError` is mutated (``correction_attempts_used``
  populated) and re-raised as-is; for a schema-validation failure, a NEW
  :class:`LLMSchemaValidationError` is raised (wrapping the last
  ``pydantic.ValidationError`` as its ``cause``) rather than re-raising the
  original.

See :doc:`/backend/agents/llm_service/FEATURE_SPEC_structured_output_contract.md`
for the motivating context (the ``user_agent_founder`` "Startup Founder Testing
Persona" ``LLMJsonParseError``).
"""

from __future__ import annotations

import copy
import json
import logging
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from .interface import LLMClient, LLMJsonParseError, LLMSchemaValidationError
from .util import sha256_fingerprint

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

# Delimiters wrapping the reasoning-pass prose in the formatting prompt.
# Callers' ``formatting_instructions`` / reasoning output should not contain
# these markers; collision would make the prompt structure ambiguous.
_ANALYSIS_START = "--- ANALYSIS ---"
_ANALYSIS_END = "--- END ANALYSIS ---"


def _ensure_prose_has_no_analysis_delimiters(prose: str) -> None:
    """Reject reasoning output that would collide with the wrap markers.

    Preconditions:
        - ``prose`` is the raw string returned by the reasoning ``complete`` call.
    Postconditions:
        - Returns normally when neither delimiter appears in ``prose``.
        - Raises ``ValueError`` if either marker is present, so the formatting
          call never receives an ambiguous prompt.
    """
    if _ANALYSIS_START in prose or _ANALYSIS_END in prose:
        raise ValueError(
            "Reasoning output contains an analysis delimiter, "
            "cannot safely wrap it for the formatting call"
        )


_CORRECTIVE_SUFFIX = (
    "\n\n---\n"
    "Your previous reply was rejected.\n"
    "Error: {error}\n"
    "Required JSON schema:\n{schema}\n"
    "Re-emit ONLY a JSON object satisfying this schema — no prose, no markdown, "
    "no code fences.\n"
    "The previous reply (truncated) was:\n{preview}\n"
)


def _prompt_hash(prompt: str) -> str:
    return sha256_fingerprint(prompt, length=12)


def _build_corrective_prompt(
    original_prompt: str,
    *,
    schema: type[BaseModel],
    error_message: str,
    preview: str,
) -> str:
    return original_prompt + _CORRECTIVE_SUFFIX.format(
        error=error_message,
        schema=json.dumps(schema.model_json_schema(), separators=(",", ":")),
        preview=preview or "(empty)",
    )


def _truncate(text: str, *, limit: int = 500) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


def complete_validated(
    client: LLMClient,
    prompt: str,
    *,
    schema: type[T],
    objective: str,
    system_prompt: str | None = None,
    temperature: float = 0.0,
    correction_attempts: int = 1,
    context: dict[str, Any] | None = None,
    think: "bool | str | None" = False,
    **kwargs: Any,
) -> T:
    """Call ``client.complete_json`` and validate the result against ``schema``.

    On :class:`LLMJsonParseError` or :class:`pydantic.ValidationError`, performs
    up to ``correction_attempts`` corrective follow-up calls. Each corrective
    prompt is the original prompt with an appended block containing the error
    message, the Pydantic schema, and the truncated previous reply.

    Args:
        client: The underlying :class:`LLMClient` (from ``llm_service.get_client``).
        prompt: The user prompt.
        schema: Pydantic ``BaseModel`` subclass the response must satisfy.
        objective: Required short phrase describing *why* this call is made,
            forwarded to ``complete_json`` for log/telemetry attribution.
        system_prompt: Optional system prompt forwarded to ``complete_json``.
        temperature: Sampling temperature (default 0.0 for structured output).
        correction_attempts: Max corrective follow-up calls (default 1).
            ``0`` disables the retry and matches today's single-shot behavior.
        context: Optional dict forwarded to ``schema.model_validate`` as
            the ``context`` kwarg. Validators can read cross-model state
            from it (e.g. an allowed URL set) and mutate it to surface
            side-channel signals to other validators in the same model
            tree.
        think: Forwarded to ``client.complete_json``. Defaults to ``False``:
            every call here requires a schema-conformant JSON reply, and
            extended thinking competes with strict JSON decoding for the
            content channel. Pass an explicit value to override.
        **kwargs: Forwarded to ``client.complete_json``.

    Returns:
        An instance of ``schema`` validated against the final successful reply.

    Raises:
        LLMJsonParseError: The provider could not parse JSON on every attempt.
            ``correction_attempts_used`` is set to the number of corrective
            retries that also failed.
        LLMSchemaValidationError: The provider returned valid JSON but every
            attempt failed Pydantic validation.
            ``correction_attempts_used`` is set analogously.
    """
    if correction_attempts < 0:
        raise ValueError("correction_attempts must be >= 0")

    current_prompt = prompt
    last_parse_error: LLMJsonParseError | None = None
    last_validation_error: ValidationError | None = None
    last_validation_data: dict[str, Any] | None = None
    attempts_used = 0

    # Total call budget = 1 initial + correction_attempts follow-ups.
    for attempt in range(correction_attempts + 1):
        try:
            data = client.complete_json(
                current_prompt,
                objective=objective,
                system_prompt=system_prompt,
                temperature=temperature,
                think=think,
                **kwargs,
            )
        except LLMJsonParseError as exc:
            last_parse_error = exc
            last_validation_error = None
            last_validation_data = None
            if attempt >= correction_attempts:
                break
            attempts_used = attempt + 1
            current_prompt = _build_corrective_prompt(
                prompt,
                schema=schema,
                error_message=str(exc),
                preview=exc.response_preview or "",
            )
            continue

        try:
            # Deep-copy the context on every attempt so mutations performed by
            # validators during a failed attempt (e.g. the sales outreach flow
            # setting ``context["citations_stripped"] = True``) don't leak
            # into the next retry and silently corrupt a clean payload.
            # The caller's original ``context`` dict is never mutated either.
            attempt_context = copy.deepcopy(context) if context is not None else None
            validated = schema.model_validate(data, context=attempt_context)
        except ValidationError as exc:
            last_validation_error = exc
            last_parse_error = None
            last_validation_data = data if isinstance(data, dict) else None
            if attempt >= correction_attempts:
                break
            attempts_used = attempt + 1
            try:
                preview = json.dumps(data, default=str)
            except (TypeError, ValueError):
                preview = repr(data)
            current_prompt = _build_corrective_prompt(
                prompt,
                schema=schema,
                error_message=str(exc),
                preview=_truncate(preview),
            )
            continue

        if attempts_used > 0:
            logger.info(
                "json_self_correction succeeded after %d retry (schema=%s, prompt_hash=%s)",
                attempts_used,
                schema.__name__,
                _prompt_hash(prompt),
            )
        return validated

    # Exhausted all attempts — log WARNING and re-raise the most recent failure.
    if last_parse_error is not None:
        preview_for_log = _truncate(last_parse_error.response_preview or "", limit=500)
        logger.warning(
            "json_self_correction failed terminally (schema=%s, prompt_hash=%s, "
            "kind=parse, attempts_used=%d, preview=%r)",
            schema.__name__,
            _prompt_hash(prompt),
            attempts_used,
            preview_for_log,
        )
        last_parse_error.correction_attempts_used = attempts_used
        raise last_parse_error

    assert last_validation_error is not None  # one of the two paths must be set
    try:
        preview = json.dumps(last_validation_data, default=str)
    except (TypeError, ValueError):
        preview = repr(last_validation_data)
    preview = _truncate(preview)
    logger.warning(
        "json_self_correction failed terminally (schema=%s, prompt_hash=%s, "
        "kind=validation, attempts_used=%d, preview=%r)",
        schema.__name__,
        _prompt_hash(prompt),
        attempts_used,
        preview,
    )
    raise LLMSchemaValidationError(
        f"Response failed Pydantic validation against {schema.__name__}: {last_validation_error}",
        response_preview=preview,
        correction_attempts_used=attempts_used,
        cause=last_validation_error,
    )


def complete_json_via_reasoning(
    client: LLMClient,
    *,
    reasoning_prompt: str,
    reasoning_system_prompt: str | None,
    formatting_instructions: str,
    objective: str,
    formatting_system_prompt: str | None = None,
    schema: dict | type[BaseModel] | None = None,
    reasoning_temperature: float = 0.3,
    temperature: float = 0.0,
    **kwargs: Any,
) -> dict[str, Any]:
    """Two-call split for a JSON-response task that benefits from reasoning.

    Call 1 (``think=True``): a plain-text ``client.complete`` asking the
    model to think the problem through and answer in structured prose — no
    JSON formatting instructions. Call 2 (``think=False``): a
    ``client.complete_json`` that transcribes that prose into the target
    JSON shape via ``formatting_instructions``.

    Rationale: extended thinking competes with strict JSON decoding for the
    content channel (see ``LLMSemanticExhaustionError.schema_forced`` in
    ``interface.py``), so JSON-shaped calls should keep ``think=False`` —
    but genuinely hard reasoning tasks (evaluation, critique, analysis)
    lose quality with thinking off entirely. Splitting the reasoning out
    into its own prose-only call lets it think, while the formatting call
    stays a pure, thinking-off transcription.

    Preconditions:
        * ``objective``, ``reasoning_prompt``, and ``formatting_instructions``
          are non-empty.
        * ``formatting_instructions`` describes the target JSON shape
          (keys/types) — it is prepended to the formatting prompt, ahead of
          the reasoning call's prose output.
        * The caller must ensure ``reasoning_prompt`` and
          ``reasoning_system_prompt`` do not end with JSON-formatting
          instructions. This helper forwards both verbatim to the prose
          reasoning call; a trailing "Return ONLY a JSON object" (or similar)
          would outrank the prose-only intent and collapse the split into a
          redundant re-transcription.
    Postconditions: returns the JSON-decoded dict from the formatting call.
    A step-1 exception propagates immediately (step 2 is never invoked) —
    matching the failure behavior of the single-call form this replaces.
    If the reasoning call's prose contains ``_ANALYSIS_START`` or
    ``_ANALYSIS_END``, raises ``ValueError`` before the formatting call so
    an ambiguous wrap is never sent downstream.

    Args:
        reasoning_prompt: The user prompt for the reasoning call.
        reasoning_system_prompt: System prompt for the reasoning call — the
            original system prompt with any "respond with JSON" tail
            replaced by an instruction to answer in structured prose.
        formatting_instructions: The JSON shape/schema instructions,
            prepended before the reasoning call's prose in the formatting
            prompt.
        formatting_system_prompt: Optional system prompt for the formatting
            call (default ``None`` — the formatting prompt is normally
            self-contained via ``formatting_instructions``).
        schema: Optional JSON Schema dict or Pydantic ``BaseModel`` subclass
            forwarded only to the formatting ``client.complete_json`` call
            for provider-enforced decoding. Never reaches the reasoning call.
        reasoning_temperature: Temperature for the reasoning call (default
            0.3 — some latitude for exploratory reasoning).
        temperature: Temperature for the formatting call (default 0.0 —
            pure transcription).
        **kwargs: Forwarded to the formatting ``client.complete_json`` call
            — EXCEPT ``think``, which is popped and managed internally: the
            reasoning call always uses ``think=True`` and the formatting call
            always uses ``think=False``, regardless of any ``think`` passed in
            ``**kwargs``. Reserved names: do not pass ``objective``,
            ``system_prompt``, ``temperature``, or ``schema`` in ``**kwargs`` —
            this function already forwards those explicitly to the formatting
            ``client.complete_json`` call, so a same-named entry in
            ``**kwargs`` raises ``TypeError: got multiple values for
            keyword argument`` (use the dedicated parameters above instead).
    """
    if not objective:
        raise ValueError("objective must be non-empty")
    if not reasoning_prompt:
        raise ValueError("reasoning_prompt must be non-empty")
    if not formatting_instructions:
        raise ValueError("formatting_instructions must be non-empty")
    kwargs.pop("think", None)

    prose = client.complete(
        reasoning_prompt,
        objective=f"{objective} (reasoning)",
        system_prompt=reasoning_system_prompt,
        temperature=reasoning_temperature,
        think=True,
    )
    _ensure_prose_has_no_analysis_delimiters(prose)
    format_prompt = f"{formatting_instructions}\n\n{_ANALYSIS_START}\n{prose}\n{_ANALYSIS_END}"
    return client.complete_json(
        format_prompt,
        objective=f"{objective} (format)",
        system_prompt=formatting_system_prompt,
        temperature=temperature,
        think=False,
        schema=schema,
        **kwargs,
    )


def complete_validated_via_reasoning(
    client: LLMClient,
    *,
    schema: type[T],
    reasoning_prompt: str,
    reasoning_system_prompt: str | None,
    objective: str,
    formatting_instructions: str | None = None,
    formatting_system_prompt: str | None = None,
    reasoning_temperature: float = 0.3,
    temperature: float = 0.0,
    correction_attempts: int = 1,
    context: dict[str, Any] | None = None,
    **kwargs: Any,
) -> T:
    """Same reasoning/formatting split as :func:`complete_json_via_reasoning`,
    but the formatting call goes through :func:`complete_validated` (schema
    validation + self-correction retry) instead of a bare ``complete_json``.

    The formatting user prompt defaults to a generic "transcribe into JSON
    matching the schema" instruction. Pass ``formatting_instructions`` when
    the caller has additional JSON-shape guidance (keys/types) for the user
    prompt. Independently, ``formatting_system_prompt`` is forwarded as the
    system prompt on every formatting attempt (first pass and corrective
    retries), so schema guidance placed there is available on the first
    attempt — callers such as the sales critics put the full JSON contract
    there. The Pydantic ``schema`` argument itself only reaches the model via
    :func:`complete_validated`'s corrective retry prompts (after a first-pass
    parse/validation failure), not the first attempt's user prompt.

    Preconditions: ``objective`` and ``reasoning_prompt`` are non-empty.
    ``reasoning_prompt`` / ``reasoning_system_prompt`` must not end with
    JSON-formatting instructions (same caller obligation as
    :func:`complete_json_via_reasoning`). Unlike that helper,
    ``formatting_instructions`` is optional here — when omitted, the
    hardcoded generic transcription instruction above is used alone.
    Postconditions/failure semantics: see :func:`complete_json_via_reasoning`;
    the formatting call additionally retries up to ``correction_attempts``
    times on a parse/validation failure, exactly as :func:`complete_validated`
    does today. ``**kwargs`` is forwarded to :func:`complete_validated` (and
    therefore to the formatting ``client.complete_json`` call) EXCEPT
    ``think``, which is popped and managed internally — the reasoning call
    always uses ``think=True`` and the formatting call always uses
    ``think=False``, regardless of any ``think`` passed in ``**kwargs``.
    Reserved names: do not pass ``objective``, ``system_prompt``,
    ``temperature``, ``correction_attempts``, or ``context`` in ``**kwargs`` —
    this function already forwards those explicitly to :func:`complete_validated`,
    so a same-named entry in ``**kwargs`` raises ``TypeError: got multiple
    values for keyword argument`` (use the dedicated parameters above
    instead).
    """
    if not objective:
        raise ValueError("objective must be non-empty")
    if not reasoning_prompt:
        raise ValueError("reasoning_prompt must be non-empty")
    kwargs.pop("think", None)

    prose = client.complete(
        reasoning_prompt,
        objective=f"{objective} (reasoning)",
        system_prompt=reasoning_system_prompt,
        temperature=reasoning_temperature,
        think=True,
    )
    _ensure_prose_has_no_analysis_delimiters(prose)
    instructions_block = f"{formatting_instructions}\n\n" if formatting_instructions else ""
    format_prompt = (
        "Convert the following analysis into a single JSON object matching "
        "the required schema. Return JSON only — no markdown fences, no "
        f"prose outside the object.\n\n{instructions_block}"
        f"{_ANALYSIS_START}\n{prose}\n{_ANALYSIS_END}"
    )
    return complete_validated(
        client,
        format_prompt,
        schema=schema,
        objective=f"{objective} (format)",
        system_prompt=formatting_system_prompt,
        temperature=temperature,
        correction_attempts=correction_attempts,
        context=context,
        think=False,
        **kwargs,
    )


__all__ = [
    "complete_validated",
    "complete_json_via_reasoning",
    "complete_validated_via_reasoning",
]
