"""
Thin LLM layer for Personal Assistant team.

All provider logic lives in llm_service. This module re-exports from llm_service
and adds PA-specific JSONExtractionFailure (wrapping LLMJsonParseError) and a
client wrapper that re-raises central LLM errors as JSONExtractionFailure for
backward compatibility.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

from llm_service import (
    LLMClient as _BaseLLMClient,
)
from llm_service import (
    LLMError,
    LLMJsonParseError,
    get_client,
)


class JSONExtractionFailure(LLMJsonParseError):
    """
    Raised when JSON extraction fails. Subclasses llm_service.LLMJsonParseError.
    PA agents can catch this for backward compatibility; central client raises LLMJsonParseError.
    """

    def __init__(
        self,
        message: str,
        *,
        original_prompt: str = "",
        attempts_made: int = 1,
        continuation_attempts: int = 0,
        decomposition_attempts: int = 0,
        raw_responses: Optional[List[str]] = None,
        recovery_suggestions: Optional[List[str]] = None,
        error_kind: str = "json_parse",
        response_preview: str = "",
    ):
        super().__init__(message, error_kind=error_kind, response_preview=response_preview)
        self.original_prompt = original_prompt
        self.attempts_made = attempts_made
        self.continuation_attempts = continuation_attempts
        self.decomposition_attempts = decomposition_attempts
        self.raw_responses = raw_responses or []
        self.recovery_suggestions = recovery_suggestions or []

    def __str__(self) -> str:
        suggestions = "\n".join(f"  {i + 1}. {s}" for i, s in enumerate(self.recovery_suggestions))
        last_response = (
            self.raw_responses[-1][:500] if self.raw_responses else "No responses captured"
        )
        return (
            f"\n{'=' * 80}\n"
            f"CRITICAL: JSON EXTRACTION FAILED\n"
            f"{'=' * 80}\n\n"
            f"Error: {self.args[0]}\n\n"
            f"Recovery Attempts Made:\n"
            f"  - Total attempts: {self.attempts_made}\n"
            f"  - Continuation requests: {self.continuation_attempts}\n"
            f"  - Task decompositions: {self.decomposition_attempts}\n\n"
            f"HOW TO RESOLVE:\n{suggestions}\n\n"
            f"Original prompt (first 500 chars):\n"
            f"  {self.original_prompt[:500]}{'...' if len(self.original_prompt) > 500 else ''}\n\n"
            f"Last raw response (first 500 chars):\n"
            f"  {last_response}\n"
            f"{'=' * 80}\n"
        )


class LLMClient(_BaseLLMClient):
    """
    PA-specific LLM client base class.

    Subclasses implement ``_ollama_complete`` as the template method.
    ``complete_json`` is provided here with robust extraction (truncation
    continuation + decomposition) so tests using ``MockLLMClient`` work.
    """

    MAX_CONTINUATION_ATTEMPTS = 3
    MAX_DECOMPOSITION_ATTEMPTS = 20

    def _ollama_complete(
        self,
        prompt: str,
        *,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        system_prompt: Optional[str] = None,
        json_mode: bool = False,
    ) -> str:
        """Override in subclasses to call the LLM and return raw text."""
        raise NotImplementedError("Subclasses must implement _ollama_complete")

    # ------------------------------------------------------------------ #
    # llm_service.LLMClient abstract implementation                        #
    # ------------------------------------------------------------------ #

    def complete_json(
        self,
        prompt: str,
        *,
        temperature: float = 0.0,
        system_prompt: Optional[str] = None,
        expected_keys: Optional[List[str]] = None,
        decomposition_hints: Optional[List[str]] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """Robust JSON extraction with continuation and decomposition fallbacks."""
        raw_responses: List[str] = []
        attempts_made = 0
        continuation_attempts = 0
        decomposition_attempts = 0

        # --- direct attempt ---
        response = self._ollama_complete(
            prompt, temperature=temperature, system_prompt=system_prompt, json_mode=True
        )
        raw_responses.append(response)
        attempts_made += 1

        parsed = self._try_parse_json(response)
        if parsed is not None:
            return parsed

        # --- continuation on truncation ---
        accumulated = response
        for _ in range(self.MAX_CONTINUATION_ATTEMPTS):
            if not self._is_json_truncated(accumulated):
                break
            continuation = self._ollama_complete(
                f"Continue from where you left off:\n{accumulated}",
                temperature=temperature,
                system_prompt=system_prompt,
                json_mode=True,
            )
            raw_responses.append(continuation)
            attempts_made += 1
            continuation_attempts += 1
            accumulated = accumulated + continuation
            parsed = self._try_parse_json(accumulated)
            if parsed is not None:
                return parsed

        # --- decomposition ---
        combined: Dict[str, Any] = {}
        subtasks = self._build_subtasks(prompt, expected_keys, decomposition_hints)
        for subtask_prompt, subtask_key in subtasks:
            if decomposition_attempts >= self.MAX_DECOMPOSITION_ATTEMPTS:
                break
            resp = self._ollama_complete(
                subtask_prompt, temperature=temperature, system_prompt=system_prompt, json_mode=True
            )
            raw_responses.append(resp)
            attempts_made += 1
            decomposition_attempts += 1
            p = self._try_parse_json(resp)
            if p is not None:
                if subtask_key:
                    combined[subtask_key] = p.get(subtask_key, p)
                else:
                    combined.update(p)

        if combined:
            return combined

        suggestions = self._recovery_suggestions(prompt, raw_responses)
        raise JSONExtractionFailure(
            "Failed to extract valid JSON after all recovery attempts",
            original_prompt=prompt,
            attempts_made=attempts_made,
            continuation_attempts=continuation_attempts,
            decomposition_attempts=decomposition_attempts,
            raw_responses=raw_responses,
            recovery_suggestions=suggestions,
        )

    def complete(
        self,
        prompt: str,
        *,
        temperature: float = 0.0,
        max_tokens: Optional[int] = None,
        system_prompt: Optional[str] = None,
    ) -> str:
        return self._ollama_complete(
            prompt,
            temperature=temperature,
            max_tokens=max_tokens,
            system_prompt=system_prompt,
        )

    def get_max_context_tokens(self) -> int:
        return 4096

    # ------------------------------------------------------------------ #
    # Helpers                                                              #
    # ------------------------------------------------------------------ #

    def _is_json_truncated(self, text: str) -> bool:
        text = text.strip()
        if text.count("{") > text.count("}"):
            return True
        if text.count("[") > text.count("]"):
            return True
        if text.endswith(",") or text.endswith(":"):
            return True
        if re.search(r'"\s*$', text) and not text.rstrip().endswith('"}'):
            if text.count('"') % 2 != 0:
                return True
        return False

    def _try_parse_json(self, text: str) -> Optional[Dict[str, Any]]:
        text = text.strip()
        if not text:
            return None
        # code block
        m = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
        if m:
            text = m.group(1).strip()
        # direct parse
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return parsed
            if isinstance(parsed, list):
                return {"items": parsed}
        except json.JSONDecodeError:
            pass
        # object slice
        start, end = text.find("{"), text.rfind("}") + 1
        if start != -1 and end > start:
            try:
                return json.loads(text[start:end])
            except json.JSONDecodeError:
                pass
        # array slice → wrap
        start, end = text.find("["), text.rfind("]") + 1
        if start != -1 and end > start:
            try:
                return {"items": json.loads(text[start:end])}
            except json.JSONDecodeError:
                pass
        return None

    def _build_subtasks(
        self,
        prompt: str,
        expected_keys: Optional[List[str]],
        hints: Optional[List[str]],
    ) -> List[tuple]:
        if expected_keys:
            return [
                (
                    f"Focus ONLY on extracting the '{k}' field. "
                    f"Return a small JSON object with just '{k}'.\n\n{prompt}",
                    k,
                )
                for k in expected_keys
            ]
        if hints:
            return [
                (
                    f"Focus ONLY on this aspect: {h}\n\n{prompt}",
                    h.split()[0].lower(),
                )
                for h in hints
            ]
        return [
            (
                "Simplify your response. Return the MINIMUM viable JSON.\n\n" + prompt,
                "main",
            )
        ]

    def _recovery_suggestions(self, prompt: str, raw_responses: List[str]) -> List[str]:
        suggestions = [
            "Simplify the request: Break your request into smaller, more specific parts."
        ]
        if len(prompt) > 2000:
            suggestions.append(
                f"Reduce prompt size: Your prompt is {len(prompt)} characters. "
                "Try reducing context or being more concise."
            )
        suggestions.append("Use a larger model with higher token limits.")
        suggestions.append("Check LLM configuration: SW_LLM_MODEL and SW_LLM_BASE_URL.")
        if raw_responses and (
            "error" in raw_responses[-1].lower() or "cannot" in raw_responses[-1].lower()
        ):
            suggestions.append("Review LLM response: The model may be refusing the request.")
        suggestions.append("Increase timeout: Set SW_LLM_TIMEOUT to a higher value.")
        return suggestions


class _PALLMClientWrapper(LLMClient):
    """Wraps central LLMClient and re-raises LLMJsonParseError as JSONExtractionFailure."""

    def __init__(self, inner: _BaseLLMClient):
        self._inner = inner

    def _ollama_complete(
        self,
        prompt: str,
        *,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        system_prompt: Optional[str] = None,
        json_mode: bool = False,
    ) -> str:
        return self._inner.complete(
            prompt,
            temperature=temperature,
            max_tokens=max_tokens or 4096,
            system_prompt=system_prompt,
        )

    def complete_json(
        self,
        prompt: str,
        *,
        temperature: float = 0.0,
        system_prompt: Optional[str] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        try:
            return self._inner.complete_json(
                prompt,
                temperature=temperature,
                system_prompt=system_prompt,
            )
        except LLMJsonParseError as e:
            raise JSONExtractionFailure(
                str(e),
                original_prompt=prompt,
                attempts_made=1,
                continuation_attempts=0,
                decomposition_attempts=0,
                raw_responses=[getattr(e, "response_preview", "") or ""],
                recovery_suggestions=[
                    "Check that the prompt asks for valid JSON.",
                    "Try simplifying the request or breaking it into smaller parts.",
                ],
                response_preview=getattr(e, "response_preview", ""),
            ) from e

    def complete(
        self,
        prompt: str,
        *,
        temperature: float = 0.0,
        max_tokens: Optional[int] = None,
        system_prompt: Optional[str] = None,
    ) -> str:
        return self._inner.complete(
            prompt,
            temperature=temperature,
            max_tokens=max_tokens or 4096,
            system_prompt=system_prompt,
        )

    def get_max_context_tokens(self) -> int:
        return self._inner.get_max_context_tokens()


def get_llm_client_with_pa_exceptions(agent_key: Optional[str] = None) -> LLMClient:
    """Return a client that re-raises LLMJsonParseError as JSONExtractionFailure (for PA agents)."""
    return _PALLMClientWrapper(get_client(agent_key))


def get_llm_client(agent_key: Optional[str] = None) -> LLMClient:
    """Return PA wrapper around central client (re-raises LLMJsonParseError as JSONExtractionFailure)."""
    return get_llm_client_with_pa_exceptions(agent_key)


__all__ = ["JSONExtractionFailure", "LLMClient", "LLMError", "get_llm_client"]
