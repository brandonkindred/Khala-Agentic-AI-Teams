"""LLM client wrapper for Personal Assistant team with robust JSON extraction."""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)


class LLMError(Exception):
    """Raised when LLM operations fail."""


class JSONExtractionFailure(LLMError):
    """
    Raised when JSON extraction fails after all recovery attempts.
    
    This is a LOUD failure that provides detailed information about what happened
    and how the user might resolve the issue.
    """

    def __init__(
        self,
        message: str,
        original_prompt: str,
        attempts_made: int,
        continuation_attempts: int,
        decomposition_attempts: int,
        raw_responses: List[str],
        recovery_suggestions: List[str],
    ):
        super().__init__(message)
        self.original_prompt = original_prompt
        self.attempts_made = attempts_made
        self.continuation_attempts = continuation_attempts
        self.decomposition_attempts = decomposition_attempts
        self.raw_responses = raw_responses
        self.recovery_suggestions = recovery_suggestions

    def __str__(self) -> str:
        suggestions = "\n".join(f"  {i+1}. {s}" for i, s in enumerate(self.recovery_suggestions))
        last_response = self.raw_responses[-1][:800] if self.raw_responses else "No responses captured"
        
        return (
            f"\n{'='*80}\n"
            f"CRITICAL: JSON EXTRACTION FAILED\n"
            f"{'='*80}\n\n"
            f"Error: {self.args[0]}\n\n"
            f"Recovery Attempts Made:\n"
            f"  - Total LLM calls: {self.attempts_made}\n"
            f"  - Continuation requests (asking LLM to finish): {self.continuation_attempts}\n"
            f"  - Task decompositions (breaking into smaller tasks): {self.decomposition_attempts}\n\n"
            f"HOW TO RESOLVE THIS ISSUE:\n{suggestions}\n\n"
            f"Original prompt (first 500 chars):\n"
            f"  {self.original_prompt[:500]}{'...' if len(self.original_prompt) > 500 else ''}\n\n"
            f"Last raw response (first 800 chars):\n"
            f"  {last_response}\n"
            f"{'='*80}\n"
        )


