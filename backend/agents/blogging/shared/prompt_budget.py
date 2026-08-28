"""Conservative prompt sizing helpers for blogging-agent LLM calls."""

from __future__ import annotations

import logging
from typing import Any, Callable

from llm_service import LLMClientModel, unwrap_client

logger = logging.getLogger(__name__)

DEFAULT_CONTEXT_TOKENS = 16_384
DEFAULT_RESPONSE_RESERVE_TOKENS = 4_000


def resolve_model_context_tokens(model: Any) -> int:
    """Return the smallest context supported by ``model`` and its failovers."""
    sizing_client = model.client if isinstance(model, LLMClientModel) else model
    sizing_client = unwrap_client(sizing_client)
    try:
        min_context = getattr(sizing_client, "get_min_context_tokens", None)
        return int(
            min_context() if callable(min_context) else sizing_client.get_max_context_tokens()
        )
    except (AttributeError, TypeError, ValueError):
        return DEFAULT_CONTEXT_TOKENS
    except Exception:
        logger.warning(
            "Could not resolve an LLM consumer context size; omitting optional prompt text",
            exc_info=True,
        )
        return DEFAULT_RESPONSE_RESERVE_TOKENS


def fit_optional_text_to_prompt(
    text: str,
    *,
    build_prompt: Callable[[str], str],
    system_prompt: str,
    context_tokens: int,
    extra_prompt_reserve_chars: int = 0,
    response_reserve_tokens: int = DEFAULT_RESPONSE_RESERVE_TOKENS,
) -> tuple[str, str]:
    """Build a prompt with as much optional text as its concrete context permits.

    The calculation deliberately treats every character as a token. This is
    conservative for ordinary prose and remains safe for poorly tokenizing web
    content. The prompt without the optional text, the system prompt, retry
    instructions, and a response allowance are all charged before any of ``text``
    is admitted.
    """
    normalized = (text or "").strip()
    empty_prompt = build_prompt("")
    available_chars = max(
        0,
        int(context_tokens)
        - int(response_reserve_tokens)
        - len(system_prompt or "")
        - len(empty_prompt)
        - max(0, int(extra_prompt_reserve_chars)),
    )
    fitted = normalized[:available_chars]
    return build_prompt(fitted), fitted
