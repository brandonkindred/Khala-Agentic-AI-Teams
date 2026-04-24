"""Structured-output helper: ``complete_validated``.

Layers Pydantic schema validation + one self-correction retry on top of the
already-parsed ``dict`` returned by :meth:`LLMClient.complete_json`. Designed
to prevent single-shot ``LLMJsonParseError`` / ``pydantic.ValidationError``
failures from wasting an entire run.

Contract:

- Does **not** call ``llm_service.util.extract_json_from_response``.
  Provider clients handle JSON parsing internally and raise
  :class:`LLMJsonParseError` on failure.
- JSON mode is already enforced unconditionally inside ``complete_json``
  (e.g. the Ollama client sets ``response_format={"type":"json_object"}``),
  so this helper does not need to configure it.
- On success after a correction, logs a single INFO line.
  On terminal failure, logs a single WARNING and re-raises the last error
  with ``correction_attempts_used`` populated.

See :doc:`/backend/agents/llm_service/FEATURE_SPEC_structured_output_contract.md`
for the motivating context (the ``user_agent_founder`` "Startup Founder Testing
Persona" ``LLMJsonParseError``).
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from .interface import LLMClient, LLMJsonParseError, LLMSchemaValidationError

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

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
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:12]


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
    system_prompt: str | None = None,
    temperature: float = 0.0,
    correction_attempts: int = 1,
    context: dict[str, Any] | None = None,
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
        system_prompt: Optional system prompt forwarded to ``complete_json``.
        temperature: Sampling temperature (default 0.0 for structured output).
        correction_attempts: Max corrective follow-up calls (default 1).
            ``0`` disables the retry and matches today's single-shot behavior.
        context: Optional dict forwarded to ``schema.model_validate`` as
            the ``context`` kwarg. Validators can read cross-model state
            from it (e.g. an allowed URL set) and mutate it to surface
            side-channel signals to other validators in the same model
            tree.
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
                system_prompt=system_prompt,
                temperature=temperature,
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
            validated = schema.model_validate(data, context=context)
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


__all__ = ["complete_validated"]
