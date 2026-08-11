"""LLM-backed Fibonacci scorer for GitHub issue grooming.

Calls the shared LLM client (Postgres provider list / ``get_client`` path)
using the prompt/schema contract from ``issue_scoring``. This module owns the
LLM call site only — it does not implement heuristic scoring, a mode switch,
or any Phase A wiring (those are separate, sibling slices of the parent
LLM-assisted Fibonacci scoring effort).
"""

from __future__ import annotations

from typing import Optional

from llm_service import LLMClient
from software_engineering_team.shared.single_shot_review import run_single_shot_review

from .issue_scoring import ScoreBreakdown, build_scoring_prompt

_DEFAULT_AGENT_KEY = "issue_grooming"
_DEFAULT_OBJECTIVE = "score github issue Fibonacci complexity"


def score_issue_via_llm(
    title: str,
    body: str,
    labels: list[str],
    *,
    llm_client: Optional[LLMClient] = None,
    agent_key: str = _DEFAULT_AGENT_KEY,
    objective: Optional[str] = None,
    correction_attempts: int = 1,
) -> ScoreBreakdown:
    """Score a GitHub issue's Fibonacci complexity via the shared LLM client.

    Preconditions: ``title``/``body``/``labels`` satisfy
        ``build_scoring_prompt``'s preconditions. ``llm_client`` is a
        pre-resolved ``LLMClient`` or ``None``.
    Postconditions: the client used is ``llm_client`` when given, else
        ``get_client(agent_key)`` (see ``run_single_shot_review``). Returns a
        ``ScoreBreakdown`` whose four ``*_score`` fields are drawn from
        ``FIBONACCI_COMPLEXITY_VALUES`` (pydantic-enforced, no clamping).
    Raises:
        LLMNotConfiguredError: no LLM provider is configured.
        LLMJsonParseError / LLMSchemaValidationError / pydantic.ValidationError:
            the model reply did not parse or validate against
            ``ScoreBreakdown`` after the self-correction retry. Propagated
            unchanged — a caller wanting heuristic fallback on these must
            catch them itself; that policy lives outside this module.
    """
    prompt = build_scoring_prompt(title, body, labels)
    call_objective = objective if objective is not None else _DEFAULT_OBJECTIVE
    return run_single_shot_review(
        llm_client,
        agent_key,
        prompt,
        schema=ScoreBreakdown,
        objective=call_objective,
        correction_attempts=correction_attempts,
    )


__all__ = ["score_issue_via_llm"]
