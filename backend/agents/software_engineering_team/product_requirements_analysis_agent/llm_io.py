"""
LLM invocation seam for the Product Requirements Analysis Agent.

Every raw LLM round-trip in this agent funnels through one place — a single fresh
Strands ``Agent`` per call, coerced to text and (optionally) parsed as JSON with
fence stripping. Centralizing it keeps the workflow modules free of the
``str(Agent(model=...)(prompt))`` idiom.

Pure functions parameterized by an explicit Strands ``model`` — no agent state.
"""

from __future__ import annotations

from typing import Optional

from strands import Agent
from strands.models.model import Model

from llm_service import LLMJsonParseError
from software_engineering_team.shared.json_utils import parse_json_object


def parse_llm_json(raw: str) -> Optional[dict]:
    """Parse JSON from LLM output via the shared recovery-ladder parser.

    Delegates to the canonical
    :func:`software_engineering_team.shared.json_utils.parse_json_object`
    (markdown fences, prose-prefix stripping, trailing-comma repair).

    Preconditions: ``raw`` is a string.
    Postconditions: returns a dict on success, None on any parse failure or
        non-dict recovery; never raises.
    """
    text = raw.strip()
    if not text:
        return None
    try:
        return parse_json_object(text)
    except (LLMJsonParseError, TypeError):
        return None


def call_llm_text(model: Model, prompt: str) -> str:
    """Run one Strands ``Agent`` round-trip and return the stripped text.

    Single seam for every raw LLM invocation in this agent; collapses the former
    ``str(Agent(model=model, callback_handler=None)(prompt))`` idiom that was
    copy-pasted across the workflow.

    Preconditions: ``prompt`` is a non-empty string; ``model`` is a Strands
        ``Model``.
    Postconditions: returns the model's response coerced to ``str`` and
        whitespace-stripped (possibly empty).

    Raises:
        ValueError: if ``prompt`` is not a non-empty string. (An explicit raise
        rather than ``assert`` so the precondition holds under ``-O``.)
    """
    if not isinstance(prompt, str) or not prompt:
        raise ValueError("prompt must be a non-empty string")
    return str(Agent(model=model, callback_handler=None)(prompt)).strip()


def call_llm_json(model: Model, prompt: str) -> Optional[dict]:
    """Run one LLM round-trip and parse the response as a JSON object.

    Builds on :func:`call_llm_text` + :func:`parse_llm_json` (fence-aware).

    Preconditions: ``prompt`` is a non-empty string; ``model`` is a Strands
        ``Model``.
    Postconditions: returns the parsed ``dict`` on success, or ``None`` when the
        response is empty or not valid JSON (never raises on parse failure).
    """
    raw = call_llm_text(model, prompt)
    return parse_llm_json(raw) if raw else None
