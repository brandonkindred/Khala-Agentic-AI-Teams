"""Scoring-mode facade for GitHub issue grooming (Phase A).

Picks between the LLM scorer (``issue_llm_scorer.score_issue_via_llm``) and
the heuristic scorer (``issue_heuristic_scorer.score_issue_heuristically``)
per an explicit mode, and owns the ordered fallback policy that
``issue_llm_scorer`` deliberately leaves to its callers: on any ``LLMError``
in ``auto`` mode, fall back to the heuristic scorer instead of aborting Phase
A for that issue. Bugs that are not LLM failures are never swallowed by this
fallback -- they propagate to the caller unchanged.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from llm_service import LLMClient, LLMError

from .issue_heuristic_scorer import score_issue_heuristically
from .issue_llm_scorer import score_issue_via_llm
from .issue_scoring import ScoreBreakdown

logger = logging.getLogger(__name__)

# Scoring modes for GitHub issue grooming (Phase A):
# - "auto" (default): try the LLM scorer, fall back to the heuristic scorer
#   on any LLMError (no provider configured, client/provider failure, or a
#   response that fails to parse/validate).
# - "heuristic_only": never calls the LLM scorer.
SCORING_MODES: tuple[str, ...] = ("auto", "heuristic_only")
DEFAULT_SCORING_MODE = "auto"

_SCORING_MODE_ENV_VAR = "ISSUE_GROOMING_SCORING_MODE"

_DEFAULT_AGENT_KEY = "issue_grooming"


def resolve_scoring_mode(mode: Optional[str] = None) -> str:
    """Resolve the effective GitHub issue-grooming scoring mode.

    Preconditions: ``mode`` is ``None`` or a string.
    Postconditions: returns a value in :data:`SCORING_MODES`. An explicit,
        recognized ``mode`` wins. Otherwise reads the
        ``ISSUE_GROOMING_SCORING_MODE`` environment variable
        (case-insensitive, whitespace-stripped); an unset/empty value
        resolves silently to :data:`DEFAULT_SCORING_MODE`. An explicit
        ``mode`` or environment value that is *set but unrecognized* also
        resolves to :data:`DEFAULT_SCORING_MODE`, but logs a warning so the
        misconfiguration is visible rather than silently masked.
    """
    if mode is not None:
        candidate = mode.strip().lower()
        if candidate in SCORING_MODES:
            return candidate
        logger.warning(
            "Unrecognized scoring mode %r; defaulting to %r (valid values: %s).",
            mode,
            DEFAULT_SCORING_MODE,
            ", ".join(SCORING_MODES),
        )
        return DEFAULT_SCORING_MODE

    env_value = os.getenv(_SCORING_MODE_ENV_VAR, "").strip().lower()
    if not env_value:
        return DEFAULT_SCORING_MODE
    if env_value in SCORING_MODES:
        return env_value
    logger.warning(
        "Unrecognized %s=%r; defaulting to %r (valid values: %s).",
        _SCORING_MODE_ENV_VAR,
        env_value,
        DEFAULT_SCORING_MODE,
        ", ".join(SCORING_MODES),
    )
    return DEFAULT_SCORING_MODE


def score_issue(
    title: str,
    body: str,
    labels: list[str],
    *,
    mode: Optional[str] = None,
    llm_client: Optional[LLMClient] = None,
    agent_key: str = _DEFAULT_AGENT_KEY,
    objective: Optional[str] = None,
    correction_attempts: int = 1,
) -> ScoreBreakdown:
    """Score a GitHub issue's Fibonacci complexity per the resolved mode.

    Preconditions: ``title``/``body``/``labels`` satisfy
        ``build_scoring_prompt``'s preconditions. ``mode`` is ``None`` or a
        string (see :func:`resolve_scoring_mode`).
    Postconditions: with mode ``"heuristic_only"``, returns
        ``score_issue_heuristically(title, body, labels)`` and never calls
        ``score_issue_via_llm`` / the shared LLM client. With mode ``"auto"``
        (the default), attempts ``score_issue_via_llm`` first; if it raises
        ``LLMError`` (no provider configured, a client/provider failure, or a
        parse/validation failure after the self-correction retry), logs a
        warning and returns ``score_issue_heuristically(title, body, labels)``
        instead of raising. Any non-``LLMError`` exception from the LLM path
        propagates unchanged -- it is not treated as a fallback trigger.
    """
    resolved_mode = resolve_scoring_mode(mode)

    if resolved_mode == "heuristic_only":
        return score_issue_heuristically(title, body, labels)

    try:
        return score_issue_via_llm(
            title,
            body,
            labels,
            llm_client=llm_client,
            agent_key=agent_key,
            objective=objective,
            correction_attempts=correction_attempts,
        )
    except LLMError as exc:
        logger.warning(
            "LLM scoring failed (%s: %s); falling back to heuristic scoring.",
            type(exc).__name__,
            exc,
        )
        return score_issue_heuristically(title, body, labels)


__all__ = ["DEFAULT_SCORING_MODE", "SCORING_MODES", "resolve_scoring_mode", "score_issue"]