class LLMClient:
    """
    LLM client for making completions with robust JSON extraction.
    
    Features:
    - Automatic continuation requests for truncated JSON
    - Task decomposition for complex requests
    - Up to 10 decomposition attempts before failing
    - Loud, informative failures with recovery suggestions
    """

    MAX_CONTINUATION_ATTEMPTS = 3
    MAX_DECOMPOSITION_ATTEMPTS = 10

    def __init__(
        self,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        timeout: float = 180.0,
        max_retries: int = 3,
    ) -> None:
        """
        Initialize the LLM client.
        
        Args:
            base_url: Ollama base URL. Defaults to SW_LLM_BASE_URL or localhost.
            model: Model name. Defaults to SW_LLM_MODEL.
            timeout: Request timeout in seconds.
            max_retries: Maximum retry attempts for network errors.
        """
        self.base_url = base_url or os.getenv("SW_LLM_BASE_URL", "http://127.0.0.1:11434")
        self.model = model or os.getenv("SW_LLM_MODEL", "llama3.2")
        self.timeout = timeout
        self.max_retries = max_retries
        
        self._provider = os.getenv("SW_LLM_PROVIDER", "ollama")
        if self._provider == "dummy":
            logger.warning("Using dummy LLM provider")

    def complete(
        self,
        prompt: str,
        *,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        system_prompt: Optional[str] = None,
    ) -> str:
        """
        Generate a text completion.
        
        Args:
            prompt: The prompt to complete
            temperature: Sampling temperature (0.0-1.0)
            max_tokens: Maximum tokens to generate
            system_prompt: Optional system prompt
            
        Returns:
            Generated text
        """
        if self._provider == "dummy":
            return self._dummy_complete(prompt)
        
        return self._ollama_complete(
            prompt,
            temperature=temperature,
            max_tokens=max_tokens,
            system_prompt=system_prompt,
        )

    def complete_json(
        self,
        prompt: str,
        *,
        temperature: float = 0.2,
        system_prompt: Optional[str] = None,
        expected_keys: Optional[List[str]] = None,
        decomposition_hints: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """
        Generate a JSON completion with robust extraction and recovery.
        
        This method will:
        1. Attempt direct JSON extraction
        2. If JSON is truncated, request continuations (up to 3 times)
        3. If still failing, decompose into smaller tasks (up to 10 times)
        4. If all attempts fail, raise a LOUD error with recovery suggestions
        
        Args:
            prompt: The prompt (should request JSON output)
            temperature: Sampling temperature
            system_prompt: Optional system prompt
            expected_keys: Keys expected in JSON (helps with decomposition)
            decomposition_hints: Hints for breaking down the task
            
        Returns:
            Parsed JSON dict
            
        Raises:
            JSONExtractionFailure: If extraction fails after all recovery attempts
        """
        if self._provider == "dummy":
            return self._dummy_complete_json(prompt)
        
        return self._robust_json_extraction(
            prompt,
            temperature=temperature,
            system_prompt=system_prompt,
            expected_keys=expected_keys,
            decomposition_hints=decomposition_hints,
        )

    def _robust_json_extraction(
        self,
        prompt: str,
        *,
        temperature: float = 0.2,
        system_prompt: Optional[str] = None,
        expected_keys: Optional[List[str]] = None,
        decomposition_hints: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """
        Robust JSON extraction with multiple recovery strategies.
        
        NEVER attempts partial JSON extraction. If information is missing,
        this is treated as critical and recovery is attempted.
        """
        raw_responses: List[str] = []
        total_attempts = 0
        continuation_attempts = 0
        decomposition_attempts = 0
        
        response = self._ollama_complete(
            prompt,
            temperature=temperature,
            system_prompt=system_prompt,
            json_mode=True,
        )
        raw_responses.append(response)
        total_attempts += 1
        
        parsed = self._try_parse_json(response)
        if parsed is not None:
            return parsed
        
        if self._is_json_truncated(response):
            logger.info("Detected truncated JSON, attempting continuation...")
            
            accumulated = response
            for i in range(self.MAX_CONTINUATION_ATTEMPTS):
                continuation_attempts += 1
                total_attempts += 1
                
                continuation_prompt = self._build_continuation_prompt(accumulated)
                
                continuation = self._ollama_complete(
                    continuation_prompt,
                    temperature=temperature,
                    json_mode=False,
                )
                raw_responses.append(continuation)
                
                accumulated = self._merge_json_responses(accumulated, continuation)
                
                parsed = self._try_parse_json(accumulated)
                if parsed is not None:
                    logger.info(
                        "JSON extraction succeeded after %d continuation attempt(s)",
                        i + 1
                    )
                    return parsed
                
                if not self._is_json_truncated(accumulated):
                    logger.warning(
                        "Response no longer appears truncated but JSON is still invalid"
                    )
                    break
        
        logger.info("Attempting task decomposition...")
        
        subtasks = self._decompose_task(prompt, expected_keys, decomposition_hints)
        
        if subtasks:
            combined_result: Dict[str, Any] = {}
            subtask_success = True
            
            for subtask in subtasks:
                if decomposition_attempts >= self.MAX_DECOMPOSITION_ATTEMPTS:
                    logger.error(
                        "Reached maximum decomposition attempts (%d)",
                        self.MAX_DECOMPOSITION_ATTEMPTS
                    )
                    subtask_success = False
                    break
                
                decomposition_attempts += 1
                total_attempts += 1
                
                subtask_response = self._ollama_complete(
                    subtask["prompt"],
                    temperature=temperature,
                    json_mode=True,
                )
                raw_responses.append(subtask_response)
                
                subtask_parsed = self._try_parse_json(subtask_response)
                
                if subtask_parsed is None and self._is_json_truncated(subtask_response):
                    subtask_parsed = self._attempt_subtask_continuation(
                        subtask["prompt"],
                        subtask_response,
                        temperature,
                        raw_responses,
                    )
                    total_attempts += 1
                
                if subtask_parsed is not None:
                    if subtask["key"]:
                        combined_result[subtask["key"]] = subtask_parsed.get(
                            subtask["key"], subtask_parsed
                        )
                    else:
                        combined_result.update(subtask_parsed)
                    logger.info("Subtask '%s' completed successfully", subtask["key"])
                else:
                    logger.warning("Subtask '%s' failed", subtask["key"])
                    subtask_success = False
            
            if combined_result and subtask_success:
                return combined_result
        
        raise JSONExtractionFailure(
            message="Failed to extract valid JSON after all recovery attempts",
            original_prompt=prompt,
            attempts_made=total_attempts,
            continuation_attempts=continuation_attempts,
            decomposition_attempts=decomposition_attempts,
            raw_responses=raw_responses,
            recovery_suggestions=self._generate_recovery_suggestions(prompt, raw_responses),
        )

    def _try_parse_json(self, text: str) -> Optional[Dict[str, Any]]:
        """
        Attempt to parse JSON from text.
        
        Returns None if parsing fails - does NOT attempt partial extraction.
        """
        if not text:
            return None
        
        text = text.strip()
        
        json_match = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
        if json_match:
            text = json_match.group(1).strip()
        
        try:
            result = json.loads(text)
            if isinstance(result, dict):
                return result
            elif isinstance(result, list):
                return {"items": result}
            return None
        except json.JSONDecodeError:
            pass
        
        start = text.find("{")
        end = text.rfind("}") + 1
        if start != -1 and end > start:
            candidate = text[start:end]
            try:
                result = json.loads(candidate)
                if isinstance(result, dict):
                    return result
            except json.JSONDecodeError:
                pass
        
        return None

    def _is_json_truncated(self, text: str) -> bool:
        """Detect if JSON appears to be truncated/incomplete."""
        if not text:
            return False
        
        text = text.strip()
        
        open_braces = text.count("{") - text.count("}")
        open_brackets = text.count("[") - text.count("]")
        
        if open_braces > 0 or open_brackets > 0:
            return True
        
        if text.endswith(",") or text.endswith(":") or text.endswith('"'):
            if text.count('"') % 2 != 0:
                return True
            if text.endswith(",") or text.endswith(":"):
                return True
        
        return False

    def _build_continuation_prompt(self, partial_response: str) -> str:
        """Build a prompt asking the LLM to continue the JSON."""
        truncated_part = partial_response[-2000:] if len(partial_response) > 2000 else partial_response
        
        return (
            "Your previous JSON response was cut off before completion. "
            "Continue EXACTLY from where you stopped. Do NOT restart or repeat earlier content. "
            "Output ONLY the remaining JSON content needed to complete the response.\n\n"
            "The response ended with:\n"
            f"```\n{truncated_part}\n```\n\n"
            "Continue the JSON (start immediately with the next character):"
        )

    def _merge_json_responses(self, base: str, continuation: str) -> str:
        """Merge a base JSON response with its continuation."""
        base = base.rstrip()
        continuation = continuation.strip()
        
        if continuation.startswith("```"):
            json_match = re.search(r"```(?:json)?\s*([\s\S]*?)```", continuation)
            if json_match:
                continuation = json_match.group(1).strip()
        
        while continuation and continuation[0] in "{[ \n\t":
            if continuation[0] in "{[":
                inner_close = "}" if continuation[0] == "{" else "]"
                depth = 1
                for i, c in enumerate(continuation[1:], 1):
                    if c == continuation[0]:
                        depth += 1
                    elif c == inner_close:
                        depth -= 1
                        if depth == 0:
                            continuation = continuation[i+1:].strip()
                            break
                else:
                    break
            else:
                continuation = continuation[1:]
        
        return base + continuation

    def _decompose_task(
        self,
        original_prompt: str,
        expected_keys: Optional[List[str]],
        hints: Optional[List[str]],
    ) -> List[Dict[str, Any]]:
        """Decompose a task into smaller subtasks."""
        subtasks = []
        
        if expected_keys:
            for key in expected_keys:
                subtask_prompt = (
                    f"Extract ONLY the '{key}' field from this request. "
                    f"Return a minimal JSON object containing just '{key}'.\n\n"
                    f"Request:\n{original_prompt}\n\n"
                    f"Return JSON with only '{key}':"
                )
                subtasks.append({"key": key, "prompt": subtask_prompt})
            return subtasks
        
        if hints:
            for hint in hints:
                key = hint.split()[0].lower().replace(":", "").replace(",", "")
                subtask_prompt = (
                    f"Focus on this aspect ONLY: {hint}\n\n"
                    f"Original request:\n{original_prompt}\n\n"
                    f"Return minimal JSON for just this aspect:"
                )
                subtasks.append({"key": key, "prompt": subtask_prompt})
            return subtasks
        
        subtasks = [
            {
                "key": None,
                "prompt": (
                    "Simplify your response. Return the MINIMUM viable JSON. "
                    "Use shorter strings, fewer array items, and omit optional fields.\n\n"
                    f"Request:\n{original_prompt}\n\n"
                    "Return minimal JSON:"
                ),
            }
        ]
        
        return subtasks

    def _attempt_subtask_continuation(
        self,
        subtask_prompt: str,
        partial_response: str,
        temperature: float,
        raw_responses: List[str],
    ) -> Optional[Dict[str, Any]]:
        """Attempt continuation for a subtask."""
        accumulated = partial_response
        
        for _ in range(2):
            continuation_prompt = self._build_continuation_prompt(accumulated)
            continuation = self._ollama_complete(
                continuation_prompt,
                temperature=temperature,
                json_mode=False,
            )
            raw_responses.append(continuation)
            
            accumulated = self._merge_json_responses(accumulated, continuation)
            
            parsed = self._try_parse_json(accumulated)
            if parsed is not None:
                return parsed
            
            if not self._is_json_truncated(accumulated):
                break
        
        return None

    def _generate_recovery_suggestions(
        self,
        prompt: str,
        raw_responses: List[str],
    ) -> List[str]:
        """Generate actionable suggestions for resolving the failure."""
        suggestions = []
        
        suggestions.append(
            "Simplify the request: Break your request into smaller, more specific parts. "
            "Complex requests with many fields are more likely to exceed output limits."
        )
        
        if len(prompt) > 2000:
            suggestions.append(
                f"Reduce prompt size: Your prompt is {len(prompt)} characters. "
                "Remove unnecessary context or examples to allow more room for the response."
            )
        
        suggestions.append(
            "Use expected_keys parameter: When calling complete_json(), provide "
            "expected_keys=['key1', 'key2', ...] to help with automatic task decomposition."
        )
        
        suggestions.append(
            "Use a model with higher output limits: Configure SW_LLM_MODEL to use a model "
            "that supports longer outputs (e.g., models with higher num_ctx settings)."
        )
        
        suggestions.append(
            "Increase model context: For Ollama, set num_ctx in model options to a higher value "
            "(e.g., 8192 or 16384) to allow longer responses."
        )
        
        suggestions.append(
            "Check the timeout: Increase SW_LLM_TIMEOUT if the model needs more time. "
            "Current timeout may be causing premature termination."
        )
        
        if raw_responses:
            last = raw_responses[-1].lower()
            if "error" in last or "cannot" in last or "unable" in last:
                suggestions.append(
                    "Review LLM response: The model may be refusing the request. "
                    "Check the raw response in the error output for refusal messages."
                )
        
        return suggestions

    def _ollama_complete(
        self,
        prompt: str,
        *,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        system_prompt: Optional[str] = None,
        json_mode: bool = False,
    ) -> str:
        """Make a completion request to Ollama."""
        url = f"{self.base_url}/api/generate"
        
        payload = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "options": {
                "temperature": temperature,
            },
        }
        
        if max_tokens:
            payload["options"]["num_predict"] = max_tokens
        
        if system_prompt:
            payload["system"] = system_prompt
        
        if json_mode:
            payload["format"] = "json"
        
        last_error = None
        for attempt in range(self.max_retries):
            try:
                with httpx.Client(timeout=self.timeout) as client:
                    response = client.post(url, json=payload)
                    response.raise_for_status()
                    data = response.json()
                    return data.get("response", "")
            except httpx.HTTPError as e:
                last_error = e
                logger.warning(
                    "LLM request failed (attempt %d/%d): %s",
                    attempt + 1, self.max_retries, e
                )
        
        raise LLMError(f"LLM request failed after {self.max_retries} attempts: {last_error}")

    def _dummy_complete(self, prompt: str) -> str:
        """Return a dummy completion for testing."""
        return f"[DUMMY] Response to: {prompt[:100]}..."

    def _dummy_complete_json(self, prompt: str) -> Dict[str, Any]:
        """Return a dummy JSON completion for testing."""
        if "intent" in prompt.lower():
            return {
                "primary_intent": "general",
                "secondary_intents": [],
                "entities": {},
                "confidence": 0.8,
            }
        if "extract" in prompt.lower():
            return {
                "extracted_info": [],
                "reasoning": "Dummy extraction",
            }
        return {"status": "dummy", "message": "This is a test response"}


def get_llm_client(agent_key: Optional[str] = None) -> LLMClient:
    """
    Get an LLM client, optionally configured for a specific agent.
    
    Args:
        agent_key: Optional agent identifier for agent-specific model config.
        
    Returns:
        Configured LLMClient
    """
    model = None
    if agent_key:
        env_key = f"SW_LLM_MODEL_{agent_key}"
        model = os.getenv(env_key)
    
    return LLMClient(model=model)
