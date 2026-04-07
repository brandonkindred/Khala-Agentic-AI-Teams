"""
LLM client adapter for the Agent Provisioning Team.

This is the integration seam for the LLM-driven phases (currently the
`documentation` phase, with `setup` planning to follow). It is intentionally
thin so it can be wired to the shared `backend.agents.llm_service` client
when that integration lands this week.

Until then, `LLMClient.complete()` returns a deterministic, clearly-marked
fallback string so the rest of the pipeline keeps working and tests stay
hermetic. The fallback path logs a single WARNING per process so we never
silently ship un-LLM'd output to users.

Design notes
------------
- Inputs that originate from user-controlled manifests (agent_id, tool
  names, requirements) are sanitized before they ever hit a prompt.
- The client exposes `complete()` (single-shot) today; tool-call / function
  schemas will be layered on top once the LLM service exposes them.
- Provider selection follows the project-wide env vars: LLM_PROVIDER,
  LLM_BASE_URL, LLM_MODEL.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

# Characters that have no business inside an interpolated prompt variable.
# We allow letters, digits, basic punctuation and whitespace; everything else
# is replaced with `_`. This is a defense-in-depth measure against prompt
# injection through manifest fields.
_PROMPT_VAR_ALLOWED = re.compile(r"[^A-Za-z0-9 _\-./:@,()\[\]{}+=#'\"\n\t]")
_PROMPT_VAR_MAX_LEN = 4000


def sanitize_prompt_var(value: object, *, max_len: int = _PROMPT_VAR_MAX_LEN) -> str:
    """Make a manifest-supplied value safe to interpolate into an LLM prompt.

    - Coerces to str
    - Strips disallowed characters
    - Caps length to avoid prompt-bomb / context-blowing inputs
    """
    text = "" if value is None else str(value)
    text = _PROMPT_VAR_ALLOWED.sub("_", text)
    if len(text) > max_len:
        text = text[:max_len] + "…[truncated]"
    return text


@dataclass
class LLMRequest:
    """A single LLM completion request."""

    system: str
    user: str
    temperature: float = 0.2
    max_tokens: int = 1024


class LLMClient:
    """Thin adapter around the project LLM service.

    The real wiring (Ollama / Claude via `backend.agents.llm_service`) lands
    this week. Until then `complete()` returns a deterministic fallback so
    the documentation phase still produces output and tests remain stable.
    """

    _warned_fallback = False

    def __init__(
        self,
        provider: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
    ) -> None:
        self.provider = provider or os.getenv("LLM_PROVIDER", "ollama")
        self.base_url = base_url or os.getenv("LLM_BASE_URL", "")
        self.model = model or os.getenv("LLM_MODEL", "")

    @property
    def is_configured(self) -> bool:
        """True once a real LLM endpoint has been wired in."""
        # NOTE: flip this to a real readiness check when llm_service lands.
        return False

    def complete(self, request: LLMRequest) -> str:
        """Run a single completion. Returns text.

        Falls back to a deterministic stub when no LLM is configured. The
        fallback is clearly labeled so it never gets confused for real LLM
        output downstream.
        """
        if not self.is_configured:
            if not LLMClient._warned_fallback:
                logger.warning(
                    "LLMClient: no LLM provider wired yet — using deterministic fallback. "
                    "Wire backend.agents.llm_service to enable real completions."
                )
                LLMClient._warned_fallback = True
            return self._fallback(request)

        # TODO(this week): delegate to backend.agents.llm_service
        raise NotImplementedError("LLM provider wiring pending")

    @staticmethod
    def _fallback(request: LLMRequest) -> str:
        # A small, structured fallback so callers can detect it explicitly.
        return f"[llm-fallback] {request.user.strip()[:512]}"
